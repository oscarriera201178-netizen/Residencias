import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import libsql

import db
from portal_auth import AuthStore, seed_from_secrets


PASSWORD = "contraseña-larga-1"
ADMIN_PASSWORD = "contraseña-larga-2"


class RowTest(unittest.TestCase):
    def test_access_by_name_and_index(self):
        row = db.Row(["id", "name"], (7, "Ana"))
        self.assertEqual(row["name"], "Ana")
        self.assertEqual(row[0], 7)
        self.assertEqual(row.keys(), ["id", "name"])
        self.assertEqual(dict(row), {"id": 7, "name": "Ana"})
        self.assertEqual(tuple(row), (7, "Ana"))
        self.assertEqual(row, (7, "Ana"))
        self.assertEqual(len(row), 2)


class LibsqlWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "wrapped.sqlite3"

    def test_wrapper_matches_sqlite_api(self):
        connection = db._ConnectionWrapper(libsql.connect(str(self.path)))
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        )
        with connection:
            cursor = connection.execute("INSERT INTO items (name) VALUES ('a')")
            self.assertEqual(cursor.lastrowid, 1)
            cursor = connection.execute("UPDATE items SET name='b' WHERE id=1")
            self.assertEqual(cursor.rowcount, 1)
        row = connection.execute("SELECT id, name FROM items").fetchone()
        self.assertEqual(row["name"], "b")
        self.assertEqual(row[0], 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1,
        )
        connection.close()

    def test_wrapper_transaction_rolls_back_on_error(self):
        connection = db._ConnectionWrapper(libsql.connect(str(self.path)))
        connection.executescript("CREATE TABLE items (name TEXT NOT NULL);")
        with self.assertRaises(ValueError):
            with connection:
                connection.execute("INSERT INTO items VALUES ('x')")
                raise ValueError("forzar rollback")
        self.assertIsNone(connection.execute("SELECT * FROM items").fetchone())
        connection.close()


class ConnectTest(unittest.TestCase):
    def test_local_sqlite_without_credentials(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                connection = db.connect(Path(tmp) / "local.sqlite3")
            self.assertIsInstance(connection, sqlite3.Connection)
            connection.close()


class SeedFromSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AuthStore(Path(self.tmp.name) / "auth.sqlite3")

    def test_seed_creates_programmer_and_administrator(self):
        created = seed_from_secrets(
            self.store,
            {"programmer_password": PASSWORD, "administrator_password": ADMIN_PASSWORD},
        )
        self.assertTrue(created)
        self.assertTrue(self.store.initialized())
        token = self.store.login("programador", PASSWORD)
        self.assertEqual(self.store.validate(token)["role"], "programmer")
        token = self.store.login("administrador", ADMIN_PASSWORD)
        admin = self.store.validate(token)
        self.assertEqual(admin["role"], "administrator")
        self.assertEqual(admin["must_change"], 0)

    def test_seed_is_skipped_when_initialized_or_unconfigured(self):
        self.assertFalse(seed_from_secrets(self.store, {}))
        self.store.bootstrap("programador", PASSWORD)
        self.assertFalse(
            seed_from_secrets(self.store, {"programmer_password": PASSWORD}),
        )

    def test_seed_env_fallback(self):
        from portal_auth import _seed_config

        with mock.patch.dict(
            os.environ,
            {
                "RESIDENCIAS_PROGRAMMER_PASSWORD": PASSWORD,
                "RESIDENCIAS_ADMINISTRATOR_PASSWORD": ADMIN_PASSWORD,
            },
            clear=True,
        ):
            with mock.patch("portal_auth.st") as mock_st:
                mock_st.secrets.get.return_value = {}
                config = _seed_config()
        self.assertEqual(config["programmer_password"], PASSWORD)
        self.assertEqual(config["administrator_password"], ADMIN_PASSWORD)


if __name__ == "__main__":
    unittest.main()
