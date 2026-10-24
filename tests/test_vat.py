import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest
from streamlit.testing.v1 import AppTest

from resident_store import BANKS, ResidentStore
from vat_report import calculate_vat_report, generate_vat_pdf


TODAY = date.today()
MONTH = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=1)
APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def store(tmp_path):
    store = ResidentStore(tmp_path / "vat.sqlite3")
    for residence in (0, 1):
        resident = store.add(
            residence, {
                "student_name": "RESIDENTE DE PRUEBA", "student_id": "V-12345678",
                "room_number": "1", "bed_number": "1",
                "admission_date": MONTH.strftime("%d/%m/%Y"),
            }, [], b"pdf", None,
        )
        store.record_opening_balance(
            residence, MONTH, "VES", BANKS[0], Decimal("1000"), "Apertura",
        )
        with store._connect() as connection:
            connection.execute(
                "INSERT INTO payments (resident_id, due_date, paid_on, currency, amount, "
                "receiving_bank, invoice_number) VALUES (?, ?, ?, 'VES', '1000', ?, 'COBRO-1')",
                (resident, MONTH.isoformat(), MONTH.isoformat(), BANKS[0]),
            )
        store.record_expense(
            residence, "Compra con factura", MONTH + timedelta(days=1), "VES",
            BANKS[0], Decimal("116"), with_invoice=True, invoice_number="GASTO-1",
        )
    return store


def test_vat_ledger_is_chronological_and_excludes_non_invoiced_and_dollar_activity(store):
    store.record_expense(0, "Sin factura", MONTH, "VES", BANKS[0], Decimal("10"))
    with store._connect() as connection:
        resident = connection.execute("SELECT id FROM residents WHERE residence = 0").fetchone()["id"]
        connection.execute(
            "INSERT INTO deposits (resident_id, paid_on, currency, amount, payment_method, "
            "invoice_number) VALUES (?, ?, 'USD', '100', 'Zelle', 'USD-1')",
            (resident, MONTH.isoformat()),
        )
        connection.execute(
            "INSERT INTO service_payments (resident_id, concept, paid_on, currency, amount, "
            "receiving_bank, invoice_number, receipt_number, receipt_pdf) "
            "VALUES (?, 'Llaves', ?, 'VES', '100', ?, 'SERVICIO-1', '1', ?)",
            (resident, MONTH.isoformat(), BANKS[0], b"pdf"),
        )
    report = store.vat_report(0, MONTH)
    assert len(report.entries) == 3
    assert report.entries[-1]["Nro. de factura"] == "GASTO-1"
    assert report.debit == Decimal("176.00")
    assert report.credit == Decimal("16.00")
    assert report.net == Decimal("160.00")
    assert store.vat_report(1, MONTH).net == Decimal("144.00")
    assert not store.vat_report(0, TODAY.replace(day=1)).entries


def test_vat_payment_is_persistent_debits_bank_once_and_does_not_create_credit(store):
    store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF-IVA", Decimal("144"))
    restored = ResidentStore(store.path)
    payment = restored.vat_payment(0, MONTH)
    assert payment["amount"] == "144.00"
    assert payment["reference"] == "REF-IVA"
    assert restored.account_balances(0, TODAY)[0]["amount"] == Decimal("1740")
    assert restored.account_balances(1, TODAY)[0]["amount"] == Decimal("1884")
    assert restored.vat_report(0, MONTH).credit == Decimal("16")
    assert len(restored.vat_report(0, MONTH).entries) == 2
    assert next(e for e in restored.expenses(0, TODAY) if "Pago de IVA" in e["concept"])["vat"] == "0"
    with pymupdf.open(
        stream=generate_vat_pdf(0, MONTH, restored.vat_report(0, MONTH), payment),
        filetype="pdf",
    ) as document:
        assert "REF-IVA" in "".join(page.get_text() for page in document)
    with pytest.raises(ValueError, match="ya está registrado"):
        restored.record_vat_payment(0, MONTH, TODAY, BANKS[0], "DUPLICADO", Decimal("144"))


def test_vat_pdf_formats_recorded_payment_amounts_in_bolivars(store):
    payment = {
        "paid_on": TODAY.isoformat(), "payment_method": BANKS[0], "reference": "REF",
        "amount": "19629.93", "debit": "35492.00", "credit": "15862.07",
    }
    with pymupdf.open(
        stream=generate_vat_pdf(0, MONTH, store.vat_report(0, MONTH), payment),
        filetype="pdf",
    ) as document:
        text = "".join(page.get_text() for page in document)
    assert "35.492,00" in text
    assert "15.862,07" in text
    assert "19.629,93" in text
    assert "35492.00" not in text
    assert "15862.07" not in text


def test_vat_payment_rejects_changed_amount_insufficient_balance_and_readonly(store):
    with pytest.raises(ValueError, match="cambió"):
        store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF", Decimal("143"))
    with pytest.raises(ValueError, match="Saldo insuficiente"):
        store.record_vat_payment(0, MONTH, TODAY, BANKS[1], "REF", Decimal("144"))
    assert store.vat_payment(0, MONTH) is None

    def deny():
        raise PermissionError("Solo consulta")

    store.authorize_write = deny
    with pytest.raises(PermissionError):
        store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF", Decimal("144"))


def test_concurrent_vat_payment_is_not_duplicated(store):
    def pay(_):
        try:
            store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF", Decimal("144"))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(pay, range(2))) == [False, True]


def test_failed_vat_payment_rolls_back_the_expense_and_bank_balance(store):
    with store._connect() as connection:
        connection.executescript(
            "CREATE TRIGGER fail_vat BEFORE INSERT ON vat_payments "
            "BEGIN SELECT RAISE(ABORT, 'Fallo de prueba'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF", Decimal("144"))
    assert store.vat_payment(0, MONTH) is None
    assert len(store.expenses(0, TODAY)) == 1
    assert store.account_balances(0, TODAY)[0]["amount"] == Decimal("1884")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "incorrecto", None])
def test_invalid_invoice_blocks_vat_calculation(amount):
    with pytest.raises(ValueError, match="no válido"):
        calculate_vat_report([{
            "kind": "Cobro", "paid_on": MONTH.isoformat(),
            "invoice_number": "INV", "concept": "Compra", "amount": amount, "vat": None,
        }])


def test_vat_credit_balance_cannot_be_paid(store):
    store.record_expense(
        0, "Compra adicional", MONTH, "VES", BANKS[0], Decimal("1160"),
        with_invoice=True, invoice_number="GASTO-2",
    )
    assert store.vat_report(0, MONTH).net == Decimal("-16")
    with pytest.raises(ValueError, match="No hay IVA"):
        store.record_vat_payment(0, MONTH, TODAY, BANKS[0], "REF", Decimal("-16"))


def test_vat_pdf_empty_and_multiple_pages(store):
    report = store.vat_report(0, MONTH)
    with pymupdf.open(stream=generate_vat_pdf(0, MONTH, report), filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        assert "PAGO DE IVA (16%)" in text
        assert "IVA A PAGAR:" in text
        assert "Débito menos crédito" not in text
        assert "COBRO-1" in text
        assert "GASTO-1" in text
        assert "144,00" in text
    empty = calculate_vat_report([])
    with pymupdf.open(stream=generate_vat_pdf(1, MONTH, empty), filetype="pdf") as document:
        assert "No hay facturas" in document[0].get_text()
    movements = [{
        "kind": "Cobro", "paid_on": MONTH.isoformat(), "invoice_number": f"FACT-{index}",
        "concept": "Mensualidad", "amount": "100", "vat": None,
    } for index in range(100)]
    with pymupdf.open(
        stream=generate_vat_pdf(0, MONTH, calculate_vat_report(movements)), filetype="pdf",
    ) as document:
        assert len(document) > 1
        assert "FACT-99" in "".join(page.get_text() for page in document)


@pytest.mark.parametrize("can_write", [True, False])
def test_vat_ui_permissions_pdf_and_payment(store, can_write):
    access = SimpleNamespace(
        can_write=can_write, role="administrator" if can_write else "viewer",
        require_write=lambda: None,
    )
    with (
        patch("resident_store.ResidentStore", return_value=store),
        patch("portal_auth.require_login", return_value=access),
        patch("portal_auth.select_residence_area", return_value=(0, 1)),
        patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), TODAY)),
    ):
        app = AppTest.from_file(APP, default_timeout=30).run()
        app.button(key="residencia_action_0_6").click().run()
        app.selectbox(key="vat_0_month").select(MONTH.strftime("%m/%Y")).run()
        assert not app.exception
        assert any(b.label == "Descargar informe de IVA (PDF)" for b in app.get("download_button"))
        submit = [b for b in app.button if b.label == "Registrar pago de IVA"]
        assert bool(submit) == can_write
        if can_write:
            app.text_input(key="vat_0_reference").set_value("REF-UI")
            app.checkbox(key="vat_0_confirmed").check()
            submit[0].click().run()
            assert not app.exception
            assert not app.error
            assert store.vat_payment(0, MONTH)["reference"] == "REF-UI"
            assert app.success
