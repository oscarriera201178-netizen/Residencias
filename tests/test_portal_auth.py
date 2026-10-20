import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from portal_auth import AuthStore, IDLE_SECONDS, SESSION_SECONDS
from resident_store import ResidentStore


APP = str(Path(__file__).resolve().parents[1] / "app.py")
PASSWORD = "Initial-test-password-123"
TEMPORARY = "Temporary-test-password-456"
NEW = "Changed-test-password-789"


class AuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.auth = AuthStore(self.path / "auth.sqlite3")
        self.auth.bootstrap("programador", PASSWORD)
        self.token = self.auth.login("programador", PASSWORD)

    def account(self, name="consulta", role="viewer"):
        self.auth.create_user(self.token, name, TEMPORARY, role)
        token = self.auth.login(name, TEMPORARY)
        self.auth.change_password(token, TEMPORARY, NEW)
        return self.auth.login(name, NEW)

    def test_bootstrap_hashes_and_quota_persist(self):
        with self.assertRaises(ValueError):
            self.auth.bootstrap("otro", PASSWORD)
        self.account("admin", "administrator")
        self.account("lector1")
        self.account("lector2")
        for role in ("programmer", "administrator", "viewer"):
            with self.assertRaises(ValueError):
                self.auth.create_user(self.token, "extra", PASSWORD, role)
        with sqlite3.connect(self.auth.path) as connection:
            hashes = connection.execute("SELECT password_hash FROM users").fetchall()
        self.assertTrue(all(PASSWORD.encode() not in h[0] for h in hashes))
        self.assertEqual(len(AuthStore(self.auth.path).users(self.token)), 4)
        self.assertEqual(self.auth.path.stat().st_mode & 0o777, 0o600)

    def test_password_rules_and_atomic_invalid_creates(self):
        for username, password, role in (
            ("x", PASSWORD, "viewer"), ("a' OR 1=1", PASSWORD, "viewer"),
            ("valid", "short", "viewer"), ("valid", "x" * 129, "viewer"),
            ("valid", PASSWORD, "unknown"),
        ):
            with self.assertRaises(ValueError):
                self.auth.create_user(self.token, username, password, role)
        self.assertEqual(len(self.auth.users(self.token)), 1)

    def test_concurrent_creation_never_exceeds_account_limit(self):
        def create(index):
            try:
                self.auth.create_user(self.token, f"admin{index}", TEMPORARY, "administrator")
            except ValueError:
                return False
            return True
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(create, range(3))), 1)
        self.assertEqual(len(self.auth.users(self.token)), 2)

    def test_recover_programmer_revokes_sessions_without_creating_account(self):
        self.auth.recover_programmer(NEW)
        with self.assertRaises(PermissionError):
            self.auth.validate(self.token)
        with self.assertRaises(ValueError):
            self.auth.login("programador", PASSWORD)
        token = self.auth.login("programador", NEW)
        self.assertEqual(len(self.auth.users(token)), 1)

    def test_uninitialized_portal_shows_only_initial_setup_without_private_data(self):
        empty = AuthStore(self.path / "empty.sqlite3")
        with patch("portal_auth.AuthStore", return_value=empty):
            app = AppTest.from_file(APP, default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertFalse(app.get("download_button"))
            self.assertEqual(
                [form.key for form in app.get("form")], ["activate_programmer"],
            )
            self.assertEqual(app.text_input[0].value, "Programador")
            self.assertTrue(app.text_input[0].disabled)
            self.assertEqual(len(app.text_input), 3)
            self.assertTrue(any("antes de publicar" in item.value for item in app.warning))

    def test_programmer_activation_requires_confirmation_and_is_one_time(self):
        store = AuthStore(self.path / "activation.sqlite3")
        for password, confirmation in (
            (PASSWORD, NEW), ("short", "short"),
        ):
            with self.assertRaises(ValueError):
                store.activate_programmer(password, confirmation)
            self.assertFalse(store.initialized())
        store.activate_programmer(PASSWORD, PASSWORD)
        token = store.login("Programador", PASSWORD)
        self.assertEqual(store.validate(token)["role"], "programmer")
        self.assertFalse(store.validate(token)["must_change"])
        with self.assertRaises(ValueError):
            store.activate_programmer(NEW, NEW)
        restored = AuthStore(store.path)
        self.assertFalse(restored.validate(restored.login("Programador", PASSWORD))["must_change"])

    def test_concurrent_initial_setup_creates_only_one_programmer(self):
        store = AuthStore(self.path / "concurrent_activation.sqlite3")
        def activate(index):
            try:
                store.activate_programmer(PASSWORD, PASSWORD)
            except ValueError:
                return False
            return True
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(activate, range(2))), 1)

    def test_browser_activation_saves_programmer_password_and_returns_to_login(self):
        store = AuthStore(self.path / "browser_activation.sqlite3")
        with patch("portal_auth.AuthStore", return_value=store):
            app = AppTest.from_file(APP, default_timeout=30).run()
            self.assertFalse(app.exception)
            form = next(f for f in app.get("form") if f.key == "activate_programmer")
            self.assertEqual(form.text_input[0].value, "Programador")
            self.assertTrue(form.text_input[0].disabled)
            self.assertEqual(len(form.text_input), 3)
            form.text_input[1].set_value(PASSWORD)
            form.text_input[2].set_value(PASSWORD)
            form.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertTrue(any(b.label == "Entrar" for b in app.button))
            self.assertFalse(store.validate(store.login("Programador", PASSWORD))["must_change"])

    def test_lockout_logout_expiration_and_revocation(self):
        with patch("portal_auth.time.time", return_value=10000):
            for _ in range(5):
                with self.assertRaises(ValueError):
                    self.auth.login("programador", "incorrect-password")
            with self.assertRaises(ValueError):
                self.auth.login("programador", PASSWORD)
        with patch("portal_auth.time.time", return_value=10901):
            token = self.auth.login("programador", PASSWORD)
        with patch("portal_auth.time.time", return_value=10901 + IDLE_SECONDS):
            with self.assertRaises(PermissionError):
                self.auth.validate(token)
        with patch("portal_auth.time.time", return_value=50000):
            token = self.auth.login("programador", PASSWORD)
        with patch("portal_auth.time.time", return_value=50000 + SESSION_SECONDS):
            with self.assertRaises(PermissionError):
                self.auth.validate(token)
        self.auth.logout(self.token)
        with self.assertRaises(PermissionError):
            self.auth.validate(self.token)

    def test_roles_temporary_password_disable_and_reset(self):
        viewer = self.account()
        admin = self.account("admin", "administrator")
        self.auth.require_write(admin)
        with self.assertRaises(PermissionError):
            self.auth.require_write(viewer)
        for token in (viewer, admin):
            with self.assertRaises(PermissionError):
                self.auth.create_user(token, "intruso", PASSWORD, "viewer")
            with self.assertRaises(PermissionError):
                self.auth.users(token)
        target = next(u for u in self.auth.users(self.token) if u["username"] == "consulta")
        self.auth.update_user(self.token, target["id"], False)
        with self.assertRaises(PermissionError):
            self.auth.validate(viewer)
        with self.assertRaises(ValueError):
            self.auth.login("consulta", NEW)
        self.auth.update_user(self.token, target["id"], True, TEMPORARY)
        token = self.auth.login("consulta", TEMPORARY)
        self.assertTrue(self.auth.validate(token)["must_change"])
        with self.assertRaises(PermissionError):
            self.auth.require_write(token)
        with self.assertRaises(ValueError):
            self.auth.update_user(self.token, 1, False)
        with self.assertRaises(ValueError):
            self.auth.change_password(token, "incorrect", NEW)

    def test_guard_denies_every_financial_write_before_mutation(self):
        store = ResidentStore(self.path / "residents.sqlite3")
        token = self.account()
        store.authorize_write = lambda: self.auth.require_write(token)
        today = date.today()
        actions = (
            lambda: store.add(0, {}, [], b"", None),
            lambda: store.delete_admission(1, 0),
            lambda: store.record_payment(1, 0, today, today, "USD", Decimal("1")),
            lambda: store.record_service_payment(1, 0, "Llaves", today, "USD", Decimal("1")),
            lambda: store.record_deposit(1, 0, today, "Efectivo", Decimal("1")),
            lambda: store.set_invoice_number(0, "Mensualidad", 1, "test"),
            lambda: store.record_opening_balance(0, today, "USD", "Efectivo", Decimal("1"), "test"),
            lambda: store.discharge(1, 0, today, "test"),
            lambda: store.upgrade_legacy_receipts(),
            lambda: store.regenerate_deposit_receipt(1, 0),
        )
        for action in actions:
            with self.assertRaises(PermissionError):
                action()
        self.assertEqual(store.list_residents(0), [])

    def test_login_gate_and_first_password_change_block_private_views(self):
        with patch("portal_auth.AuthStore", return_value=self.auth):
            app = AppTest.from_file(APP, default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.tabs), 0)
            self.assertEqual(len(app.get("download_button")), 0)
            self.auth.create_user(self.token, "consulta", TEMPORARY, "viewer")
            app.text_input[0].set_value("consulta")
            app.text_input[1].set_value(TEMPORARY)
            next(b for b in app.button if b.label == "Entrar").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.tabs), 0)
            self.assertTrue(any("contraseña temporal" in w.value for w in app.warning))
            self.assertFalse(any(b.label == "Nuevo Ingreso" for b in app.button))
            form = next(f for f in app.get("form") if f.key == "change_password")
            form.text_input[0].set_value(TEMPORARY)
            form.text_input[1].set_value(NEW)
            form.text_input[2].set_value(NEW)
            form.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertTrue(any(b.label == "Entrar" for b in app.button))
            token = self.auth.login("consulta", NEW)
            self.assertFalse(self.auth.validate(token)["must_change"])

    def test_programmer_can_create_and_reset_users_in_portal(self):
        store = ResidentStore(self.path / "residents.sqlite3")
        with patch("portal_auth.AuthStore", return_value=self.auth), patch("resident_store.ResidentStore", return_value=store):
            app = AppTest.from_file(APP, default_timeout=30)
            app.session_state["auth_token"] = self.token
            app.run()
            self.assertFalse(app.exception)
            form = next(f for f in app.get("form") if f.key == "create_user")
            form.text_input[0].set_value("admin")
            form.selectbox[0].select("administrator")
            form.text_input[1].set_value(TEMPORARY)
            form.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(self.auth.users(self.token)), 2)
            admin = self.auth.login("admin", TEMPORARY)
            form = next(f for f in app.get("form") if f.key == "manage_user_2")
            form.checkbox[0].uncheck()
            form.button[0].click().run()
            self.assertFalse(app.exception)
            with self.assertRaises(PermissionError):
                self.auth.validate(admin)

    def test_administrator_selects_single_area_and_switches_without_logout(self):
        from test_residents import admission_values
        store = ResidentStore(self.path / "areas.sqlite3")
        for residence in (0, 1):
            values = admission_values()
            values["student_id"] = f"V-AREA-{residence}"
            store.add(residence, values, [False] * 8, b"%PDF-test", None)
        token = self.account("admin", "administrator")
        with patch("portal_auth.AuthStore", return_value=self.auth), patch(
            "resident_store.ResidentStore", return_value=store,
        ), patch.object(store, "list_residents", wraps=store.list_residents) as listed:
            app = AppTest.from_file(APP, default_timeout=30)
            app.session_state["auth_token"] = token
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any(
                "admin" in item.value
                and "Usuario:" not in item.value
                and "</div>" not in item.value
                for item in app.markdown
            ))
            self.assertFalse(app.tabs)
            self.assertFalse(app.get("download_button"))
            listed.assert_not_called()
            app.button(key="enter_residence_area").click().run()
            self.assertTrue(any("Selecciona la residencia" in e.value for e in app.error))
            for residence, label in ((1, "Residencia Las Acacias"), (0, "Residencia Carmen Luisa")):
                app.selectbox(key="residence_area_choice").select(residence)
                app.button(key="enter_residence_area").click().run()
                self.assertFalse(app.exception)
                self.assertEqual([t.label for t in app.tabs], [label])
                self.assertTrue(any(
                    b.key == f"residencia_action_{residence}_1" for b in app.button
                ))
                self.assertFalse(any(
                    b.key == f"residencia_action_{1 - residence}_1" for b in app.button
                ))
                listed.reset_mock()
                app.button(key=f"residencia_action_{residence}_1").click().run()
                self.assertFalse(app.exception)
                self.assertTrue(listed.called)
                self.assertTrue(all(call.args[0] == residence for call in listed.call_args_list))
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                self.assertTrue(any(
                    b.key == f"monthly_received_pdf_{residence}"
                    for b in app.get("download_button")
                ))
                self.assertFalse(any(
                    b.key == f"monthly_received_pdf_{1 - residence}"
                    for b in app.get("download_button")
                ))
                app.session_state["admission_draft_0"] = {"student_name": "BORRADOR"}
                app.button(key="change_residence_area").click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.tabs)
                self.assertEqual(app.session_state["auth_token"], token)
                self.assertNotIn("admission_draft_0", app.session_state)
                self.assertNotIn("administrator_area", app.session_state)
            next(b for b in app.button if b.label == "Cerrar sesión").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label == "Entrar" for b in app.button))
            self.assertNotIn("administrator_area", app.session_state)

    def test_viewer_has_downloads_but_no_mutation_controls_even_with_forged_view(self):
        from test_residents import admission_values
        store = ResidentStore(self.path / "residents.sqlite3")
        resident = store.add(0, admission_values(), [False] * 8, b"%PDF-test", None)
        store.record_service_payment(
            resident, 0, "Llaves", date.today(), "USD", Decimal("10"), payment_method="Zelle",
        )
        token = self.account()
        with store._connect() as connection:
            before = connection.iterdump()
            snapshot = list(before)
        with patch("portal_auth.AuthStore", return_value=self.auth), patch("resident_store.ResidentStore", return_value=store):
            app = AppTest.from_file(APP, default_timeout=30)
            app.session_state["auth_token"] = token
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(app.get("download_button"))
            self.assertFalse(app.get("popover"))
            self.assertFalse(any("Nuevo Ingreso" == b.label for b in app.button))
            for view in (2, 3, 4):
                app.button(key=f"residencia_action_0_{view}").click().run()
                self.assertFalse(app.exception)
                self.assertFalse(any(
                    "Registrar" in b.label or "Guardar" in b.label or "Crear usuario" == b.label
                    for b in app.button
                ))
            app.session_state["residence_view_0"] = 0
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any("solo permite" in e.value for e in app.error))
            self.assertFalse(any(b.label == "Guardar ingreso y generar planilla" for b in app.button))
            app.session_state["residence_view_0"] = 1
            self.auth.logout(token)
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertFalse(app.get("download_button"))
        with store._connect() as connection:
            self.assertEqual(list(connection.iterdump()), snapshot)


if __name__ == "__main__":
    unittest.main()
