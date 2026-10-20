import argparse
import getpass
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import streamlit as st


LOGGER = logging.getLogger(__name__)
ROLES = {
    "programmer": "ADMINISTRADOR PROGRAMADOR",
    "administrator": "ADMINISTRADOR",
    "viewer": "USUARIO VISUALIZAR",
}
LIMITS = {"programmer": 1, "administrator": 1, "viewer": 2}
IDLE_SECONDS = 30 * 60
SESSION_SECONDS = 8 * 60 * 60
RESIDENCE_NAMES = ("Residencia Carmen Luisa", "Residencia Las Acacias")


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=3, dklen=32, maxmem=64 * 1024 * 1024,
    )


def _check_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise ValueError("La contraseña debe tener entre 12 y 128 caracteres.")


class AuthStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("RESIDENCIAS_DATA_DIR", Path(__file__).parent / "data")) / "auth.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL CHECK(role IN ('programmer','administrator','viewer')),
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change INTEGER NOT NULL DEFAULT 1,
                    failures INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    created REAL NOT NULL,
                    touched REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialized(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM users WHERE role='programmer'").fetchone() is not None

    def bootstrap(self, username: str, password: str) -> None:
        self._create(username, password, "programmer", bootstrap=True)

    def activate_programmer(self, password: str, confirmation: str) -> None:
        if password != confirmation:
            raise ValueError("Las contraseñas no coinciden.")
        self._create("Programador", password, "programmer", bootstrap=True)

    def recover_programmer(self, password: str) -> None:
        _check_password(password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute("SELECT id FROM users WHERE role='programmer'").fetchone()
            if not user:
                raise ValueError("Primero crea la cuenta inicial con bootstrap.")
            self._set_password(connection, user["id"], password, False)
            connection.execute("UPDATE users SET active=1 WHERE id=?", (user["id"],))

    def _create(
        self, username: str, password: str, role: str, *,
        bootstrap: bool = False, token: str = "",
    ) -> None:
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
            raise ValueError("El usuario admite 3 a 40 letras sin acentos, números, punto, guion o guion bajo.")
        _check_password(password)
        if role not in ROLES:
            raise ValueError("Perfil no válido.")
        salt = secrets.token_bytes(16)
        password_hash = _password_hash(password, salt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if bootstrap:
                if role != "programmer" or connection.execute("SELECT 1 FROM users").fetchone():
                    raise ValueError("El portal ya tiene cuentas; no se permite otro registro inicial.")
            else:
                self._session(connection, token, programmer=True)
                if role == "programmer":
                    raise ValueError("Solo se permite un ADMINISTRADOR PROGRAMADOR.")
            count = connection.execute("SELECT COUNT(*) FROM users WHERE role=?", (role,)).fetchone()[0]
            if count >= LIMITS[role]:
                raise ValueError("Se alcanzó el máximo de cuentas de este perfil. Edita la cuenta existente.")
            if connection.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ValueError("Ese nombre de usuario ya existe.")
            connection.execute(
                "INSERT INTO users(username,role,salt,password_hash,must_change) VALUES(?,?,?,?,?)",
                (username, role, salt, password_hash, 0 if bootstrap else 1),
            )

    def create_user(self, token: str, username: str, password: str, role: str) -> None:
        self._create(username, password, role, token=token)

    def login(self, username: str, password: str) -> str:
        if len(password) > 128:
            raise ValueError("Usuario o contraseña incorrectos, cuenta inactiva o temporalmente bloqueada.")
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT * FROM users WHERE username=?", (username.strip().lower(),),
            ).fetchone()
            salt = bytes(user["salt"]) if user else b"\0" * 16
            valid = hmac.compare_digest(
                _password_hash(password, salt),
                bytes(user["password_hash"]) if user else b"\0" * 32,
            )
            if not user or not valid or not user["active"] or user["locked_until"] > now:
                if user and user["locked_until"] <= now:
                    failures = user["failures"] + 1
                    connection.execute(
                        "UPDATE users SET failures=?,locked_until=? WHERE id=?",
                        (0 if failures >= 5 else failures, now + 900 if failures >= 5 else 0, user["id"]),
                    )
                token = None
            else:
                token = secrets.token_urlsafe(32)
                connection.execute("UPDATE users SET failures=0,locked_until=0 WHERE id=?", (user["id"],))
                connection.execute(
                    "DELETE FROM sessions WHERE created < ? OR touched < ?",
                    (now - SESSION_SECONDS, now - IDLE_SECONDS),
                )
                connection.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(), user["id"], now, now),
                )
        if token is None:
            raise ValueError("Usuario o contraseña incorrectos, cuenta inactiva o temporalmente bloqueada.")
        return token

    def _session(self, connection, token: str, *, programmer: bool = False):
        now = time.time()
        key = hashlib.sha256(token.encode()).hexdigest()
        user = connection.execute(
            "SELECT u.*,s.created,s.touched FROM users u JOIN sessions s ON s.user_id=u.id "
            "WHERE s.token_hash=? AND u.active=1", (key,),
        ).fetchone()
        if not user or now - user["created"] >= SESSION_SECONDS or now - user["touched"] >= IDLE_SECONDS:
            raise PermissionError("La sesión venció. Inicia sesión nuevamente.")
        if programmer and (user["role"] != "programmer" or user["must_change"]):
            raise PermissionError("Solo el ADMINISTRADOR PROGRAMADOR puede gestionar usuarios.")
        connection.execute("UPDATE sessions SET touched=? WHERE token_hash=?", (now, key))
        return user

    def validate(self, token: str):
        with self.connect() as connection:
            return self._session(connection, token)

    def require_write(self, token: str) -> None:
        user = self.validate(token)
        if user["role"] not in ("programmer", "administrator") or user["must_change"]:
            raise PermissionError("Tu cuenta solo permite consultar y descargar.")

    def logout(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, token: str, current: str, password: str) -> None:
        _check_password(password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = self._session(connection, token)
            if not hmac.compare_digest(_password_hash(current, user["salt"]), user["password_hash"]):
                raise ValueError("La contraseña actual no es correcta.")
            if current == password:
                raise ValueError("La nueva contraseña debe ser distinta de la actual.")
            self._set_password(connection, user["id"], password, False)

    @staticmethod
    def _set_password(connection, user_id: int, password: str, must_change: bool) -> None:
        salt = secrets.token_bytes(16)
        connection.execute(
            "UPDATE users SET salt=?,password_hash=?,must_change=?,failures=0,locked_until=0 WHERE id=?",
            (salt, _password_hash(password, salt), int(must_change), user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def users(self, token: str):
        with self.connect() as connection:
            self._session(connection, token, programmer=True)
            return connection.execute("SELECT id,username,role,active FROM users ORDER BY id").fetchall()

    def update_user(self, token: str, user_id: int, active: bool, password: str = "") -> None:
        if password:
            _check_password(password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._session(connection, token, programmer=True)
            target = connection.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if not target or target["role"] == "programmer":
                raise ValueError("Esta acción solo permite administrar las cuentas subordinadas.")
            connection.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            if password:
                self._set_password(connection, user_id, password, True)


@dataclass
class Access:
    store: AuthStore
    token: str
    role: str

    @property
    def can_write(self) -> bool:
        return self.role in ("programmer", "administrator")

    def require_write(self) -> None:
        self.store.require_write(self.token)


def _password_form(store: AuthStore, token: str) -> None:
    with st.form("change_password", clear_on_submit=True):
        current = st.text_input("Contraseña actual", type="password")
        password = st.text_input("Nueva contraseña (mínimo 12 caracteres)", type="password")
        confirmation = st.text_input("Repetir nueva contraseña", type="password")
        submit = st.form_submit_button("Cambiar contraseña")
    if submit:
        if password != confirmation:
            st.error("Las contraseñas no coinciden.")
        else:
            try:
                store.change_password(token, current, password)
            except ValueError as error:
                st.error(str(error))
            else:
                st.session_state.clear()
                st.rerun()


def require_login() -> Access:
    try:
        store = AuthStore()
        if not store.initialized():
            st.info(
                "Establece y confirma la contraseña de Programador para crear la cuenta inicial."
            )
            st.warning(
                "Completa esta configuración antes de publicar el portal. Cualquier persona "
                "con acceso a esta pantalla podría crear la cuenta mientras esté pendiente."
            )
            with st.container():
                st.subheader("Crear ADMINISTRADOR PROGRAMADOR")
                with st.form("activate_programmer", clear_on_submit=True):
                    st.text_input("Usuario", value="Programador", disabled=True)
                    password = st.text_input("Nueva contraseña", type="password", max_chars=128)
                    confirmation = st.text_input("Confirmar contraseña", type="password", max_chars=128)
                    st.caption("Mínimo 12 caracteres. Esta será tu contraseña habitual; no es temporal.")
                    activate = st.form_submit_button("Guardar contraseña y activar cuenta")
                if activate:
                    try:
                        store.activate_programmer(password, confirmation)
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        st.session_state.clear()
                        st.rerun()
            st.stop()
        token = st.session_state.get("auth_token", "")
        user = None
        if token:
            try:
                user = store.validate(token)
            except PermissionError as error:
                st.session_state.clear()
                st.warning(str(error))
        if user is None:
            st.subheader("Iniciar sesión")
            with st.form("login", clear_on_submit=True):
                username = st.text_input("Usuario", max_chars=40)
                password = st.text_input("Contraseña", type="password", max_chars=128)
                submit = st.form_submit_button("Entrar")
            if submit:
                try:
                    token = store.login(username, password)
                except ValueError as error:
                    st.error(str(error))
                else:
                    st.session_state.clear()
                    st.session_state["auth_token"] = token
                    st.rerun()
            st.stop()
        if st.session_state.get("portal_username") != user["username"]:
            st.session_state["portal_username"] = user["username"]
            st.rerun()
        st.sidebar.write(f"{user['username']} — {ROLES[user['role']]}")
        if st.sidebar.button("Cerrar sesión"):
            store.logout(token)
            st.session_state.clear()
            st.rerun()
        if user["must_change"]:
            st.warning("Debes cambiar tu contraseña temporal antes de acceder al portal.")
            _password_form(store, token)
            st.stop()
        with st.sidebar.expander("Cambiar mi contraseña"):
            _password_form(store, token)
        return Access(store, token, user["role"])
    except PermissionError as error:
        st.session_state.clear()
        st.error(str(error))
        st.stop()
    except (sqlite3.Error, OSError):
        LOGGER.exception("Error de almacenamiento de autenticación")
        st.error("No se pudo verificar el acceso. El portal permanece bloqueado.")
        st.stop()


def select_residence_area(access: Access) -> tuple[int, ...]:
    if access.role != "administrator":
        return (0, 1)
    selected = st.session_state.get("administrator_area")
    if type(selected) is int and selected in (0, 1):
        st.sidebar.write(f"Área: {RESIDENCE_NAMES[selected]}")
        if st.sidebar.button("Cambiar de área", key="change_residence_area"):
            st.session_state.clear()
            st.session_state["auth_token"] = access.token
            st.rerun()
        return (selected,)
    st.subheader("Seleccionar área de trabajo")
    area = st.selectbox(
        "Residencia", (0, 1), index=None,
        placeholder="Selecciona una residencia",
        format_func=lambda value: RESIDENCE_NAMES[value],
        key="residence_area_choice",
    )
    enter = st.button(
        "Ingresar al área",
        type="primary",
        key="enter_residence_area",
        use_container_width=True,
    )
    if enter:
        if area is None:
            st.error("Selecciona la residencia a la que deseas ingresar.")
        else:
            st.session_state["administrator_area"] = area
            st.rerun()
    st.stop()


def render_user_management(access: Access) -> None:
    if access.role != "programmer":
        return
    with st.expander("Administración de usuarios"):
        st.caption("Máximo: un programador, un administrador y dos usuarios de consulta. Las cuentas inactivas también ocupan su cupo.")
        with st.form("create_user", clear_on_submit=True):
            username = st.text_input("Nuevo usuario", max_chars=40)
            role = st.selectbox("Perfil", ("administrator", "viewer"), format_func=ROLES.__getitem__)
            password = st.text_input("Contraseña temporal (mínimo 12 caracteres)", type="password")
            submit = st.form_submit_button("Crear usuario")
        if submit:
            try:
                access.store.create_user(access.token, username, password, role)
            except ValueError as error:
                st.error(str(error))
            else:
                st.success("Usuario creado. Entrega la contraseña por un canal privado; deberá cambiarla al entrar.")
        for user in access.store.users(access.token):
            st.write(f"{user['username']} — {ROLES[user['role']]}")
            if user["role"] == "programmer":
                continue
            with st.form(f"manage_user_{user['id']}", clear_on_submit=True):
                active = st.checkbox("Cuenta activa", value=bool(user["active"]))
                password = st.text_input("Nueva contraseña temporal (opcional)", type="password")
                save = st.form_submit_button("Guardar cuenta y cerrar sus sesiones")
            if save:
                try:
                    access.store.update_user(access.token, user["id"], active, password)
                except ValueError as error:
                    st.error(str(error))
                else:
                    st.success("Cuenta actualizada; sus sesiones anteriores quedaron cerradas.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crear la única cuenta inicial del portal.")
    parser.add_argument("command", choices=["bootstrap", "recover-programmer"])
    args = parser.parse_args()
    try:
        username = input("Usuario del ADMINISTRADOR PROGRAMADOR: ") if args.command == "bootstrap" else ""
        password = getpass.getpass("Contraseña (mínimo 12 caracteres): ")
        if password != getpass.getpass("Repetir contraseña: "):
            raise ValueError("Las contraseñas no coinciden.")
        if args.command == "bootstrap":
            AuthStore().bootstrap(username, password)
        else:
            AuthStore().recover_programmer(password)
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, f"No se pudo crear la cuenta: {error}\n")
    print("Cuenta preparada. Inicia sesión en el portal.")
