from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from client_api import app
from portal_auth import AuthStore
from resident_store import ResidentStore


def add_resident(store, name, identity):
    return store.add(
        0,
        {
            "student_name": name,
            "student_id": identity,
            "student_mobile": "0412-1234567",
            "email": "client@example.com",
            "admission_date": date.today().strftime("%d/%m/%Y"),
            "room_number": "1",
            "bed_number": "2",
        },
        [False] * 8,
        b"%PDF-test",
        None,
    )


def test_client_api_is_scoped_and_uses_a_secure_persistent_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENCIAS_DATA_DIR", str(tmp_path))
    auth = AuthStore(tmp_path / "auth.sqlite3")
    auth.bootstrap("programador", "Initial-test-password-123")
    programmer_token = auth.login("programador", "Initial-test-password-123")
    residents = ResidentStore(tmp_path / "residencias.sqlite3")
    first_id = add_resident(residents, "CLIENTE UNO", "V-ONE")
    second_id = add_resident(residents, "CLIENTE DOS", "V-TWO")
    auth.create_client_user(
        programmer_token, "cliente1", "Temporary-test-password-456",
        first_id, residents.get_resident(first_id).client_key,
    )
    auth.create_client_user(
        programmer_token, "cliente2", "Temporary-test-password-789",
        second_id, residents.get_resident(second_id).client_key,
    )
    client_token = auth.login("cliente1", "Temporary-test-password-456")
    auth.change_password(client_token, "Temporary-test-password-456", "Changed-test-password-789")

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/").status_code == 200
        assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
        response = client.post(
            "/api/login",
            json={"username": "cliente1", "password": "Changed-test-password-789"},
        )
        assert response.status_code == 200
        set_cookie = response.headers["set-cookie"].lower()
        assert "max-age=2592000" in set_cookie
        assert "httponly" in set_cookie
        assert "secure" in set_cookie
        assert "samesite=strict" in set_cookie

        response = client.get("/api/client/summary")
        assert response.status_code == 200
        assert response.json()["name"] == "CLIENTE UNO"
        assert "email" not in response.json()
        assert "student_mobile" not in response.json()
        assert client.get(f"/api/client/summary?resident_id={second_id}").json()["name"] == "CLIENTE UNO"

        residents.record_opening_balance(
            0, date.today(), "USD", "Efectivo", Decimal("100"), "Saldo de prueba",
        )
        residents.record_expense(
            0, "Mantenimiento común", date.today(), "USD", "Efectivo", Decimal("12.50"),
        )
        expenses = client.get("/api/client/expenses")
        assert expenses.status_code == 200
        assert expenses.json()[0]["concept"] == "Mantenimiento común"
        assert expenses.json()[0]["amount"] == "12.50"

        response = client.get("/api/client/documents/admission")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert client.delete("/api/login").status_code == 204
        assert client.get("/api/client/summary").status_code == 401


def test_administrative_accounts_cannot_log_into_the_client_portal(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENCIAS_DATA_DIR", str(tmp_path))
    auth = AuthStore(tmp_path / "auth.sqlite3")
    auth.bootstrap("programador", "Initial-test-password-123")
    programmer_token = auth.login("programador", "Initial-test-password-123")
    auth.create_user(
        programmer_token, "admin", "Temporary-test-password-456", "administrator",
    )
    token = auth.login("admin", "Temporary-test-password-456")
    auth.change_password(token, "Temporary-test-password-456", "Changed-test-password-789")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/login",
            json={"username": "admin", "password": "Changed-test-password-789"},
        )
        assert response.status_code == 403
        assert not client.cookies
