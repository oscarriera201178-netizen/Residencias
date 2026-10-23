"""Conexión a la base de datos del portal.

En desarrollo se usa el archivo SQLite local de siempre. En producción, cuando
existen las credenciales de Turso (st.secrets["turso"] o variables de entorno
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN), se abre una réplica local sincronizada
con la base de datos en la nube para que la información sobreviva a los
reinicios del hospedaje.

La réplica se sincroniza como máximo una vez cada SYNC_SECONDS por archivo:
cada sincronización es un viaje de red al servidor y hacerlo en cada operación
haría el portal extremadamente lento. Las escrituras se envían al servidor en
el momento, por lo que nada se pierde entre sincronizaciones.
"""

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

SYNC_SECONDS = 5.0
_last_sync: dict[str, float] = {}
_sync_lock = threading.Lock()
_connections: dict[str, "_ConnectionWrapper"] = {}
_connections_lock = threading.RLock()


def _turso_credentials() -> tuple[str, str]:
    url = token = ""
    try:
        import streamlit as st

        config = st.secrets.get("turso", {})
        url = config.get("url", "")
        token = config.get("token", "")
    except Exception:  # sin Streamlit o sin secrets configurados
        pass
    return (
        url or os.environ.get("TURSO_DATABASE_URL", ""),
        token or os.environ.get("TURSO_AUTH_TOKEN", ""),
    )


class Row:
    """Emula sqlite3.Row: acceso por nombre de columna o por posición."""

    __slots__ = ("_values", "_index")

    def __init__(self, columns: list[str], values) -> None:
        self._values = tuple(values)
        self._index: dict[str, int] = {}
        for position, name in enumerate(columns):
            self._index.setdefault(name, position)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({self._values!r})"

    def keys(self) -> list[str]:
        return list(self._index)


class _CursorWrapper:
    """Convierte las filas a Row cuando se configuró row_factory."""

    def __init__(self, cursor, row_factory) -> None:
        self._cursor = cursor
        self._row_factory = row_factory

    def _convert(self, row):
        if row is None or self._row_factory is None:
            return row
        columns = [column[0] for column in self._cursor.description]
        return Row(columns, row)

    def fetchone(self):
        return self._convert(self._cursor.fetchone())

    def fetchall(self) -> list:
        return [self._convert(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description


class _ConnectionWrapper:
    """Expone la misma API que sqlite3.Connection sobre una conexión libsql."""

    _PRAGMA_FOREIGN_KEYS = re.compile(r"^\s*PRAGMA\s+foreign_keys\b", re.IGNORECASE)

    def __init__(self, connection) -> None:
        self._connection = connection
        self.row_factory = None
        self._foreign_keys_done = False

    def execute(self, sql: str, parameters=()):
        # Turso aplica las llaves foráneas en el servidor; repetir el PRAGMA en
        # cada operación cuesta un viaje de red (~0,5 s), así que se envía una
        # sola vez por conexión y las repeticiones se responden en local.
        if not parameters and self._PRAGMA_FOREIGN_KEYS.match(sql):
            if self._foreign_keys_done:
                return _CursorWrapper(self._connection.execute("SELECT 1"), self.row_factory)
            self._foreign_keys_done = True
        return _CursorWrapper(self._connection.execute(sql, parameters), self.row_factory)

    def executescript(self, script: str):
        return self._connection.executescript(script)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        pass  # la conexión se reutiliza; cerrarla forzaría un costoso reenlace

    def sync(self) -> None:
        self._connection.sync()

    def __enter__(self):
        _connections_lock.acquire()
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            _connections_lock.release()


def _turso_connection(path: Path, url: str, token: str) -> "_ConnectionWrapper":
    """Devuelve la conexión réplica compartida del proceso, creándola una vez.

    Abrir una réplica libsql cuesta más de un segundo (negociación con el
    servidor), así que se conserva una única conexión por archivo. El candado
    serializa las transacciones de los hilos (pantallas) que la comparten.
    """
    import libsql

    key = str(path)
    with _connections_lock:
        wrapper = _connections.get(key)
        if wrapper is None:
            if path.exists() and path.stat().st_size == 0:
                path.unlink()  # un archivo vacío (touch) impide crear la réplica
            try:
                raw = libsql.connect(
                    key, sync_url=url, auth_token=token, timeout=10,
                    _check_same_thread=False,
                )
                raw.sync()
            except Exception as error:
                raise OSError(f"No se pudo conectar con la base de datos en la nube: {error}") from error
            wrapper = _ConnectionWrapper(raw)
            _connections[key] = wrapper
            with _sync_lock:
                _last_sync[key] = time.monotonic()
            return wrapper
    with _sync_lock:
        due = time.monotonic() - _last_sync.get(key, 0.0) > SYNC_SECONDS
        if due:
            _last_sync[key] = time.monotonic()
    if due:
        with _connections_lock:
            try:
                wrapper.sync()
            except Exception as error:
                raise OSError(f"No se pudo sincronizar con la base de datos en la nube: {error}") from error
    return wrapper


def connect(path: Path):
    """Abre la base de datos.

    Con credenciales de Turso devuelve una réplica embebida sincronizada con la
    nube (envuelta para ofrecer la API de sqlite3); sin ellas, el SQLite local.
    """
    url, token = _turso_credentials()
    if url and token:
        return _turso_connection(path, url, token)
    return sqlite3.connect(path, timeout=10)
