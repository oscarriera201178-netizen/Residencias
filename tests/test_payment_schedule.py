from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from payment_reminders import VENEZUELA, build_reminder, MailConfig, run_reminders
from payment_schedule import late_payment_days, next_month_start
from resident_store import ResidentStore, payment_status


def add_resident(store, residence, admission):
    return store.add(residence, {
        "student_name": "RESIDENTE PRUEBA", "student_id": f"V-TEST-{residence}-{admission}",
        "student_mobile": "0412-1234567", "email": "prueba@example.com",
        "admission_date": admission.strftime("%d/%m/%Y"),
        "room_number": "1", "bed_number": "2",
    }, [False] * 8, b"%PDF-test", None)


@pytest.mark.parametrize("admission", [
    date(2026, 1, 1), date(2026, 1, 15), date(2026, 1, 31),
    date(2026, 2, 28), date(2028, 2, 29), date(2026, 12, 31),
])
@pytest.mark.parametrize("residence", [0, 1])
def test_admission_then_calendar_months_without_skipping_arrears(tmp_path, admission, residence):
    store = ResidentStore(tmp_path / "calendar.sqlite3")
    resident_id = add_resident(store, residence, admission)
    due = admission
    for _ in range(4):
        assert store.list_residents(residence)[0].next_payment == due
        store.record_payment(
            resident_id, residence, due, date(2029, 1, 10), "USD",
            Decimal("100"), payment_method="Zelle",
        )
        due = next_month_start(due)
        store = ResidentStore(store.path)
    assert len(store.payment_history(resident_id, residence)) == 4
    assert store.list_residents(residence)[0].next_payment == due
    assert store.list_residents(1 - residence) == []
    notices = store.client_notifications()
    assert len(notices) == 4
    assert all(notice["kind"] == "payment" for notice in notices)
    assert all(
        notice["payload"] == {
            "message": "Se registró un pago recibido.",
            "residence": residence,
        }
        for notice in notices
    )


@pytest.mark.parametrize("day", range(1, 11))
def test_monthly_window_and_retardo_start_on_sixth(tmp_path, day):
    store = ResidentStore(tmp_path / "calendar.sqlite3")
    resident_id = add_resident(store, 0, date(2026, 1, 24))
    store.record_payment(
        resident_id, 0, date(2026, 1, 24), date(2026, 1, 24),
        "USD", Decimal("100"), payment_method="Zelle",
    )
    due, paid = date(2026, 2, 1), date(2026, 2, day)
    expected_days = max(0, day - 5)
    assert late_payment_days(due, paid) == expected_days
    assert payment_status(due, paid) == (
        "Pago pendiente (plazo del 1 al 5)" if day <= 5 else "Vencido"
    )
    if day <= 5:
        with pytest.raises(ValueError, match="plazo de pago"):
            store.record_payment(
                resident_id, 0, due, paid, "USD", Decimal("110"),
                payment_method="Zelle", monthly_amount_usd=Decimal("100"),
                late_fee_usd=Decimal("10"),
            )
    fee = Decimal("0") if day <= 5 else Decimal("10")
    store.record_payment(
        resident_id, 0, due, paid, "USD", Decimal("100") + fee,
        payment_method="Zelle", monthly_amount_usd=Decimal("100"), late_fee_usd=fee,
    )
    assert store.payment_history(resident_id, 0)[-1]["late_days"] == str(expected_days)
    assert store.list_residents(0)[0].next_payment == date(2026, 3, 1)
    with pymupdf.open(stream=store.get_payment_receipt(resident_id, 0, due), filetype="pdf") as document:
        text = document[0].get_text()
        if day <= 5:
            assert "Días de retardo:" not in text
        else:
            assert f"Días de retardo: {expected_days}" in text


def test_migration_preserves_history_and_discharge_and_runs_only_once(tmp_path):
    store = ResidentStore(tmp_path / "legacy.sqlite3")
    unpaid = add_resident(store, 0, date(2026, 1, 20))
    paid = add_resident(store, 1, date(2025, 12, 31))
    former = add_resident(store, 0, date(2026, 1, 24))
    store.discharge(former, 0, date(2026, 2, 1), "FIN")
    with store._connect() as connection:
        connection.execute("ALTER TABLE residents DROP COLUMN calendar_billing")
        connection.execute(
            "UPDATE residents SET next_payment = '2026-01-30' WHERE id = ?", (paid,),
        )
        connection.execute(
            "INSERT INTO payments (resident_id, due_date, paid_on, receipt_pdf, amount) "
            "VALUES (?, '2025-12-31', '2026-01-02', ?, '150')", (paid, b"PDF ORIGINAL"),
        )
    history = store.payment_history(paid, 1)
    migrated = ResidentStore(store.path)
    assert migrated.list_residents(0)[0].id == unpaid
    assert migrated.list_residents(0)[0].next_payment == date(2026, 1, 20)
    assert migrated.list_residents(1)[0].next_payment == date(2026, 1, 1)
    assert migrated.payment_history(paid, 1) == history
    assert migrated.get_payment_receipt(paid, 1, date(2025, 12, 31)) == b"PDF ORIGINAL"
    assert migrated.list_residents(0, inactive=True)[0].next_payment == date(2026, 1, 24)
    migrated.record_payment(
        paid, 1, date(2026, 1, 1), date(2026, 1, 5), "USD",
        Decimal("100"), payment_method="Zelle",
    )
    assert ResidentStore(store.path).list_residents(1)[0].next_payment == date(2026, 2, 1)


@pytest.mark.parametrize("residence", [0, 1])
@pytest.mark.parametrize("can_write", [False, True])
@pytest.mark.parametrize("today", [
    date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 5),
    date(2026, 11, 6), date(2026, 11, 15),
])
def test_ui_alerts_from_first_until_payment_in_both_residences(
    tmp_path, residence, can_write, today,
):
    store = ResidentStore(tmp_path / "ui.sqlite3")
    resident_id = add_resident(store, residence, date(2026, 10, 24))
    store.record_payment(
        resident_id, residence, date(2026, 10, 24), date(2026, 10, 24),
        "USD", Decimal("100"), payment_method="Zelle",
    )
    access = SimpleNamespace(
        role="administrator" if can_write else "viewer",
        can_write=can_write, require_write=lambda: None,
    )
    path = Path(__file__).resolve().parents[1] / "app.py"
    with (
        patch("resident_store.ResidentStore", return_value=store),
        patch("portal_auth.require_login", return_value=access),
        patch("portal_auth.select_residence_area", return_value=(0, 1)),
        patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), today)),
        patch("payment_schedule.billing_today", return_value=today),
    ):
        app = AppTest.from_file(str(path), default_timeout=30).run()
        app.button(key=f"residencia_action_{residence}_1").click().run()
        assert not app.exception
        assert app.tabs[residence].expander[0].label.startswith(":red[") == (today.month == 11)
        app.button(key=f"residencia_action_{residence}_2").click().run()
        assert not app.exception
        if today.month == 10:
            assert not app.tabs[residence].expander
            return
        assert len(app.tabs[residence].expander) == 1
        if can_write:
            key = f"resident_{residence}_{resident_id}_True_False"
            app.selectbox(key=f"{key}_payment_currency").select("Dólares").run()
            assert any(
                widget.key == f"{key}_payment_late_fee" for widget in app.text_input
            ) == (today.day >= 6)
            app.text_input(key=f"{key}_payment_amount").set_value("100")
            app.selectbox(key=f"{key}_payment_method").select("Zelle").run()
            next(
                item for item in app.tabs[residence].checkbox
                if item.label == "Confirmo que recibí este pago"
            ).check()
            next(
                item for item in app.tabs[residence].button if item.label == "Registrar pago"
            ).click().run()
        else:
            store.record_payment(
                resident_id, residence, date(2026, 11, 1), today,
                "USD", Decimal("100"), payment_method="Zelle",
            )
            app.run()
        assert not app.exception
        assert not app.tabs[residence].expander
        assert store.list_residents(residence)[0].next_payment == date(2026, 12, 1)


def test_email_eligibility_and_wording_follow_monthly_window(tmp_path):
    store = ResidentStore(tmp_path / "email.sqlite3")
    resident_id = add_resident(store, 0, date(2026, 10, 24))
    store.record_payment(
        resident_id, 0, date(2026, 10, 24), date(2026, 10, 24),
        "USD", Decimal("100"), payment_method="Zelle",
    )
    assert run_reminders(store, datetime(2026, 10, 31, 8, tzinfo=VENEZUELA))["eligible"] == 0
    for day in (1, 2, 3, 4, 5, 6, 15):
        now = datetime(2026, 11, day, 8, tzinfo=VENEZUELA)
        assert run_reminders(store, now)["eligible"] == 1
        body = build_reminder(
            store.list_residents(0)[0], now, MailConfig("sender@gmail.com", "test-only"),
        ).get_content()
        assert ("plazo del 1 al 5" in body) == (day <= 5)
