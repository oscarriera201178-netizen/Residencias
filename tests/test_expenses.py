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

from expense_export import generate_expense_report
from monthly_report import calculate_monthly_available_summary, generate_monthly_report
from resident_store import BANKS, ResidentStore, calculate_expense_vat


TODAY = date.today()


@pytest.fixture
def store(tmp_path):
    return ResidentStore(tmp_path / "expenses.sqlite3")


def fund(store, currency="VES", method=BANKS[0], amount="1000", day=TODAY, residence=0):
    store.record_opening_balance(residence, day, currency, method, Decimal(amount), "Saldo de prueba")


def balance(store, currency="VES", method=BANKS[0], day=TODAY, residence=0):
    return next(
        row["amount"] for row in store.account_balances(residence, day)
        if row["currency"] == currency and row["payment_method"] == method
    )


@pytest.mark.parametrize(
    "currency,method,with_invoice",
    [
        ("VES", bank, with_invoice)
        for bank in BANKS
        for with_invoice in (True, False)
    ] + [
        ("USD", method, False)
        for method in ("Efectivo", "Binance", "Zelle")
    ],
)
def test_expenses_are_persistent_and_debit_only_the_selected_account(
    store, currency, method, with_invoice,
):
    fund(store, currency, method)
    store.record_expense(
        0, "Mantenimiento", TODAY, currency, method, Decimal("116"),
        with_invoice=with_invoice, invoice_number="000001-A" if with_invoice else "",
    )
    restored = ResidentStore(store.path)
    expense = restored.expenses(0, TODAY)[0]
    assert expense["vat"] == ("16.00" if with_invoice else "0")
    assert expense["invoice_number"] == ("000001-A" if with_invoice else None)
    assert expense["registered_on"] == TODAY.isoformat()
    assert "exchange_rate" not in expense
    assert balance(restored, currency, method) == Decimal("884")
    assert all(row["amount"] == 0 for row in restored.account_balances(1, TODAY))
    assert len(restored.expenses(1, TODAY)) == 0
    assert sum(row["amount"] for row in restored.account_balances(0, TODAY)) == Decimal("884")


@pytest.mark.parametrize("amount,expected", [("116", "16.00"), ("100", "13.79"), ("0.04", "0.01")])
def test_included_vat_is_not_added_to_the_total(amount, expected):
    assert calculate_expense_vat(Decimal(amount)) == Decimal(expected)


def test_expense_pdf_contains_full_ledger_and_totals():
    exported = generate_expense_report(0, [{
        "paid_on": TODAY.isoformat(),
        "registered_on": TODAY.isoformat(),
        "concept": "Mantenimiento",
        "invoice_number": "000012",
        "currency": "VES",
        "payment_method": BANKS[0],
        "amount": "116",
        "vat": "16.00",
    }], TODAY)
    with pymupdf.open(stream=exported, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "CONTROL DE GASTOS" in text
    assert TODAY.strftime("%d/%m/%Y") in text
    assert "Mantenimiento" in text
    assert "000012" in text
    assert "Fecha de factura / gasto" in text
    assert "Fecha de registro" in text
    expected_columns = [
        "Fecha de registro",
        "Fecha de factura / gasto",
        "Nro. de factura",
        "Concepto",
        "Base imponible",
        "IVA 16%",
        "Total pagado",
        "Tipo de moneda",
        "Banco / medio",
    ]
    assert [text.index(column) for column in expected_columns] == sorted(
        text.index(column) for column in expected_columns
    )
    assert "100,00" in text
    assert "16,00" in text
    assert "116,00" in text
    assert "VES 116,00" in text
    assert "Tasa BCV" not in text


def test_expense_pdf_labels_the_date_without_invoice_as_expense_date():
    exported = generate_expense_report(0, [{
        "paid_on": TODAY.isoformat(),
        "registered_on": TODAY.isoformat(),
        "concept": "Transporte",
        "invoice_number": None,
        "currency": "USD",
        "payment_method": "Zelle",
        "amount": "25",
        "vat": "0",
    }], TODAY)
    with pymupdf.open(stream=exported, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "Fecha de factura / gasto" in text
    assert "Fecha de registro" in text
    assert text.index("Fecha de registro") < text.index("Fecha de factura / gasto")
    assert "Transporte" in text


def test_expense_pdf_handles_empty_and_multiple_pages():
    empty_pdf = generate_expense_report(1, [], TODAY)
    with pymupdf.open(stream=empty_pdf, filetype="pdf") as document:
        assert "No hay gastos registrados" in document[0].get_text()
    expenses = [
        {
            "paid_on": TODAY.isoformat(),
            "registered_on": TODAY.isoformat(),
            "concept": f"Gasto recurrente número {index}",
            "invoice_number": None,
            "currency": "USD",
            "payment_method": "Zelle",
            "amount": "1",
            "vat": "0",
        }
        for index in range(100)
    ]
    report = generate_expense_report(1, expenses, TODAY)
    with pymupdf.open(stream=report, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        assert len(document) > 1
        assert "Gasto recurrente número 0" in text
        assert "Gasto recurrente número 99" in text
        assert "USD 100,00" in text


def test_legacy_expenses_migrate_without_fabricating_registration_dates(tmp_path):
        path = tmp_path / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE expenses (id INTEGER PRIMARY KEY, residence INTEGER NOT NULL, "
                "concept TEXT NOT NULL, paid_on TEXT NOT NULL, currency TEXT NOT NULL, "
                "payment_method TEXT NOT NULL, amount TEXT NOT NULL, invoice_number TEXT, "
                "vat TEXT NOT NULL, exchange_rate TEXT)"
            )
            connection.execute(
                "INSERT INTO expenses VALUES (1, 0, 'Compra antigua', ?, 'USD', 'Zelle', "
                "'10.00', NULL, '0', NULL)",
                (TODAY.isoformat(),),
            )
        migrated = ResidentStore(path)
        expense = migrated.expenses(0, TODAY)[0]
        assert expense["concept"] == "Compra antigua"
        assert expense["registered_on"] is None


@pytest.mark.parametrize("changes", [
    {"amount": Decimal("0")}, {"amount": Decimal("-1")},
    {"amount": Decimal("NaN")}, {"amount": Decimal("Infinity")},
    {"amount": Decimal("1.001")}, {"concept": " "},
    {"paid_on": TODAY + timedelta(days=1)}, {"currency": "EUR"},
    {"payment_method": "Zelle"}, {"residence": 2},
    {"with_invoice": True}, {"with_invoice": True, "invoice_number": "x" * 41},
    {"invoice_number": "123"},
])
def test_invalid_expenses_do_not_change_balances(store, changes):
    fund(store)
    kwargs = dict(
        residence=0, concept="Compra", paid_on=TODAY, currency="VES",
        payment_method=BANKS[0], amount=Decimal("100"),
    )
    kwargs.update(changes)
    with pytest.raises(ValueError):
        store.record_expense(**kwargs)
    assert store.expenses(0, TODAY) == []
    assert balance(store) == Decimal("1000")


def test_dollar_expenses_cannot_be_registered_with_invoice(store):
    fund(store, "USD", "Zelle")
    with pytest.raises(ValueError, match="no pueden registrarse con factura"):
        store.record_expense(
            0, "Compra", TODAY, "USD", "Zelle", Decimal("100"),
            with_invoice=True, invoice_number="123",
        )
    assert store.expenses(0, TODAY) == []
    assert balance(store, "USD", "Zelle") == Decimal("1000")


def test_insufficient_balance_and_backdated_expenses_are_rejected(store):
    yesterday = TODAY - timedelta(days=1)
    fund(store, day=TODAY)
    with pytest.raises(ValueError, match="Saldo insuficiente"):
        store.record_expense(
            0, "Compra", yesterday, "VES", BANKS[0], Decimal("100"),
        )
    store.record_expense(
        0, "Compra", TODAY, "VES", BANKS[0], Decimal("1000"),
    )
    with pytest.raises(ValueError, match="Saldo insuficiente"):
        store.record_expense(
            0, "Compra", TODAY, "VES", BANKS[0], Decimal("0.01"),
        )
    assert balance(store) == 0


def test_backdated_expense_cannot_overdraw_a_later_day(store):
    earlier = TODAY - timedelta(days=2)
    fund(store, day=earlier)
    store.record_expense(
        0, "Compra de hoy", TODAY, "VES", BANKS[0], Decimal("900"),
    )
    with pytest.raises(ValueError, match="fechas posteriores"):
        store.record_expense(
            0, "Compra anterior", earlier, "VES", BANKS[0], Decimal("101"),
        )
    assert balance(store, day=earlier) == Decimal("1000")
    assert balance(store) == Decimal("100")


def test_concurrent_expenses_cannot_spend_the_same_balance(store):
    fund(store, "USD", "Efectivo", "100")

    def spend(_):
        try:
            store.record_expense(0, "Compra", TODAY, "USD", "Efectivo", Decimal("75"))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(spend, range(2))) == [False, True]
    assert balance(store, "USD", "Efectivo") == Decimal("25")


def test_permissions_are_checked_before_any_expense_write(store):
    def deny():
        raise PermissionError("Solo consulta")

    store.authorize_write = deny
    with pytest.raises(PermissionError):
        store.record_expense(0, "Compra", TODAY, "USD", "Efectivo", Decimal("1"))
    assert store.expenses(0, TODAY) == []


def test_insert_failure_rolls_back_without_spending_balance(store):
    fund(store)
    with store._connect() as connection:
        connection.executescript(
            "CREATE TRIGGER fail_expense BEFORE INSERT ON expenses "
            "BEGIN SELECT RAISE(ABORT, 'Fallo de prueba'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_expense(
            0, "Compra", TODAY, "VES", BANKS[0], Decimal("100"),
        )
    assert balance(store) == Decimal("1000")
    assert store.expenses(0, TODAY) == []


def test_balances_include_collections_and_subtract_refunds(store):
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO residents (id, residence, identity, values_json, documents_json, pdf, "
            "admission_date, next_payment) VALUES (1, 0, 'TEST', '{}', '[]', ?, ?, ?)",
            (b"test", TODAY.isoformat(), TODAY.isoformat()),
        )
        connection.execute(
            "INSERT INTO payments (resident_id, due_date, paid_on, currency, amount, receiving_bank) "
            "VALUES (1, ?, ?, 'VES', '116.01', ?)",
            (TODAY.isoformat(), TODAY.isoformat(), BANKS[0]),
        )
        connection.execute(
            "INSERT INTO deposits (resident_id, paid_on, currency, amount, payment_method) "
            "VALUES (1, ?, 'USD', '100', 'Zelle')", (TODAY.isoformat(),),
        )
        connection.execute(
            "INSERT INTO service_payments (resident_id, concept, paid_on, currency, amount, "
            "payment_method, receipt_number, receipt_pdf) "
            "VALUES (1, 'Llaves', ?, 'USD', '20.05', 'Zelle', '1', ?)",
            (TODAY.isoformat(), b"test"),
        )
        connection.execute(
            "INSERT INTO deposit_refunds (resident_id, refunded_on, currency, deposit_amount, "
            "deduction, deduction_reason, amount, keys_returned, inspection_ok, payment_method, "
            "receipt_number, receipt_pdf) VALUES (1, ?, 'USD', '100', '20', 'Prueba', "
            "'80', 1, 1, 'Zelle', '2', ?)", (TODAY.isoformat(), b"test"),
        )
    assert balance(store) == Decimal("116.01")
    assert balance(store, "USD", "Zelle") == Decimal("40.05")
    with pytest.raises(ValueError, match="Saldo insuficiente"):
        store.record_expense(0, "Compra", TODAY, "USD", "Zelle", Decimal("40.06"))
    store.record_expense(0, "Compra", TODAY, "USD", "Zelle", Decimal("40.05"))
    assert balance(store, "USD", "Zelle") == 0


@pytest.mark.parametrize("invalid_amount", ["NaN", "Infinity", "incorrecto", "-10"])
def test_invalid_movements_block_spending_and_are_reported(store, invalid_amount):
    fund(store)
    with store._connect() as connection:
        connection.execute("UPDATE opening_balances SET amount = ?", (invalid_amount,))
    with pytest.raises(ValueError, match="importe no válido"):
        store.account_balances(0, TODAY)
    with pytest.raises(ValueError, match="importe no válido"):
        store.record_expense(
            0, "Compra", TODAY, "VES", BANKS[0], Decimal("1"),
        )
    assert not store.expenses(0, TODAY)


@pytest.fixture
def app_context(store):
    access = SimpleNamespace(role="administrator", can_write=True, require_write=lambda: None)
    with (
        patch("resident_store.ResidentStore", return_value=store),
        patch("portal_auth.require_login", return_value=access),
        patch("portal_auth.select_residence_area", return_value=(0, 1)),
        patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), TODAY)),
    ):
        yield access


def open_expenses(residence=0):
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
    app.button(key=f"residencia_action_{residence}_5").click().run()
    assert not app.exception
    return app


def test_ui_saves_invoice_with_included_vat_and_updates_balance(store, app_context):
    fund(store)
    app = open_expenses()
    assert not any(item.key == "expenses_0_rate" for item in app.text_input)
    assert not any("Tasa BCV" in item.value for item in app.markdown)
    app.checkbox(key="expenses_0_with_invoice").check().run()
    app.text_input(key="expenses_0_amount").set_value("116").run()
    assert any("16,00" in item.value and "100,00" in item.value for item in app.info)
    app.text_input(key="expenses_0_concept").set_value("Mantenimiento")
    app.text_input(key="expenses_0_invoice").set_value("000001")
    app.checkbox(key="expenses_0_confirmed").check()
    next(button for button in app.button if button.label == "Guardar gasto").click().run()
    assert not app.exception
    assert not app.error
    assert len(store.expenses(0, TODAY)) == 1
    assert balance(store) == Decimal("884")
    assert app.success
    assert not app.text_input(key="expenses_0_amount").value
    app.button(key="residencia_action_0_4").click().run()
    assert not app.exception
    assert not app.error


def test_ui_usd_without_invoice_and_readonly_access(store, app_context):
    fund(store, "USD", "Zelle", residence=1)
    app = open_expenses(1)
    app.selectbox(key="expenses_1_currency").select("USD").run()
    invoice_checkbox = app.checkbox(key="expenses_1_with_invoice")
    assert invoice_checkbox.proto.disabled
    assert not invoice_checkbox.value
    app.selectbox(key="expenses_1_USD_method").select("Zelle").run()
    app.text_input(key="expenses_1_amount").set_value("25").run()
    app.text_input(key="expenses_1_concept").set_value("Transporte")
    app.checkbox(key="expenses_1_confirmed").check()
    next(button for button in app.button if button.label == "Guardar gasto").click().run()
    assert not app.exception
    assert balance(store, "USD", "Zelle", residence=1) == Decimal("975")
    assert store.expenses(1, TODAY)[0]["vat"] == "0"
    app_context.can_write = False
    app_context.role = "viewer"
    app = open_expenses(1)
    assert not app.exception
    assert not any(button.label == "Guardar gasto" for button in app.button)
    assert any("Total pagado" in table.value for table in app.table)
    assert any(button.label == "Descargar control de gastos (PDF)" for button in app.download_button)


def test_switching_to_dollars_clears_invoice_selection(store, app_context):
    app = open_expenses()
    app.checkbox(key="expenses_0_with_invoice").check().run()
    app.selectbox(key="expenses_0_currency").select("USD").run()
    assert app.checkbox(key="expenses_0_with_invoice").proto.disabled
    assert not app.checkbox(key="expenses_0_with_invoice").value
    assert not any(item.key == "expenses_0_invoice" for item in app.text_input)


def test_ui_offers_empty_expense_download(store, app_context):
    app = open_expenses()
    assert not app.exception
    assert any(button.label == "Descargar control de gastos (PDF)" for button in app.download_button)


def test_ui_initial_balance_and_insufficient_funds(store, app_context):
    app = open_expenses()
    app.text_input(key="expenses_0_opening_amount").set_value("100")
    app.text_input(key="expenses_0_opening_description").set_value("Apertura")
    next(button for button in app.button if button.label == "Guardar saldo inicial").click().run()
    assert not app.exception
    assert balance(store) == Decimal("100")
    app.text_input(key="expenses_0_amount").set_value("101").run()
    app.text_input(key="expenses_0_concept").set_value("Compra")
    app.checkbox(key="expenses_0_confirmed").check()
    next(button for button in app.button if button.label == "Guardar gasto").click().run()
    assert not app.exception
    assert any("Saldo insuficiente" in error.value for error in app.error)
    assert not store.expenses(0, TODAY)


def test_ui_reports_invalid_balance_without_allowing_expenses(store, app_context):
    fund(store)
    with store._connect() as connection:
        connection.execute("UPDATE opening_balances SET amount = 'NaN'")
    app = open_expenses()
    assert any("No se pueden calcular los saldos" in error.value for error in app.error)
    assert not any(button.label == "Guardar gasto" for button in app.button)


def test_monthly_pdf_includes_available_balances():
    pdf = generate_monthly_report(
        0, TODAY.replace(day=1), [], [{"Concepto": "Cobros", "Total": "116,00"}],
        available_summary=[{
            "Banco / medio de pago": BANKS[0],
            "Moneda": "VES",
            "Saldo inicial / meses anteriores": "50,00",
            "Pagos recibidos en el mes": "100,00",
            "Gastos del mes": "50,00",
            "Saldo disponible al cierre": "100,00",
        }],
    )
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert "Saldos disponibles al cierre del informe" in text
    assert "100,00" in text
    assert "Saldo inicial / meses anteriores" in text
    assert "Saldo disponible al cierre" in text


def test_monthly_available_summary_adds_prior_openings_to_month_net():
    summary = calculate_monthly_available_summary(
        {
            bank: {
                "current": Decimal("100") if bank == BANKS[0] else Decimal("0"),
                "opening": Decimal("500") if bank == BANKS[0] else Decimal("0"),
                "opening_previous": Decimal("500") if bank == BANKS[0] else Decimal("0"),
            }
            for bank in BANKS
        },
        {
            method: {"current": Decimal("50") if method == "Zelle" else Decimal("0")}
            for method in ("Efectivo", "Zelle", "Binance")
        },
        [
            {
                "paid_on": TODAY.isoformat(), "currency": "VES",
                "payment_method": BANKS[0], "amount": "40",
            },
            {
                "paid_on": TODAY.isoformat(), "currency": "USD",
                "payment_method": "Zelle", "amount": "25",
            },
        ],
        [
            {"currency": "VES", "payment_method": BANKS[0], "amount": Decimal("500")},
            {"currency": "USD", "payment_method": "Zelle", "amount": Decimal("10")},
        ],
        TODAY.replace(day=1), TODAY,
    )
    by_account = {
        (row["Moneda"], row["Banco / medio de pago"]): row
        for row in summary
    }
    ves_row = by_account[("VES", BANKS[0])]
    assert ves_row["Saldo inicial / meses anteriores"] == "500,00"
    assert ves_row["Pagos recibidos en el mes"] == "100,00"
    assert ves_row["Gastos del mes"] == "40,00"
    assert ves_row["Saldo disponible al cierre"] == "560,00"
    usd_row = by_account[("USD", "Zelle")]
    assert usd_row["Saldo inicial / meses anteriores"] == "10,00"
    assert usd_row["Pagos recibidos en el mes"] == "50,00"
    assert usd_row["Gastos del mes"] == "25,00"
    assert usd_row["Saldo disponible al cierre"] == "35,00"


def test_monthly_available_summary_reports_inconsistent_negative_balance():
    with pytest.raises(ValueError, match="no cuadran"):
        calculate_monthly_available_summary(
            {BANKS[0]: {"current": Decimal("10")}}, {},
            [{
                "paid_on": TODAY.isoformat(), "currency": "VES",
                "payment_method": BANKS[0], "amount": "20",
            }],
            [], TODAY.replace(day=1), TODAY,
        )


@pytest.mark.parametrize("currency,method", [
    *[("VES", bank) for bank in BANKS],
    *[("USD", method) for method in ("Efectivo", "Binance", "Zelle")],
])
def test_monthly_closing_includes_current_month_opening(store, app_context, currency, method):
    fund(store, currency, method, amount="1000")
    store.record_expense(0, "Compra del mes", TODAY, currency, method, Decimal("116"))
    with patch(
        "monthly_report.generate_monthly_report", wraps=generate_monthly_report,
    ) as report:
        app = open_expenses()
        app.button(key="residencia_action_0_4").click().run()
        assert not app.exception
        assert not app.warning
        summary = next(
            table.value for table in app.table
            if "Saldo disponible al cierre" in table.value.columns
        )
        row = summary.loc[
            (summary["Moneda"] == currency)
            & (summary["Banco / medio de pago"] == method)
        ].iloc[0]
        assert row["Saldo inicial / meses anteriores"] == "1.000,00"
        assert row["Gastos del mes"] == "116,00"
        assert row["Saldo disponible al cierre"] == "884,00"
        call = report.call_args
        assert call.kwargs["available_summary"] == summary.to_dict("records")
        with pymupdf.open(
            stream=generate_monthly_report(*call.args, **call.kwargs), filetype="pdf",
        ) as document:
            assert "884,00" in "".join(page.get_text() for page in document)


def test_expense_control_current_balance_displays_debit_after_save(store, app_context):
    fund(store)
    app = open_expenses()
    app.text_input(key="expenses_0_amount").set_value("116")
    app.text_input(key="expenses_0_concept").set_value("Mantenimiento")
    app.checkbox(key="expenses_0_confirmed").check()
    next(button for button in app.button if button.label == "Guardar gasto").click().run()
    assert not app.exception
    assert balance(store) == Decimal("884")
    available = app.table[0].value
    assert available.loc[
        available["Banco / medio de pago"] == BANKS[0], "Saldo disponible"
    ].iloc[0] == "884,00"


@pytest.mark.parametrize("currency,method", [("VES", BANKS[0]), ("USD", "Efectivo")])
def test_grouped_money_input_preserves_value_and_formats_display(store, app_context, currency, method):
    fund(store, currency, method, amount="5000")
    app = open_expenses()
    app.selectbox(key="expenses_0_currency").select(currency).run()
    app.text_input(key="expenses_0_amount").set_value("1.234,56").run()
    assert app.text_input(key="expenses_0_amount").value == "1.234,56"
    app.text_input(key="expenses_0_concept").set_value("Compra de prueba")
    app.checkbox(key="expenses_0_confirmed").check()
    next(button for button in app.button if button.label == "Guardar gasto").click().run()
    assert not app.exception
    assert not app.error
    assert store.expenses(0, TODAY)[0]["amount"] == "1234.56"
    history = next(table.value for table in app.table if "Total pagado" in table.value)
    assert "1.234,56" in list(history["Total pagado"])


def test_monthly_report_carries_forward_prior_month_net_balance(store, app_context):
    previous_month = TODAY.replace(day=1) - timedelta(days=1)
    resident_id = store.add(
        0,
        {
            "student_name": "RESIDENTE DE PRUEBA",
            "student_id": "V-12345678",
            "room_number": "1",
            "bed_number": "1",
            "admission_date": TODAY.strftime("%d/%m/%Y"),
        },
        [False] * 8,
        b"%PDF-test",
        None,
    )
    two_months_ago = previous_month.replace(day=1) - timedelta(days=1)
    store.record_opening_balance(
        0, two_months_ago, "VES", BANKS[0], Decimal("500"), "Saldo inicial",
    )
    store.record_expense(
        0, "Gasto del mes anterior", previous_month, "VES", BANKS[0], Decimal("50"),
    )
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO payments (resident_id, due_date, paid_on, currency, amount, receiving_bank) "
            "VALUES (?, ?, ?, 'VES', '100', ?)",
            (resident_id, TODAY.isoformat(), TODAY.isoformat(), BANKS[0]),
        )
    store.record_expense(0, "Compra", TODAY, "VES", BANKS[0], Decimal("40"))

    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30,
    ).run()
    app.button(key="residencia_action_0_4").click().run()
    assert not app.exception
    assert not app.warning, [warning.value for warning in app.warning]
    summary_table = next(
        table.value for table in app.table
        if "Saldo disponible al cierre" in table.value.columns
    )
    available_row = summary_table.loc[
        (summary_table["Moneda"] == "VES")
        & (summary_table["Banco / medio de pago"] == BANKS[0])
    ].iloc[0]
    assert available_row["Saldo inicial / meses anteriores"] == "450,00"
    assert available_row["Pagos recibidos en el mes"] == "100,00"
    assert available_row["Gastos del mes"] == "40,00"
    assert available_row["Saldo disponible al cierre"] == "510,00"
