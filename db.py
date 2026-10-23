"""Conexión a la base de datos del portal.

En desarrollo se usa el archivo SQLite local de siempre. En producción, cuando
existen las credenciales de Turso (st.secrets["turso"] o variables de entorno
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN), se abre una réplica local sincronizada
con la base de datos en la nube para que la información sobreviva a los
reinicios del hospedaje.
"""

import os
import sqlite3
from pathlib import Path


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

    def __init__(self, connection) -> None:
        self._connection = connection
        self.row_factory = None

    def execute(self, sql: str, parameters=()):
        return _CursorWrapper(self._connection.execute(sql, parameters), self.row_factory)

    def executescript(self, script: str):
        return self._connection.executescript(script)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def sync(self) -> None:
        self._connection.sync()

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)


def connect(path: Path):
    """Abre la base de datos.

    Con credenciales de Turso devuelve una réplica embebida sincronizada con la
    nube (envuelta para ofrecer la API de sqlite3); sin ellas, el SQLite local.
    """
    url, token = _turso_credentials()
    if url and token:
        import libsql

        if path.exists() and path.stat().st_size == 0:
            path.unlink()  # un archivo vacío (touch) impide crear la réplica
        connection = libsql.connect(str(path), sync_url=url, auth_token=token, timeout=10)
        connection.sync()
        return _ConnectionWrapper(connection)
    return sqlite3.connect(path, timeout=10)
