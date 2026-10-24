import json
from io import BytesIO
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
from streamlit.testing.v1 import AppTest
from monthly_report import generate_monthly_report

from deposit_receipt import (
    SERVICE_CONCEPTS, calculate_receipt_vat, format_bolivars,
    generate_deposit_receipt, generate_monthly_receipt, generate_refund_receipt,
)
from resident_store import (
    ResidentStore, bolivars_from_dollars, calculate_age,
    normalize_admission_values, payment_status,
)
from resident_format import format_resident_display_name
from payment_schedule import next_month_start


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def admission_values(admission="01/01/2026"):
    return {
        "student_name": "Residente Ficticia",
        "student_id": "V-12.345.678",
        "birth_date": "01/01/2005",
        "age": "21",
        "student_mobile": "0412-1234567",
        "student_landline": "",
        "emergency_phone": "0414-1234567",
        "email": "residente@example.com",
        "student_address": "Calle de ejemplo",
        "city": "Caracas",
        "state": "Distrito Capital",
        "mother_name": "Tutora Ficticia",
        "mother_phone": "",
        "mother_workplace": "",
        "mother_landline": "",
        "mother_address": "",
        "father_name": "",
        "father_phone": "",
        "father_workplace": "",
        "father_landline": "",
        "father_address": "",
        "emergency_contacts": "",
        "representative_id": "V-87.654.321",
        "admission_date": admission,
        "room_number": "1",
        "bed_number": "2",
    }


class StoreTests(unittest.TestCase):
    def test_service_payments_receipts_invoices_history_and_schedule(self):
        today = date.today()
        for residence in (0, 1):
            resident_id = self.add_resident(residence)
            due = self.store.list_residents(residence)[0].next_payment
            for concept in SERVICE_CONCEPTS:
                for currency in ("USD", "VES"):
                    kwargs = (
                        {"payment_method": "Zelle"} if currency == "USD" else {
                            "exchange_rate": Decimal("1000"),
                            "exchange_rate_date": today,
                            "amount_usd": Decimal("130.01"),
                            "receiving_bank": "Banco de Venezuela",
                        }
                    )
                    self.store.record_service_payment(
                        resident_id, residence, concept, today, currency,
                        Decimal("130.01") if currency == "USD" else Decimal("130010"),
                        invoice_number="00012-PRUEBA", **kwargs,
                    )
            self.assertEqual(self.store.list_residents(residence)[0].next_payment, due)
            self.assertEqual(self.store.payment_history(resident_id, residence), [])
            self.assertFalse(self.store.can_delete_admission(resident_id, residence))
            with self.assertRaises(ValueError):
                self.store.delete_admission(resident_id, residence)
            rows = ResidentStore(self.path).service_payment_history(resident_id, residence)
            self.assertEqual(len(rows), 6)
            self.assertEqual(len({r["receipt_number"] for r in rows}), 6)
            self.assertEqual(self.store.service_payment_history(resident_id, 1 - residence), [])
            for row in rows:
                with pymupdf.open(stream=row["receipt_pdf"], filetype="pdf") as document:
                    self.assertEqual(len(document), 1)
                    text = document[0].get_text()
                    self.assertEqual(text.count("RECIBO DE PAGO POR OTROS SERVICIOS"), 2)
                    self.assertEqual(text.count(row["concept"]), 2)
                    self.assertEqual(text.count("00012-PRUEBA"), 2)
                    self.assertEqual(text.count("NO ES VALIDO PARA FACTURA FISCAL"), 2)
                    self.assertNotIn("Días de retardo:", text)
                    if row["currency"] == "VES":
                        for amount in ("130.010,00", "20.801,60", "109.208,40"):
                            self.assertEqual(text.count(amount), 2)
                        self.assertEqual(text.count("IVA (16%):"), 2)
                    else:
                        self.assertNotIn("IVA", text)
                        self.assertIn("130,01", text)
                self.store.set_invoice_number(
                    residence, f"Otros servicios: {row['concept']}", row["id"], "00099",
                )
                with self.assertRaises(ValueError):
                    self.store.set_invoice_number(
                        1 - residence, f"Otros servicios: {row['concept']}", row["id"], "BAD",
                    )
            updated = self.store.service_payment_history(resident_id, residence)
            self.assertTrue(all(r["invoice_number"] == "00099" for r in updated))
            self.assertEqual(
                [r["receipt_pdf"] for r in rows], [r["receipt_pdf"] for r in updated],
            )
            received = self.store.received_payments(residence, today)
            self.assertEqual(len(received), 6)
            self.assertEqual(
                {r["kind"] for r in received}, {f"Otros servicios: {c}" for c in SERVICE_CONCEPTS},
            )
            self.store.discharge(resident_id, residence, today, "Fin")
            self.assertEqual(len(self.store.service_payment_history(resident_id, residence)), 6)
            self.assertEqual(len(self.store.received_payments(residence, today)), 6)
            with self.assertRaises(ValueError):
                self.store.record_service_payment(
                    resident_id, residence, "Llaves", today, "USD", Decimal("10"),
                    payment_method="Efectivo",
                )

    def test_service_payment_validation_and_receipt_failure_are_atomic(self):
        resident_id = self.add_resident()
        today = date.today()
        base = dict(
            resident_id=resident_id, residence=0, concept="Llaves",
            paid_on=today, currency="USD", amount=Decimal("10"), payment_method="Efectivo",
        )
        for invalid in (
            {"residence": 1}, {"concept": "Otro"}, {"paid_on": today + timedelta(days=1)},
            {"amount": Decimal("NaN")}, {"amount": Decimal("-1")}, {"amount": Decimal("0.001")},
            {"payment_method": None}, {"currency": "EUR"}, {"invoice_number": "X" * 41},
            {"exchange_rate": Decimal("10")}, {"receiving_bank": "Banco de Venezuela"},
            {"currency": "VES", "payment_method": None},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.record_service_payment(**{**base, **invalid})
        with patch("resident_store.generate_monthly_receipt", side_effect=ValueError("PDF")):
            with self.assertRaisesRegex(ValueError, "PDF"):
                self.store.record_service_payment(**base)
        self.assertEqual(self.store.service_payment_history(resident_id, 0), [])
        self.assertTrue(self.store.can_delete_admission(resident_id, 0))
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receipt_counters").fetchone()[0], 0)
        self.store.record_service_payment(**base)
        row = self.store.service_payment_history(resident_id, 0)[0]
        self.assertTrue(row["receipt_number"].startswith("00001-"))

    def test_opening_balances_are_separate_persistent_and_dated(self):
        self.store.record_opening_balance(
            0, date(2025, 12, 31), "USD", "Efectivo", Decimal("2000"), "Prueba ficticia",
        )
        self.store.record_opening_balance(
            0, date(2025, 12, 31), "USD", "Zelle", Decimal("1000"), "Prueba ficticia",
        )
        restored = ResidentStore(self.path)
        for through in (date(2025, 12, 31), date(2026, 1, 1), date(2026, 10, 19)):
            balances = restored.opening_balances(0, through)
            self.assertEqual(sum(Decimal(b["amount"]) for b in balances), Decimal("3000"))
        self.assertEqual({b["payment_method"] for b in balances}, {"Efectivo", "Zelle"})
        self.assertEqual(restored.opening_balances(1, date(2026, 1, 1)), [])
        self.assertEqual(restored.opening_balances(0, date(2025, 12, 30)), [])
        self.assertEqual(restored.received_payments(0, date.today()), [])
        self.assertEqual(restored.list_residents(0), [])
        with self.assertRaises(ValueError):
            restored.record_opening_balance(
                0, date.today() + timedelta(days=1), "USD", "Binance",
                Decimal("100"), "Futuro",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            restored.record_opening_balance(
                0, date(2025, 12, 31), "USD", "Zelle", Decimal("1000"), "Duplicado",
            )
        for currency, method, amount, description in (
            ("USD", "Banco de Venezuela", Decimal("100"), "Inválido"),
            ("VES", "Zelle", Decimal("100"), "Inválido"),
            ("USD", "Binance", Decimal("NaN"), "Inválido"),
            ("USD", "Binance", Decimal("-1"), "Inválido"),
            ("USD", "Binance", Decimal("0.001"), "Inválido"),
            ("USD", "Binance", Decimal("100"), ""),
        ):
            with self.subTest(amount=amount, method=method):
                with self.assertRaises(ValueError):
                    restored.record_opening_balance(
                        0, date(2025, 12, 31), currency, method, amount, description,
                    )

    def test_invoice_numbers_persist_per_payment_and_do_not_change_receipts(self):
        resident_id = self.add_resident()
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("200"))
        due = self.store.list_residents(0)[0].next_payment
        self.store.record_payment(
            resident_id, 0, due, date.today(), "USD", Decimal("150"), payment_method="Efectivo",
        )
        for payment in self.store.received_payments(0, date.today()):
            receipt = (
                self.store.get_deposit_receipt(resident_id, 0)
                if payment["kind"] == "Depósito de ingreso" else
                self.store.get_payment_receipt(resident_id, 0, due)
            )
            for residence, kind, record_id, number in (
                (1, payment["kind"], payment["record_id"], "001"),
                (0, "Inválido", payment["record_id"], "001"),
                (0, payment["kind"], 99999, "001"),
                (0, payment["kind"], payment["record_id"], "A" * 41),
                (0, payment["kind"], payment["record_id"], "A\nB"),
            ):
                with self.assertRaises(ValueError):
                    self.store.set_invoice_number(residence, kind, record_id, number)
            self.store.set_invoice_number(0, payment["kind"], payment["record_id"], " 00012-A ")
            restored = ResidentStore(self.path)
            updated = next(
                p for p in restored.received_payments(0, date.today()) if p["kind"] == payment["kind"]
            )
            self.assertEqual(updated["invoice_number"], "00012-A")
            after = (
                restored.get_deposit_receipt(resident_id, 0)
                if payment["kind"] == "Depósito de ingreso" else
                restored.get_payment_receipt(resident_id, 0, due)
            )
            self.assertEqual(receipt, after)
            restored.set_invoice_number(0, payment["kind"], payment["record_id"], "")
            self.assertIsNone(next(
                p for p in restored.received_payments(0, date.today()) if p["kind"] == payment["kind"]
            )["invoice_number"])

    def test_monthly_invoice_is_saved_atomically_and_printed_on_both_copies(self):
        for residence in (0, 1):
            for currency in ("USD", "VES"):
                with self.subTest(residence=residence, currency=currency):
                    self.values["student_id"] = f"INVOICE-{residence}-{currency}"
                    resident_id = self.add_resident(residence)
                    due = date(2026, 1, 1)
                    invoice = "000012-ABC" if currency == "USD" else "W" * 40
                    self.store.record_payment(
                        resident_id, residence, due, date.today(), currency,
                        Decimal("150") if currency == "USD" else Decimal("1500"),
                        payment_method="Zelle" if currency == "USD" else None,
                        exchange_rate=Decimal("10") if currency == "VES" else None,
                        exchange_rate_date=date.today() if currency == "VES" else None,
                        amount_usd=Decimal("150") if currency == "VES" else None,
                        receiving_bank="Fondo Común" if currency == "VES" else None,
                        invoice_number=f" {invoice} ",
                    )
                    restored = ResidentStore(self.path)
                    payment = next(
                        p for p in restored.received_payments(residence, date.today())
                        if p["student_id"] == self.values["student_id"]
                    )
                    self.assertEqual(payment["invoice_number"], invoice)
                    with pymupdf.open(
                        stream=restored.get_payment_receipt(resident_id, residence, due),
                        filetype="pdf",
                    ) as document:
                        page = document[0]
                        self.assertEqual(page.get_text().count("FACTURA Nro."), 2)
                        self.assertEqual(page.get_text().count(invoice), 2)
                        self.assertEqual(page.get_text().count(
                            restored.receipt_number(resident_id, residence, due)
                        ), 2)
                        invoice_spans = [
                            span
                            for block in page.get_text("dict")["blocks"]
                            for line in block.get("lines", [])
                            for span in line["spans"] if span["text"].strip() == invoice
                        ]
                        self.assertEqual(len(invoice_spans), 2)
                        self.assertTrue(all(
                            span["font"] == "Helvetica"
                            and span["bbox"][2] <= 580
                            and span["bbox"][3] < 101 + index * 396
                            for index, span in enumerate(invoice_spans)
                        ))

    def test_invalid_monthly_invoice_does_not_save_payment_or_advance_due(self):
        resident_id = self.add_resident()
        for invoice in ("A" * 41, "A\nB", "A\tB"):
            with self.subTest(invoice=invoice):
                with self.assertRaisesRegex(ValueError, "factura"):
                    self.store.record_payment(
                        resident_id, 0, date(2026, 1, 1), date.today(),
                        "USD", Decimal("100"), payment_method="Efectivo",
                        invoice_number=invoice,
                    )
                self.assertEqual(self.store.payment_history(resident_id, 0), [])
                self.assertEqual(
                    self.store.list_residents(0)[0].next_payment, date(2026, 1, 1)
                )

    def test_receipts_are_visually_identical_except_residence_name_and_rif(self):
        values = normalize_admission_values(admission_values())
        def receipts(residence):
            yield generate_deposit_receipt(
                1, residence, values, date(2026, 10, 19), Decimal("200"), "Efectivo",
                receipt_number="00001-2026",
            )
            for method in ("Efectivo", "Zelle", "Binance"):
                yield generate_monthly_receipt(
                    1, residence, values, date(2026, 10, 14), date(2026, 10, 19),
                    Decimal("160"), "USD", method, None, None,
                    monthly_amount_usd=Decimal("150"), late_fee_usd=Decimal("10"),
                    receipt_number="00001-2026",
                )
            for bank in ("Banco de Venezuela", "Banco del Tesoro", "Fondo Común"):
                yield generate_monthly_receipt(
                    1, residence, values, date(2026, 10, 14), date(2026, 10, 19),
                    Decimal("1600"), "VES", None, Decimal("10"), date(2026, 10, 19),
                    amount_usd=Decimal("160"), monthly_amount_usd=Decimal("150"),
                    late_fee_usd=Decimal("10"), receiving_bank=bank,
                    receipt_number="00001-2026",
                )
            for discount in (Decimal("0"), Decimal("10"), Decimal("200")):
                yield generate_refund_receipt(
                    residence, values, date(2026, 10, 19), Decimal("200"), discount,
                    "Llaves no entregadas" if discount else "",
                    not discount, True, "Binance", receipt_number="00001-2026",
                )
        for first, second in zip(receipts(0), receipts(1)):
            with pymupdf.open(stream=first, filetype="pdf") as a, pymupdf.open(
                stream=second, filetype="pdf"
            ) as b:
                for document in (a, b):
                    page = document[0]
                    self.assertEqual(len(document), 1)
                    self.assertEqual(page.rect, pymupdf.Rect(0, 0, 612, 792))
                    for offset in (0, 396):
                        page.add_redact_annot(
                            pymupdf.Rect(135, 5 + offset, 585, 39 + offset), fill=(1, 1, 1)
                        )
                    page.apply_redactions()
                self.assertEqual(a[0].get_text(), b[0].get_text())
                self.assertEqual(
                    a[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).samples,
                    b[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).samples,
                )

    def test_payment_receipt_fiscal_notice_is_small_centered_and_below_signatures(self):
        notice = "NO ES VALIDO PARA FACTURA FISCAL"
        values = admission_values()
        for residence in (0, 1):
            receipts = [
                generate_deposit_receipt(
                    1, residence, values, date.today(), Decimal("150"), "Zelle",
                    receipt_number="00001-2026",
                ),
                generate_monthly_receipt(
                    1, residence, values, date.today(), date.today(),
                    Decimal("150"), "USD", "Zelle", None, None,
                    receipt_number="00001-2026", invoice_number="00001-A",
                ),
                generate_monthly_receipt(
                    1, residence, values, date.today() - timedelta(days=5),
                    date.today(), Decimal("1600"), "VES", None, Decimal("10"),
                    date.today(), amount_usd=Decimal("160"),
                    monthly_amount_usd=Decimal("150"), late_fee_usd=Decimal("10"),
                    receiving_bank="Fondo Común", receipt_number="00001-2026",
                    invoice_number="00001-B",
                ),
            ]
            for pdf in receipts:
                with self.subTest(residence=residence), pymupdf.open(
                    stream=pdf, filetype="pdf",
                ) as document:
                    page = document[0]
                    spans = [
                        span for block in page.get_text("dict")["blocks"]
                        for line in block.get("lines", [])
                        for span in line["spans"]
                    ]
                    notices = [span for span in spans if span["text"] == notice]
                    self.assertEqual(len(notices), 2)
                    for index, span in enumerate(notices):
                        rect = pymupdf.Rect(span["bbox"])
                        self.assertEqual(span["size"], 7)
                        self.assertEqual(span["font"], "Helvetica")
                        self.assertAlmostEqual((rect.x0 + rect.x1) / 2, 306, places=2)
                        self.assertGreater(rect.y0, 370 + index * 396)
                        self.assertLess(rect.y1, 396 + index * 396)
                        self.assertTrue(all(
                            not rect.intersects(pymupdf.Rect(other["bbox"]))
                            for other in spans if other["text"] != notice
                        ))
            refund = generate_refund_receipt(
                residence, values, date.today(), Decimal("150"), Decimal("0"),
                "", True, True, "Zelle", receipt_number="00001-2026",
            )
            with pymupdf.open(stream=refund, filetype="pdf") as document:
                self.assertNotIn(notice, document[0].get_text())

    def test_monthly_bolivar_receipt_vat_is_sixteen_percent_of_received_total(self):
        for residence in (0, 1):
            for amount, subtotal, vat in (
                ("160000", "134.400,00", "25.600,00"),
                ("28384.16", "23.842,69", "4.541,47"),
                ("1160", "974,40", "185,60"),
                ("100.03", "84,03", "16,00"),
                ("0.03", "0,03", "0,00"),
                ("0.04", "0,03", "0,01"),
            ):
                with self.subTest(residence=residence, amount=amount):
                    pdf = generate_monthly_receipt(
                        1, residence, admission_values(), date.today() - timedelta(days=5),
                        date.today(), Decimal(amount), "VES", None, Decimal("10"),
                        date.today(), amount_usd=Decimal("160"),
                        monthly_amount_usd=Decimal("150"), late_fee_usd=Decimal("10"),
                        receiving_bank="Banco de Venezuela", receipt_number="00001-2026",
                        invoice_number="000001-A",
                    )
                    with pymupdf.open(stream=pdf, filetype="pdf") as document:
                        page = document[0]
                        for label, value in (
                            ("SUBTOTAL:", subtotal), ("IVA (16%):", vat),
                            ("TOTAL:", f"{Decimal(amount):,.2f}".translate(str.maketrans(",.", ".,"))),
                        ):
                            matches = page.search_for(f"{label} Bs. {value}")
                            if label == "TOTAL:":
                                matches = [rect for rect in matches if rect.x0 >= 400]
                            self.assertEqual(
                                len(matches), 2
                            )
                        spans = [
                            span for block in page.get_text("dict")["blocks"]
                            for line in block.get("lines", [])
                            for span in line["spans"]
                        ]
                        tax_spans = [
                            span for span in spans
                            if any(label in span["text"] for label in ("SUBTOTAL:", "IVA (16%):"))
                        ]
                        self.assertTrue(all(
                            not pymupdf.Rect(tax["bbox"]).intersects(pymupdf.Rect(other["bbox"]))
                            for tax in tax_spans for other in spans if tax is not other
                        ))

    def test_vat_does_not_change_received_amounts_or_other_receipt_types(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        self.store.record_payment(
            resident_id, 0, due, date.today(), "VES", Decimal("160000"),
            exchange_rate=Decimal("1000"), exchange_rate_date=date.today(),
            receiving_bank="Banco del Tesoro",
        )
        self.assertEqual(self.store.payment_history(resident_id, 0)[0]["amount"], "160000")
        self.assertEqual(self.store.received_payments(0, date.today())[0]["amount"], "160000")
        with pymupdf.open(
            stream=self.store.get_payment_receipt(resident_id, 0, due), filetype="pdf",
        ) as document:
            self.assertEqual(len(document[0].search_for("IVA (16%): Bs. 25.600,00")), 2)
        receipts = [
            generate_deposit_receipt(
                1, 0, admission_values(), date.today(), Decimal("150"), "Zelle",
                receipt_number="00001-2026",
            ),
            generate_monthly_receipt(
                1, 0, admission_values(), date.today(), date.today(),
                Decimal("150"), "USD", "Zelle", None, None, receipt_number="00001-2026",
            ),
            generate_refund_receipt(
                0, admission_values(), date.today(), Decimal("150"), Decimal("0"),
                "", True, True, "Zelle", receipt_number="00001-2026",
            ),
        ]
        for pdf in receipts:
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                self.assertNotIn("IVA (16%)", document[0].get_text())
                self.assertNotIn("SUBTOTAL", document[0].get_text())

    def test_bolivar_receipt_amounts_use_thousands_points_and_decimal_comma(self):
        for amount, formatted in (
            ("28384.16", "28.384,16"), ("999.99", "999,99"),
            ("1000", "1.000,00"), ("1234567.89", "1.234.567,89"),
        ):
            for residence in (0, 1):
                with self.subTest(amount=amount, residence=residence):
                    pdf = generate_monthly_receipt(
                        1, residence, admission_values(), date.today() - timedelta(days=1),
                        date.today(), Decimal(amount), "VES", None, Decimal("881.0664"),
                        date.today(), receipt_number="00001-2026",
                    )
                    with pymupdf.open(stream=pdf, filetype="pdf") as document:
                        page = document[0]
                        self.assertEqual(
                            len(page.search_for(f"TOTAL: Bs. {formatted}")), 2
                        )
                        self.assertEqual(
                            len(page.search_for(f"Mensualidad: Bs. {formatted}")), 2
                        )
                        self.assertEqual(
                            len(page.search_for("Tasa BCV: Bs. 881,0664 por USD.")), 2
                        )

    def test_monthly_reports_are_identical_except_residence_identity(self):
        entries = [{
            "Residente": "ESTUDIANTE DE PRUEBA", "Bolívares": "1500,00",
            "Banco": "Fondo Común", "Dólares": "200,00",
        }]
        summary = [{"Resumen": "Total recibido en el mes", "Bs.": "1500,00", "USD": "200,00"}]
        pdfs = [
            generate_monthly_report(residence, date(2026, 10, 19), entries, summary)
            for residence in (0, 1)
        ]
        with pymupdf.open(stream=pdfs[0], filetype="pdf") as a, pymupdf.open(
            stream=pdfs[1], filetype="pdf"
        ) as b:
            for document in (a, b):
                document[0].add_redact_annot(pymupdf.Rect(20, 10, 400, 56), fill=(1, 1, 1))
                document[0].apply_redactions()
            self.assertEqual(a[0].get_text(), b[0].get_text())
            self.assertEqual(a[0].get_pixmap().samples, b[0].get_pixmap().samples)

    def test_monthly_report_uses_uniform_thin_table_lines(self):
        entries = [{
            "Fecha": f"{day:02d}/10/2026", "Residente": f"ESTUDIANTE PRUEBA {day}",
            "Cédula de identidad": f"V-1234567{day}",
            "Habitación": f"HAB-{day}", "Cama": f"CAMA-{day}",
            "Tipo de pago": "Mensualidad", "N.º de factura": f"FACT-{day}",
            "Bolívares (Bs.)": "1.234,00", "IVA (16%) (Bs.)": "170,21",
            "Tasa BCV (Bs./USD)": "450,1234", "Fecha de la tasa": "20/10/2026",
            "Banco receptor": "Banco de Venezuela", "Dólares (USD)": "—",
            "Forma de pago USD": "—",
        } for day in range(18, 21)]
        pdf = generate_monthly_report(0, date.today(), entries, [{"Total": "1"}])
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            page = document[0]
            text = " ".join(page.get_text().split())
            for heading in (
                "Fecha", "Residente", "Bolívares (Bs.)", "IVA (16%) (Bs.)",
                "Tasa BCV (Bs./USD)", "Fecha de la tasa",
            ):
                self.assertIn(" ".join(heading.split()), text)
            self.assertIn("Cédula de Identidad", text)
            self.assertNotIn("CÉDULA DE IDENTIDAD", text)
            for value in ("HAB-18", "CAMA-18", "FACT-18"):
                text_rect = page.search_for(value)[0]
                self.assertLess(
                    abs(text_rect.x0 + text_rect.width / 2 - 396),
                    396,
                )
            vertical_grid_lines = [
                drawing["rect"] for drawing in page.get_drawings()
                if 0 < drawing["rect"].width <= 0.6
                and drawing["rect"].height > 15
                and drawing["rect"].y0 < 150
            ]
            self.assertTrue(vertical_grid_lines)
            self.assertTrue(all(line.width == 0.5 for line in vertical_grid_lines))
            column_edges = sorted({
                round(line.x0, 2)
                for line in vertical_grid_lines
                if line.y0 < 121
            })
            resident_identity_boundary = column_edges[2]
            dividing_segments = sorted(
                (
                    line for line in vertical_grid_lines
                    if abs(line.x0 - resident_identity_boundary) < 0.1
                ),
                key=lambda line: line.y0,
            )
            self.assertTrue(dividing_segments)
            self.assertLessEqual(dividing_segments[0].y0, 120)
            self.assertGreaterEqual(dividing_segments[-1].y1, 150)
            for previous, following in zip(dividing_segments, dividing_segments[1:]):
                self.assertLessEqual(following.y0, previous.y1 + 0.1)
            for value, column_index in (
                ("HAB-18", 3), ("CAMA-18", 4), ("FACT-18", 6),
            ):
                cell = page.search_for(value)[0]
                column_center = (column_edges[column_index] + column_edges[column_index + 1]) / 2
                text_center = (cell.x0 + cell.x1) / 2
                self.assertAlmostEqual(text_center, column_center, delta=1)

    def test_monthly_report_resident_name_uses_given_and_surname_initials(self):
        self.assertEqual(
            format_resident_display_name("MARÍA CAROLINA PÉREZ GÓMEZ"),
            "MARÍA C.\nPÉREZ G.",
        )
        self.assertEqual(
            format_resident_display_name("MARÍA CAROLINA PÉREZ"),
            "MARÍA C.\nPÉREZ",
        )
        self.assertEqual(
            format_resident_display_name("MARÍA PÉREZ"),
            "MARÍA\nPÉREZ",
        )

    def test_discharge_history_lists_newest_admission_first_for_same_student(self):
        ids = []
        for _ in range(3):
            resident_id = self.add_resident()
            self.store.discharge(resident_id, 0, date.today(), "Prueba")
            ids.append(resident_id)
        self.assertEqual(
            [r.id for r in self.store.list_residents(0, inactive=True)], ids[::-1]
        )

    def test_monthly_report_pdf_printable_multipage_and_exact_totals(self):
        entries = [{
            "Fecha": "19/10/2026", "Residente": f"ESTUDIANTE PRUEBA {index}",
            "Cédula": f"V-{index}", "Habitación": "1", "Cama": "2",
            "Tipo": "Mensualidad", "Bolívares": "1500,00",
            "Banco": "Banco de Venezuela", "Dólares": "—", "Forma USD": "—",
        } for index in range(65)]
        summary = [{
            "Resumen": "Total recibido en el mes", "Bolívares": "97500,00", "Dólares": "0,00",
        }]
        for residence in (0, 1):
            pdf = generate_monthly_report(
                residence, date(2026, 10, 19), entries, summary, "Datos anteriores incompletos."
            )
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                self.assertGreater(len(document), 1)
                text = " ".join(page.get_text() for page in document)
                for index in range(65):
                    self.assertIn(f"ESTUDIANTE PRUEBA {index}", text)
                self.assertIn("97500,00", text)
                self.assertIn("Datos anteriores incompletos.", text)
                self.assertEqual(text.count("FIRMA DEL ADMINISTRADOR"), 1)
                self.assertIn("FIRMA DEL ADMINISTRADOR", document[-1].get_text())
                for index, page in enumerate(document):
                    self.assertIn(
                        f"Fecha de impresión: {date.today():%d/%m/%Y}", page.get_text()
                    )
                    self.assertEqual(page.rect, pymupdf.Rect(0, 0, 792, 612))
                    self.assertIn(f"Página {index + 1} de {len(document)}", page.get_text())
                    self.assertIn("504912700" if residence == 0 else "50793608", page.get_text())
        empty = generate_monthly_report(0, date.today(), [], summary)
        with pymupdf.open(stream=empty, filetype="pdf") as document:
            self.assertIn("No hay pagos recibidos", document[0].get_text())
            self.assertIn("FIRMA DEL ADMINISTRADOR", document[-1].get_text())

    def test_monthly_report_keeps_rows_headers_and_signature_inside_printable_area(self):
        entries = [{
            "Fecha": "19/10/2026",
            "Residente": f"INICIO{index:03d} " + "NOMBRE LARGO DE PRUEBA " * 4,
            "Cédula de identidad": f"FICT-{index:03d}",
            "Habitación": "1", "Cama": "2", "Tipo de pago": "Mensualidad",
            "N.º de factura": "F" * 40,
            "Bolívares (Bs.)": "156020,00", "Banco receptor": "Banco de Venezuela",
            "Dólares (USD)": "—", "Forma de pago USD": f"FIN{index:03d}",
        } for index in range(35)]
        summary = [{"Resumen de cobros": "TOTALCOMPLETO", "Bs.": "5460700,00", "USD": "0,00"}]
        bank_summary = [
            {"Banco receptor": bank, "Acumulado al cierre (Bs.)": "1820233,33"}
            for bank in ("Banco de Venezuela", "Banco del Tesoro", "Fondo Común")
        ]
        for residence in (0, 1):
            pdf = generate_monthly_report(
                residence, date.today(), entries, summary, bank_summary=bank_summary,
            )
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                texts = [page.get_text() for page in document]
                self.assertGreater(len(document), 1)
                for index in range(35):
                    starts = [i for i, text in enumerate(texts) if f"INICIO{index:03d}" in text]
                    ends = [i for i, text in enumerate(texts) if f"FIN{index:03d}" in text]
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(starts, ends, "Una fila se dividió entre hojas.")
                    self.assertIn("Residente", texts[starts[0]])
                    self.assertIn("Banco receptor", " ".join(texts[starts[0]].split()))
                printable = pymupdf.Rect(23.9, 23.9, 768.1, 588.1)
                for page in document:
                    for word in page.get_text("words"):
                        self.assertTrue(printable.contains(pymupdf.Rect(word[:4])), word)
                    for drawing in page.get_drawings():
                        self.assertTrue(printable.contains(drawing["rect"]), drawing["rect"])
                signature = document[-1].search_for("FIRMA DEL ADMINISTRADOR")
                self.assertEqual(len(signature), 1)
                self.assertGreater(signature[0].y0, 548)
                self.assertLess(signature[0].y1, 576)
                for page in document[:-1]:
                    self.assertNotIn("FIRMA DEL ADMINISTRADOR", page.get_text())
                for page in document:
                    if "Totales y acumulados" in page.get_text():
                        self.assertIn("TOTALCOMPLETO", page.get_text())
                    if "Resumen de Bolivares en Bancos" in page.get_text():
                        self.assertIn("1820233,33", page.get_text())

    def test_monthly_report_moves_summary_heading_with_its_first_row(self):
        entries = [{"Residente": f"FILA {i:03d}", "Monto": "200,00"} for i in range(20)]
        summary = [{"Resumen": "UNICO RESUMEN", "Monto": "4000,00"}]
        pdf = generate_monthly_report(0, date.today(), entries, summary)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            for page in document:
                if "Totales y acumulados" in page.get_text():
                    self.assertIn("UNICO RESUMEN", page.get_text())

    def test_monthly_report_rejects_row_taller_than_printable_page(self):
        with self.assertRaisesRegex(ValueError, "fila.*no cabe completa"):
            generate_monthly_report(
                0, date.today(), [{"Residente": "DATO LARGO " * 3000}],
                [{"Resumen": "Total", "Monto": "200,00"}],
            )

    def test_receiving_bank_validation_and_all_supported_banks_on_receipt(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        for bank in (None, "", "Otro banco"):
            with self.subTest(bank=bank):
                with self.assertRaisesRegex(ValueError, "banco"):
                    self.store.record_payment(
                        resident_id, 0, due, date.today(), "VES", Decimal("1500"),
                        exchange_rate=Decimal("10"), exchange_rate_date=date.today(),
                        receiving_bank=bank,
                    )
                self.assertEqual(self.store.payment_history(resident_id, 0), [])
        with self.assertRaisesRegex(ValueError, "banco"):
            self.store.record_payment(
                resident_id, 0, due, date.today(), "USD", Decimal("150"),
                payment_method="Zelle", receiving_bank="Banco de Venezuela",
            )
        for bank in ("Banco de Venezuela", "Banco del Tesoro", "Fondo Común"):
            self.store.record_payment(
                resident_id, 0, due, date.today(), "VES", Decimal("1500"),
                exchange_rate=Decimal("10"), exchange_rate_date=date.today(),
                amount_usd=Decimal("150"), receiving_bank=bank,
            )
            self.assertEqual(self.store.payment_history(resident_id, 0)[-1]["receiving_bank"], bank)
            pdf = self.store.get_payment_receipt(resident_id, 0, due)
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                self.assertEqual(len(document[0].search_for(f"Banco receptor: {bank}")), 2)
                self.assertEqual(document[0].rect, pymupdf.Rect(0, 0, 612, 792))
                spans = [
                    span for block in document[0].get_text("dict")["blocks"] if "lines" in block
                    for line in block["lines"] for span in line["spans"]
                ]
                for offset in (0, 396):
                    bank_spans = [s for s in spans if bank in s["text"] and offset <= s["bbox"][1] < 396 + offset]
                    self.assertTrue(bank_spans)
                    self.assertLess(max(s["bbox"][3] for s in bank_spans), 355 + offset)
            due = next_month_start(due)
        bolivar_payment = next(
            payment for payment in self.store.received_payments(0, date.today())
            if payment["currency"] == "VES"
        )
        self.assertEqual(bolivar_payment["exchange_rate"], "10")
        self.assertEqual(bolivar_payment["exchange_rate_date"], date.today().isoformat())

    def test_received_payments_month_boundaries_year_rollover_and_residence(self):
        resident_id = self.add_resident()
        other = self.add_resident(1)
        self.store.record_deposit(resident_id, 0, date(2025, 12, 31), "Zelle", Decimal("50"))
        due = self.store.list_residents(0)[0].next_payment
        for paid_on, amount in (
            (date(2025, 12, 31), "10.10"), (date(2026, 1, 1), "20.20"),
            (date(2026, 1, 31), "30.30"), (date(2026, 2, 1), "40.40"),
        ):
            self.store.record_payment(
                resident_id, 0, due, paid_on, "USD", Decimal(amount), payment_method="Efectivo",
            )
            due = next_month_start(due)
        self.store.record_payment(
            other, 1, date(2026, 1, 1), date(2026, 1, 1), "USD",
            Decimal("999"), payment_method="Binance",
        )
        self.store.discharge(
            resident_id, 0, date.today(), "Fin", keys_returned=True, inspection_ok=True,
            refund_method="Zelle", refund_confirmed=True,
        )
        january = self.store.received_payments(0, date(2026, 1, 15))
        self.assertEqual(len(january), 4)
        self.assertEqual(sum(Decimal(p["amount"]) for p in january), Decimal("110.60"))
        self.assertEqual(sum(
            Decimal(p["amount"]) for p in january if p["paid_on"] < "2026-01-01"
        ), Decimal("60.10"))
        february = self.store.received_payments(0, date(2026, 2, 1))
        self.assertEqual(len(february), 5)
        self.assertEqual(sum(
            Decimal(p["amount"]) for p in february if p["paid_on"] < "2026-02-01"
        ), Decimal("110.60"))
        self.assertTrue(all(p["student_id"] == "V- 12.345.678" for p in february))
        self.assertEqual(len(self.store.received_payments(1, date(2026, 1, 1))), 1)
        self.assertTrue(all(p["room_number"] == "1" and p["bed_number"] == "2" for p in january))
        self.assertEqual(
            {p["kind"] for p in january}, {"Mensualidad", "Depósito de ingreso"}
        )

    def test_residence_legal_name_and_rif_in_all_receipts(self):
        values = normalize_admission_values(admission_values())
        for residence in (0, 1):
            receipts = [
                generate_deposit_receipt(
                    1, residence, values, date.today(), Decimal("150"), "Zelle",
                    receipt_number="00001-2026",
                ),
                generate_monthly_receipt(
                    1, residence, values, date.today(), date.today(),
                    Decimal("150"), "USD", "Zelle", None, None,
                    receipt_number="00001-2026",
                ),
                generate_refund_receipt(
                    residence, values, date.today(), Decimal("150"), Decimal("0"),
                    "", True, True, "Zelle", receipt_number="00001-2026",
                ),
            ]
            for pdf in receipts:
                with pymupdf.open(stream=pdf, filetype="pdf") as document:
                    self.assertEqual(len(document), 1)
                    page = document[0]
                    name = "PENSIÓN QUINTA CARMEN LUISA, C.A"
                    if residence == 0:
                        self.assertEqual(page.get_text().count(name), 2)
                        self.assertEqual(page.get_text().count("RIF: 504912700"), 2)
                        self.assertNotIn("RESIDENCIA CARMEN LUISA", page.get_text())
                        for name_rect, rif_rect in zip(
                            page.search_for(name), page.search_for("RIF: 504912700")
                        ):
                            self.assertLess(name_rect.y1, rif_rect.y0)
                    else:
                        acacias_name = "RESIDENCIAS LAS ACACIAS, C.A"
                        self.assertEqual(page.get_text().count(acacias_name), 2)
                        self.assertEqual(page.get_text().count("RIF: 50793608"), 2)
                        self.assertNotIn("RESIDENCIA LAS ACACIAS", page.get_text())
                        for name_rect, rif_rect in zip(
                            page.search_for(acacias_name), page.search_for("RIF: 50793608")
                        ):
                            self.assertLess(name_rect.y1, rif_rect.y0)
                        self.assertNotIn("504912700", page.get_text())
                        self.assertNotIn(name, page.get_text())

    def test_late_payment_manual_fee_usd_and_ves_receipt_and_persistence(self):
        for currency in ("USD", "VES"):
            self.values["student_id"] = f"V-LATE-{currency}"
            resident_id = self.add_resident()
            due = date(2026, 1, 1)
            paid_on = date(2026, 1, 8)
            self.store.record_payment(
                resident_id, 0, due, paid_on, currency,
                Decimal("160.50") if currency == "USD" else Decimal("1621.05"),
                payment_method="Zelle" if currency == "USD" else None,
                exchange_rate=Decimal("10.1") if currency == "VES" else None,
                exchange_rate_date=paid_on if currency == "VES" else None,
                receiving_bank="Fondo Común" if currency == "VES" else None,
                amount_usd=Decimal("160.50") if currency == "VES" else None,
                monthly_amount_usd=Decimal("150"), late_fee_usd=Decimal("10.50"),
            )
            restored = ResidentStore(self.path)
            history = restored.payment_history(resident_id, 0)[0]
            self.assertEqual(history["late_days"], "3")
            self.assertEqual(history["late_fee_usd"], "10.50")
            self.assertEqual(history["monthly_amount_usd"], "150")
            pdf = restored.get_payment_receipt(resident_id, 0, due)
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                self.assertEqual(len(document), 1)
                page = document[0]
                self.assertEqual(page.rect, pymupdf.Rect(0, 0, 612, 792))
                for expected in (
                    "Mensualidad: USD 150,00", "Días de retardo: 3", "Mora: USD 10,50",
                    "TOTAL: USD 160,50" if currency == "USD" else "TOTAL: Bs. 1.621,05",
                ):
                    self.assertEqual(len(page.search_for(expected)), 2, expected)
                if currency == "VES":
                    self.assertEqual(len(page.search_for("Banco receptor: Fondo Común")), 2)
                    self.assertEqual(len(page.search_for("Monto base: USD 160,50")), 2)
                    self.assertEqual(len(page.search_for("Tasa BCV: Bs. 10,1 por USD.")), 2)

    def test_manual_fee_validation_and_on_time_days(self):
        resident_id = self.add_resident()
        due = date(2026, 1, 1)
        for fee, total, paid_on in [
            (Decimal("-1"), Decimal("149"), date(2026, 2, 1)),
            (Decimal("NaN"), Decimal("150"), date(2026, 2, 1)),
            (Decimal("Infinity"), Decimal("150"), date(2026, 2, 1)),
            (Decimal("0.001"), Decimal("150"), date(2026, 2, 1)),
            (Decimal("10"), Decimal("160"), due),
            (Decimal("10"), Decimal("160"), due - timedelta(days=1)),
            (Decimal("10"), Decimal("150"), date(2026, 2, 1)),
        ]:
            with self.subTest(fee=fee, paid_on=paid_on):
                with self.assertRaises(ValueError):
                    self.store.record_payment(
                        resident_id, 0, due, paid_on, "USD", total,
                        payment_method="Zelle", monthly_amount_usd=Decimal("150"),
                        late_fee_usd=fee,
                    )
                self.assertEqual(self.store.list_residents(0)[0].next_payment, due)
                self.assertEqual(self.store.payment_history(resident_id, 0), [])
        self.store.record_payment(
            resident_id, 0, due, due, "USD", Decimal("150"), payment_method="Zelle",
            monthly_amount_usd=Decimal("150"),
        )
        self.assertEqual(self.store.payment_history(resident_id, 0)[0]["late_days"], "0")

    def test_concurrent_refunds_are_unique_and_duplicate_discharge_is_rejected(self):
        ids = []
        for index in range(3):
            self.values["student_id"] = f"V-CONCURRENT-REFUND-{index}"
            resident_id = self.add_resident()
            self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
            ids.append(resident_id)

        def discharge(resident_id):
            self.store.discharge(
                resident_id, 0, date.today(), "Fin", keys_returned=True,
                inspection_ok=True, refund_method="Zelle", refund_confirmed=True,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(discharge, ids))
        self.assertEqual(
            sorted(self.store.deposit_refund(i, 0)["receipt_number"] for i in ids),
            [f"{number:05d}-{date.today().year}" for number in range(1, 4)],
        )
        with self.assertRaises(ValueError):
            discharge(ids[0])
        self.assertEqual(self.store.list_residents(0), [])

    def test_multiline_refund_reason_fits_and_overflow_rolls_back(self):
        resident_id = self.add_resident(1)
        self.store.record_deposit(resident_id, 1, date.today(), "Zelle", Decimal("150"))
        kwargs = dict(
            keys_returned=False, inspection_ok=False, deduction=Decimal("25"),
            refund_method="Zelle", refund_confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "no cabe"):
            self.store.discharge(
                resident_id, 1, date.today(), "Fin",
                deduction_reason="Llaves no entregadas. " * 30, **kwargs,
            )
        self.assertIsNone(self.store.deposit_refund(resident_id, 1))
        self.assertEqual(len(self.store.list_residents(1)), 1)
        reason = (
            "Llaves no entregadas. Descuento indicado por la administración para reemplazar "
            "el juego de llaves y reparar la puerta de la habitación."
        )
        self.store.discharge(
            resident_id, 1, date.today(), "Fin", deduction_reason=reason, **kwargs
        )
        refund = self.store.deposit_refund(resident_id, 1)
        self.assertEqual(refund["receipt_number"], f"00001-{date.today().year}")
        with pymupdf.open(stream=refund["receipt_pdf"], filetype="pdf") as document:
            spans = [
                span for block in document[0].get_text("dict")["blocks"] if "lines" in block
                for line in block["lines"] for span in line["spans"]
            ]
            for offset in (0, 396):
                reason_spans = [
                    span for span in spans
                    if 305 + offset <= span["bbox"][1] <= 340 + offset
                    and span["text"] != "DESCUENTO POR:"
                ]
                self.assertEqual(len(reason_spans), 2)
                self.assertLess(max(span["bbox"][3] for span in reason_spans), 345 + offset)
                self.assertTrue(all(not span["flags"] & 16 for span in reason_spans))

    def test_refund_full_discounted_and_zero_keep_original_deposit(self):
        for index, (residence, discount, keys, inspection, method) in enumerate([
            (0, "0", True, True, "Efectivo"),
            (1, "25.25", False, False, "Binance"),
            (0, "150.50", True, False, "Zelle"),
        ]):
            with self.subTest(discount=discount):
                self.values["student_id"] = f"V-REFUND-{index}"
                resident_id = self.add_resident(residence)
                self.store.record_deposit(
                    resident_id, residence, date.today(), "Zelle", Decimal("150.50")
                )
                original = self.store.deposit_details(resident_id, residence)
                due = next(
                    r.next_payment for r in self.store.list_residents(residence)
                    if r.id == resident_id
                )
                self.store.discharge(
                    resident_id, residence, date.today(), "Fin de estadía",
                    keys_returned=keys, inspection_ok=inspection,
                    deduction=Decimal(discount),
                    deduction_reason="Llaves y reparación de puerta" if Decimal(discount) else "",
                    refund_method=method, refund_confirmed=True,
                )
                restored = ResidentStore(self.path)
                refund = restored.deposit_refund(resident_id, residence)
                self.assertEqual(refund["amount"], str(Decimal("150.50") - Decimal(discount)))
                self.assertEqual(refund["keys_returned"], keys)
                self.assertEqual(refund["inspection_ok"], inspection)
                self.assertEqual(refund["payment_method"], method)
                self.assertEqual(restored.deposit_details(resident_id, residence), original)
                self.assertIsNone(restored.deposit_refund(resident_id, 1 - residence))
                former = next(
                    r for r in restored.list_residents(residence, inactive=True)
                    if r.id == resident_id
                )
                self.assertEqual(former.next_payment, due)
                with pymupdf.open(stream=refund["receipt_pdf"], filetype="pdf") as document:
                    self.assertEqual(len(document), 1)
                    page = document[0]
                    self.assertEqual(page.rect, pymupdf.Rect(0, 0, 612, 792))
                    self.assertEqual(len(page.search_for("CONSTANCIA DE ENTREGA DE DEPOSITO")), 2)
                    self.assertNotIn("RECIBO DE ENTREGA DE DEPOSITO", page.get_text())
                    self.assertEqual(len(page.search_for("POR CONCEPTO DE: ENTREGA DE DEPOSITO")), 2)
                    self.assertEqual(len(page.search_for(refund["receipt_number"])), 2)
                    self.assertEqual(len(page.search_for("RESIDENTE FICTICIA")), 2)
                    self.assertNotIn("TUTORA FICTICIA", page.get_text())
                    if inspection:
                        self.assertEqual(len(page.search_for("SIN NOVEDAD")), 2)
                        self.assertNotIn("Sin novedad", page.get_text())
                    if Decimal(discount):
                        self.assertEqual(len(page.search_for("Llaves y reparación de puerta")), 2)
                    spans = [
                        span for block in page.get_text("dict")["blocks"] if "lines" in block
                        for line in block["lines"] for span in line["spans"]
                    ]
                    for text in ("DEPÓSITO ORIGINAL:", "DESCUENTO:", "LLAVES ENTREGADAS:", "REVISIÓN:"):
                        self.assertTrue(all(
                            span["flags"] & 16 for span in spans if span["text"] == text
                        ))
                    self.assertTrue(all(
                        not span["flags"] & 16
                        for span in spans if span["text"] == "RESIDENTE FICTICIA"
                    ))
                with self.assertRaises(ValueError):
                    restored.discharge(resident_id, residence, date.today(), "Duplicado")
                self.assertEqual(restored.deposit_refund(resident_id, residence), refund)

    def test_refund_validation_does_not_discharge_or_save(self):
        resident_id = self.add_resident()
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        valid = dict(
            keys_returned=True, inspection_ok=True, refund_method="Efectivo",
            refund_confirmed=True,
        )
        invalid = [
            {"keys_returned": None}, {"inspection_ok": None},
            {"keys_returned": False}, {"inspection_ok": False},
            {"deduction": Decimal("25")}, {"refund_method": None},
            {"refund_confirmed": False}, {"deduction": Decimal("-1")},
            {"deduction": Decimal("151")}, {"deduction": Decimal("0.001")},
            {"deduction": Decimal("NaN")}, {"deduction": Decimal("Infinity")},
            {"deduction_reason": "Sin monto"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.store.discharge(
                        resident_id, 0, date.today(), "Fin", **(valid | changes)
                    )
                self.assertEqual(len(self.store.list_residents(0)), 1)
                self.assertIsNone(self.store.deposit_refund(resident_id, 0))
        with self.assertRaises(ValueError):
            self.store.discharge(resident_id, 1, date.today(), "Otra residencia", **valid)
        with self.assertRaises(ValueError):
            self.store.discharge(
                resident_id, 0, date.today() + timedelta(days=1), "Futuro", **valid
            )

    def test_failed_refund_pdf_and_insert_roll_back_baja_and_number(self):
        resident_id = self.add_resident()
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        kwargs = dict(
            keys_returned=True, inspection_ok=True, refund_method="Zelle",
            refund_confirmed=True,
        )
        with patch("resident_store.generate_refund_receipt", side_effect=ValueError("PDF")):
            with self.assertRaisesRegex(ValueError, "PDF"):
                self.store.discharge(resident_id, 0, date.today(), "Fin", **kwargs)
        self.assertEqual(len(self.store.list_residents(0)), 1)
        self.assertIsNone(self.store.deposit_refund(resident_id, 0))
        with self.store._connect() as connection:
            connection.execute(
                "CREATE TRIGGER fail_refund BEFORE INSERT ON deposit_refunds "
                "BEGIN SELECT RAISE(ABORT, 'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.discharge(resident_id, 0, date.today(), "Fin", **kwargs)
        self.assertEqual(len(self.store.list_residents(0)), 1)
        self.assertIsNone(self.store.deposit_refund(resident_id, 0))
        with self.store._connect() as connection:
            connection.execute("DROP TRIGGER fail_refund")
        self.store.discharge(resident_id, 0, date.today(), "Fin", **kwargs)
        self.assertEqual(
            self.store.deposit_refund(resident_id, 0)["receipt_number"],
            f"00001-{date.today().year}",
        )

    def test_refund_series_restart_and_existing_counter_migration(self):
        with self.store._connect() as connection:
            connection.execute("DROP TABLE receipt_counters")
            connection.execute(
                "CREATE TABLE receipt_counters (residence INTEGER NOT NULL, "
                "kind TEXT NOT NULL CHECK (kind IN ('deposit', 'monthly')), "
                "year INTEGER NOT NULL, last_number INTEGER NOT NULL, "
                "PRIMARY KEY(residence, kind, year))"
            )
            connection.execute("INSERT INTO receipt_counters VALUES (0, 'deposit', 2026, 7)")
        self.store = ResidentStore(self.path)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT last_number FROM receipt_counters WHERE kind = 'deposit'"
            ).fetchone()[0], 7)
        for index, (residence, year, expected) in enumerate([
            (0, 2026, "00001-2026"), (0, 2026, "00002-2026"),
            (1, 2026, "00001-2026"), (0, 2027, "00001-2027"),
        ]):
            self.values["student_id"] = f"V-YEAR-{index}"
            resident_id = self.add_resident(residence)
            with patch("resident_store.date", wraps=date) as clock:
                clock.today.return_value = date(year, 12, 31) if year == 2026 else date(year, 1, 1)
                self.store.record_deposit(resident_id, residence, date(2026, 1, 1), "Zelle", Decimal("150"))
                self.store.discharge(
                    resident_id, residence, date(2026, 12, 31), "Fin",
                    keys_returned=True, inspection_ok=True, refund_method="Zelle",
                    refund_confirmed=True,
                )
            self.assertEqual(self.store.deposit_refund(resident_id, residence)["receipt_number"], expected)

    def test_no_deposit_has_no_refund_and_incomplete_deposit_blocks_discharge(self):
        resident_id = self.add_resident()
        self.store.discharge(resident_id, 0, date.today(), "Sin depósito")
        self.assertIsNone(self.store.deposit_refund(resident_id, 0))
        new_id = self.add_resident()
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO deposits (resident_id, paid_on) VALUES (?, ?)",
                (new_id, date.today().isoformat()),
            )
        with self.assertRaisesRegex(ValueError, "completos"):
            self.store.discharge(new_id, 0, date.today(), "Fin")
        self.assertEqual(len(self.store.list_residents(0)), 1)

    def test_concurrent_deposits_receive_distinct_consecutive_numbers(self):
        ids = []
        for index in range(4):
            self.values["student_id"] = f"V-CONCURRENT-{index}"
            ids.append(self.add_resident())

        def save(resident_id):
            self.store.record_deposit(
                resident_id, 0, date.today(), "Zelle", Decimal("150")
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(save, ids))
        self.assertEqual(
            sorted(self.store.receipt_number(resident_id, 0) for resident_id in ids),
            [f"{number:05d}-{date.today().year}" for number in range(1, 5)],
        )

    def test_annual_receipt_sequences_are_separate_and_restart_on_january_first(self):
        first = self.add_resident()
        other_residence = self.add_resident(1)
        self.values["student_id"] = "V-NEW"
        second = self.add_resident()
        with patch("resident_store.date", wraps=date) as clock:
            clock.today.return_value = date(2026, 12, 31)
            self.store.record_deposit(first, 0, date(2026, 12, 31), "Zelle", Decimal("150"))
            self.store.record_deposit(second, 0, date(2026, 12, 31), "Zelle", Decimal("150"))
            self.store.record_deposit(
                other_residence, 1, date(2026, 12, 31), "Zelle", Decimal("150")
            )
            due = self.store.list_residents(0)[0].next_payment
            self.store.record_payment(
                first, 0, due, date(2026, 12, 31), "USD", Decimal("100"),
                payment_method="Efectivo",
            )
            self.assertEqual(self.store.receipt_number(first, 0), "00001-2026")
            self.assertEqual(self.store.receipt_number(second, 0), "00002-2026")
            self.assertEqual(self.store.receipt_number(other_residence, 1), "00001-2026")
            self.assertEqual(self.store.receipt_number(first, 0, due), "00001-2026")
            clock.today.return_value = date(2027, 1, 1)
            next_due = next_month_start(due)
            self.store.record_payment(
                first, 0, next_due, date(2026, 12, 31), "USD", Decimal("100"),
                payment_method="Efectivo",
            )
            self.values["student_id"] = "V-YEAR-2027"
            third = self.add_resident()
            self.store.record_deposit(third, 0, date(2026, 12, 31), "Zelle", Decimal("150"))
            self.assertEqual(self.store.receipt_number(third, 0), "00001-2027")
            self.assertEqual(self.store.receipt_number(first, 0, next_due), "00001-2027")
            self.store.regenerate_deposit_receipt(first, 0)
            self.assertEqual(self.store.receipt_number(first, 0), "00001-2026")
        self.assertEqual(ResidentStore(self.path).receipt_number(first, 0), "00001-2026")

    def test_failed_receipt_does_not_consume_number(self):
        resident_id = self.add_resident()
        with patch("resident_store.generate_deposit_receipt", side_effect=ValueError("test")):
            with self.assertRaises(ValueError):
                self.store.record_deposit(
                    resident_id, 0, date.today(), "Zelle", Decimal("150")
                )
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        self.assertEqual(
            self.store.receipt_number(resident_id, 0), f"00001-{date.today().year}"
        )

    def test_legacy_receipt_upgrade_is_idempotent_and_preserves_payment_data(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        self.store.record_deposit(resident_id, 0, date.today(), "Binance", Decimal("150"))
        self.store.record_payment(
            resident_id, 0, due, date.today(), "USD", Decimal("100"),
            payment_method="Zelle",
        )
        deposit = self.store.deposit_details(resident_id, 0)
        history = self.store.payment_history(resident_id, 0)
        next_due = self.store.list_residents(0)[0].next_payment
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE deposits SET receipt_number = NULL")
            connection.execute("UPDATE payments SET receipt_number = NULL")
            connection.execute("DELETE FROM receipt_counters")
        self.assertEqual(self.store.upgrade_legacy_receipts(), 2)
        self.assertEqual(self.store.upgrade_legacy_receipts(), 0)
        self.assertEqual(self.store.deposit_details(resident_id, 0), deposit)
        self.assertEqual(self.store.payment_history(resident_id, 0), history)
        self.assertEqual(self.store.list_residents(0)[0].next_payment, next_due)
        self.assertEqual(
            self.store.receipt_number(resident_id, 0), f"00001-{date.today().year}"
        )

    def test_receipt_labels_are_bold_and_inserted_values_are_regular(self):
        values = normalize_admission_values(admission_values())
        receipts = [
            generate_deposit_receipt(
                1, 0, values, date(2026, 10, 19), Decimal("150.50"), "Zelle",
                receipt_number="00001-2026",
            ),
            generate_monthly_receipt(
                1, 0, values, date(2026, 10, 19), date(2026, 10, 19),
                Decimal("1500"), "VES", None, Decimal("10"),
                date(2026, 10, 19), amount_usd=Decimal("150"),
                receipt_number="00001-2026",
            ),
        ]
        labels = {
            "RECIBO Nro.", "FECHA:", "NOMBRES Y APELLIDOS:",
            "CÉDULA DE IDENTIDAD:", "HE RECIBIDO LA CANTIDAD DE (EN LETRAS):",
            "POR CONCEPTO DE:", "FORMA DE PAGO:", "MONEDA:", "TOTAL:", "SUBTOTAL:", "IVA (16%):",
            "RECIBIDO CONFORME", "ENTREGA CONFORME",
            "Monto base:", "Tasa BCV:", "Fecha de la tasa:", "RIF:",
            "Mensualidad:", "Días de retardo:", "Mora:",
        }
        for pdf in receipts:
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                spans = [
                    span for block in document[0].get_text("dict")["blocks"]
                    if "lines" in block
                    for line in block["lines"] for span in line["spans"]
                ]
                for span in spans:
                    text = span["text"].strip()
                    expected_bold = (
                        text in labels or text.startswith("RESIDENCIA ")
                        or text == "RESIDENCIAS LAS ACACIAS, C.A"
                        or text == "PENSIÓN QUINTA CARMEN LUISA, C.A"
                        or text.startswith("RECIBO DE PAGO DE ")
                        or text == "COPIA PARA EL ARCHIVO DE LA RESIDENCIA"
                    )
                    self.assertEqual(
                        bool(span["flags"] & 16), expected_bold, text
                    )
                self.assertEqual(
                    sum(s["text"].strip() == "NOMBRES Y APELLIDOS:" for s in spans), 2
                )

    def test_dollar_conversion_rounds_half_up_and_rejects_invalid_values(self):
        self.assertEqual(
            bolivars_from_dollars(Decimal("150"), Decimal("881.0664")),
            Decimal("132159.96"),
        )
        self.assertEqual(
            bolivars_from_dollars(Decimal("1"), Decimal("1.005")), Decimal("1.01")
        )
        for amount, rate in (
            ("0", "10"), ("-1", "10"), ("NaN", "10"),
            ("10", "0"), ("10", "Infinity"), ("1.001", "10"),
        ):
            with self.subTest(amount=amount, rate=rate):
                with self.assertRaises(ValueError):
                    bolivars_from_dollars(Decimal(amount), Decimal(rate))

    def test_converted_payment_rejects_inconsistent_total(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        with self.assertRaisesRegex(ValueError, "no coincide"):
            self.store.record_payment(
                resident_id, 0, due, date.today(), "VES", Decimal("999"),
                receiving_bank="Banco de Venezuela",
                exchange_rate=Decimal("10"), exchange_rate_date=date.today(),
                amount_usd=Decimal("150"),
            )
        self.assertEqual(self.store.payment_history(resident_id, 0), [])
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)

    def test_age_uses_birthday_and_years_suffix(self):
        self.assertEqual(calculate_age(date(1978, 11, 20), date(2026, 10, 19)), "47 AÑOS")
        self.assertEqual(calculate_age(date(1978, 11, 20), date(2026, 11, 20)), "48 AÑOS")
        self.assertEqual(calculate_age(date(2000, 2, 29), date(2025, 2, 28)), "24 AÑOS")
        self.assertEqual(calculate_age(date(2000, 2, 29), date(2025, 3, 1)), "25 AÑOS")
        with self.assertRaises(ValueError):
            calculate_age(date(2027, 1, 1), date(2026, 1, 1))

    def test_admission_values_format_identity_and_venezuelan_phones(self):
        values = normalize_admission_values({
            "student_id": "14648043",
            "representative_id": "E-14648043",
            "student_mobile": "0414 824 8527",
            "emergency_phone": "4148248527",
            "mother_phone": "+58 414 8248527",
            "emergency_contacts": "Tía: 0212 1234567",
        })
        self.assertEqual(values["student_id"], "V- 14.648.043")
        self.assertEqual(values["representative_id"], "E-14.648.043")
        self.assertEqual(values["student_mobile"], "0414-8248527")
        self.assertEqual(values["emergency_phone"], "0414-8248527")
        self.assertEqual(values["mother_phone"], "0414-8248527")
        self.assertEqual(values["emergency_contacts"], "TÍA: 0212-1234567")
        self.assertEqual(
            normalize_admission_values({"student_id": "9134677"})["student_id"],
            "V- 09.134.677",
        )

    def test_receipt_formats_unprefixed_identity(self):
        values = admission_values()
        values["student_id"] = "14648043"
        pdf = generate_deposit_receipt(
            1, 0, values, date.today(), Decimal("150"), "Efectivo",
            receipt_number="00001-2026",
        )
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            self.assertEqual(document[0].get_text().count("V- 14.648.043"), 2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "test.sqlite3"
        self.store = ResidentStore(self.path)
        self.values = admission_values()

    def add_resident(self, residence=0):
        return self.store.add(
            residence, self.values, [True, False] * 4, b"%PDF-test", b"photo"
        )

    def test_data_and_pdf_survive_new_store_instance(self):
        resident_id = self.add_resident()
        restored = ResidentStore(self.path)
        resident = restored.list_residents(0)[0]
        self.assertEqual(resident.values, normalize_admission_values(self.values))
        self.assertEqual(resident.documents, [True, False] * 4)
        self.assertEqual(resident.next_payment, date(2026, 1, 1))
        self.assertEqual(restored.get_pdf(resident_id, 0), b"%PDF-test")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_first_monthly_payment_is_due_on_admission_and_advances_to_next_month(self):
        for residence in (0, 1):
            resident_id = self.add_resident(residence)
            due = self.store.list_residents(residence)[0].next_payment
            self.assertEqual(due, date(2026, 1, 1))
            self.store.record_payment(
                resident_id, residence, due, due, "USD", Decimal("100"),
                payment_method="Efectivo",
            )
            restored = ResidentStore(self.path)
            self.assertEqual(
                restored.list_residents(residence)[0].next_payment,
                next_month_start(due),
            )

    def test_existing_due_dates_are_not_rewritten_on_reopening_store(self):
        resident_id = self.add_resident()
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE residents SET next_payment = ? WHERE id = ?",
                ("2026-01-31", resident_id),
            )
        restored = ResidentStore(self.path)
        self.assertEqual(restored.list_residents(0)[0].next_payment, date(2026, 1, 31))

    def test_delete_unpaid_admission_is_persistent_and_isolated(self):
        resident_id = self.add_resident()
        other_id = self.add_resident(1)
        self.assertTrue(self.store.can_delete_admission(resident_id, 0))
        self.assertFalse(self.store.can_delete_admission(resident_id, 1))
        with self.assertRaisesRegex(ValueError, "No se puede eliminar"):
            self.store.delete_admission(resident_id, 1)
        self.store.delete_admission(resident_id, 0)
        restored = ResidentStore(self.path)
        self.assertEqual(restored.list_residents(0), [])
        self.assertEqual(restored.list_residents(1)[0].id, other_id)
        with self.assertRaisesRegex(ValueError, "No se encontró"):
            restored.get_pdf(resident_id, 0)
        with restored._connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT photo FROM residents WHERE id = ?", (resident_id,)
            ).fetchone())
        self.add_resident()

    def test_delete_admission_rechecks_payments_and_deposits_in_storage(self):
        for kind in ("deposit", "monthly", "legacy_deposit", "legacy_monthly", "inactive"):
            with self.subTest(kind=kind):
                self.values["student_id"] = f"TEST-{kind}"
                resident_id = self.add_resident()
                self.assertTrue(self.store.can_delete_admission(resident_id, 0))
                if kind == "deposit":
                    self.store.record_deposit(
                        resident_id, 0, date.today(), "Zelle", Decimal("150"),
                    )
                elif kind == "monthly":
                    self.store.record_payment(
                        resident_id, 0, date(2026, 1, 1), date.today(),
                        "USD", Decimal("100"), payment_method="Efectivo",
                    )
                elif kind == "inactive":
                    self.store.discharge(resident_id, 0, date.today(), "Prueba")
                else:
                    with self.store._connect() as connection:
                        if kind == "legacy_deposit":
                            connection.execute(
                                "INSERT INTO deposits (resident_id, paid_on) VALUES (?, ?)",
                                (resident_id, date.today().isoformat()),
                            )
                        else:
                            connection.execute(
                                "INSERT INTO payments (resident_id, due_date, paid_on) "
                                "VALUES (?, ?, ?)",
                                (resident_id, "2026-01-01", date.today().isoformat()),
                            )
                self.assertFalse(self.store.can_delete_admission(resident_id, 0))
                with self.assertRaisesRegex(ValueError, "No se puede eliminar"):
                    self.store.delete_admission(resident_id, 0)
                self.assertEqual(self.store.get_pdf(resident_id, 0), b"%PDF-test")

    def test_existing_payment_table_is_migrated(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                "CREATE TABLE payments (id INTEGER PRIMARY KEY, resident_id INTEGER "
                "NOT NULL, due_date TEXT NOT NULL, paid_on TEXT NOT NULL)"
            )
        ResidentStore(legacy_path)
        with sqlite3.connect(legacy_path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payments)")
            }
        self.assertTrue(
            {
                "currency", "amount", "payment_method", "exchange_rate",
                "exchange_rate_date",
            }
            <= columns
        )

    def test_unique_deposit_preserves_monthly_due_and_survives_discharge(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        paid_on = date.today() - timedelta(days=1)
        self.store.record_deposit(resident_id, 0, paid_on, "Binance", Decimal("150.50"))
        restored = ResidentStore(self.path)
        self.assertEqual(restored.deposit_date(resident_id, 0), paid_on)
        self.assertEqual(
            restored.deposit_details(resident_id, 0),
            {
                "paid_on": paid_on.isoformat(),
                "currency": "USD",
                "amount": "150.50",
                "payment_method": "Binance",
            },
        )
        self.assertEqual(restored.list_residents(0)[0].next_payment, due)
        self.assertEqual(restored.payment_history(resident_id, 0), [])
        with self.assertRaisesRegex(ValueError, "ya está registrado"):
            restored.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        self.assertEqual(restored.deposit_date(resident_id, 0), paid_on)
        self.assertIsNone(restored.deposit_date(resident_id, 1))
        restored.discharge(
            resident_id, 0, date.today(), "Fin de estadía",
            keys_returned=True, inspection_ok=True, refund_method="Binance",
            refund_confirmed=True,
        )
        self.assertEqual(restored.deposit_date(resident_id, 0), paid_on)
        new_id = self.add_resident()
        self.assertIsNone(restored.deposit_date(new_id, 0))

    def test_deposit_rejects_other_residence_future_date_and_discharged_resident(self):
        resident_id = self.add_resident()
        with self.assertRaisesRegex(ValueError, "residente activa"):
            self.store.record_deposit(resident_id, 1, date.today(), "Efectivo", Decimal("150"))
        with self.assertRaisesRegex(ValueError, "futura"):
            self.store.record_deposit(
                resident_id, 0, date.today() + timedelta(days=1), "Efectivo", Decimal("150")
            )
        self.store.discharge(resident_id, 0, date.today(), "Fin de estadía")
        with self.assertRaisesRegex(ValueError, "residente activa"):
            self.store.record_deposit(resident_id, 0, date.today(), "Efectivo", Decimal("150"))
        self.assertIsNone(self.store.deposit_date(resident_id, 0))

    def test_deposit_payment_methods_and_invalid_method(self):
        resident_id = self.add_resident()
        with self.assertRaisesRegex(ValueError, "Selecciona"):
            self.store.record_deposit(resident_id, 0, date.today(), "", Decimal("150"))
        self.assertIsNone(self.store.deposit_date(resident_id, 0))
        for index, method in enumerate(("Efectivo", "Binance", "Zelle")):
            with self.subTest(method=method):
                self.values["student_id"] = f"V-TEST-{index}"
                new_id = self.add_resident()
                self.store.record_deposit(new_id, 0, date.today(), method, Decimal("150"))
                self.assertEqual(
                    ResidentStore(self.path).deposit_details(new_id, 0),
                    {
                        "paid_on": date.today().isoformat(),
                        "currency": "USD",
                        "amount": "150",
                        "payment_method": method,
                    },
                )

    def test_legacy_deposit_migration_preserves_date(self):
        resident_id = self.add_resident()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE deposits")
            connection.execute(
                "CREATE TABLE deposits (resident_id INTEGER PRIMARY KEY, paid_on TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO deposits VALUES (?, ?)", (resident_id, "2026-01-01")
            )
        restored = ResidentStore(self.path)
        self.assertEqual(
            restored.deposit_details(resident_id, 0),
            {
                "paid_on": "2026-01-01", "currency": None, "amount": None,
                "payment_method": None,
            },
        )

    def test_deposit_rejects_invalid_amount_without_changing_due(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        for amount in ("", "abc", "0", "-1", "NaN", "Infinity"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    self.store.record_deposit(
                        resident_id, 0, date.today(), "Efectivo", amount
                    )
                self.assertIsNone(self.store.deposit_date(resident_id, 0))
                self.assertEqual(self.store.list_residents(0)[0].next_payment, due)

    def test_deposit_receipt_uses_student_data_and_survives_discharge(self):
        for residence, label in ((0, "CARMEN LUISA"), (1, "LAS ACACIAS")):
            with self.subTest(residence=residence):
                resident_id = self.add_resident(residence)
                self.store.record_deposit(
                    resident_id, residence, date.today(), "Zelle", Decimal("150.50")
                )
                restored = ResidentStore(self.path)
                pdf = restored.get_deposit_receipt(resident_id, residence)
                self.assertTrue(pdf.startswith(b"%PDF"))
                with pymupdf.open(stream=pdf, filetype="pdf") as document:
                    text = "".join(page.get_text() for page in document)
                    self.assertIn("RESIDENTE FICTICIA", text)
                    self.assertIn("V- 12.345.678", text)
                    self.assertNotIn("TUTORA FICTICIA", text)
                    self.assertIn("USD 150,50", text)
                    self.assertIn("CIENTO CINCUENTA DÓLARES CON 50/100", text)
                    self.assertIn("FORMA DE PAGO: ZELLE", text)
                    self.assertIn(date.today().strftime("%d/%m/%Y"), text)
                    number = restored.receipt_number(resident_id, residence)
                    self.assertIn(number, text)
                    self.assertEqual(len(document[0].search_for("RECIBO DE PAGO DE DEPOSITO")), 2)
                    self.assertIn(label, text.upper())
                    self.assertEqual(len(document), 1)
                    page = document[0]
                    self.assertEqual(page.rect, pymupdf.Rect(0, 0, 612, 792))
                    self.assertEqual(len(page.search_for("RESIDENTE FICTICIA")), 2)
                    self.assertEqual(len(page.search_for(number)), 2)
                    self.assertEqual(len(page.search_for("USD 150,50")), 2)
                    self.assertNotIn("COPIA PARA LA ESTUDIANTE", text)
                    self.assertIn("COPIA PARA EL ARCHIVO DE LA RESIDENCIA", text)
                    self.assertNotIn("RECORTAR", text)
                    cut_area = pymupdf.Rect(290, 388, 322, 404)
                    scissors = [
                        drawing for drawing in page.get_drawings()
                        if cut_area.contains(drawing["rect"])
                    ]
                    self.assertEqual(len(scissors), 4)
                    self.assertEqual(
                        sum(
                            any(item[0] == "c" for item in drawing["items"])
                            for drawing in scissors
                        ),
                        2,
                    )
                    names = page.search_for("RESIDENTE FICTICIA")
                    self.assertLess(names[0].y1, 396)
                    self.assertGreater(names[1].y0, 396)
                    self.assertTrue(
                        any(
                            drawing["dashes"] != "[] 0"
                            and abs(drawing["rect"].y0 - 396) < 1
                            for drawing in page.get_drawings()
                        )
                    )
                self.assertIsNone(restored.get_deposit_receipt(resident_id, 1 - residence))
                restored.discharge(
                    resident_id, residence, date.today(), "Fin",
                    keys_returned=True, inspection_ok=True, refund_method="Zelle",
                    refund_confirmed=True,
                )
                self.assertEqual(restored.get_deposit_receipt(resident_id, residence), pdf)

    def test_receipt_failure_rolls_back_deposit(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        with patch(
            "resident_store.generate_deposit_receipt",
            side_effect=ValueError("Error de prueba al generar recibo"),
        ):
            with self.assertRaisesRegex(ValueError, "generar recibo"):
                self.store.record_deposit(
                    resident_id, 0, date.today(), "Efectivo", Decimal("150")
                )
        self.assertIsNone(self.store.deposit_details(resident_id, 0))
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)

    def test_regenerate_receipt_preserves_deposit_and_requires_active_resident(self):
        resident_id = self.add_resident()
        self.store.record_deposit(
            resident_id, 0, date.today(), "Binance", Decimal("150.50")
        )
        original = self.store.deposit_details(resident_id, 0)
        due = self.store.list_residents(0)[0].next_payment
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE deposits SET receipt_pdf = NULL WHERE resident_id = ?",
                (resident_id,),
            )
        self.store.regenerate_deposit_receipt(resident_id, 0)
        self.assertTrue(self.store.get_deposit_receipt(resident_id, 0).startswith(b"%PDF"))
        self.assertEqual(self.store.deposit_details(resident_id, 0), original)
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)
        self.assertEqual(self.store.payment_history(resident_id, 0), [])
        with self.assertRaisesRegex(ValueError, "activa"):
            self.store.regenerate_deposit_receipt(resident_id, 1)
        self.store.discharge(
            resident_id, 0, date.today(), "Fin",
            keys_returned=True, inspection_ok=True, refund_method="Binance",
            refund_confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "activa"):
            self.store.regenerate_deposit_receipt(resident_id, 0)

    def test_regenerate_incomplete_legacy_deposit_reports_missing_data(self):
        resident_id = self.add_resident()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO deposits (resident_id, paid_on) VALUES (?, ?)",
                (resident_id, date.today().isoformat()),
            )
        with self.assertRaisesRegex(ValueError, "completos"):
            self.store.regenerate_deposit_receipt(resident_id, 0)
        self.assertIsNone(self.store.get_deposit_receipt(resident_id, 0))

    def test_cycle_uses_calendar_month_even_for_admission_at_end_of_month(self):
        self.values["admission_date"] = "31/01/2026"
        resident_id = self.add_resident()
        self.assertEqual(self.store.list_residents(0)[0].next_payment, date(2026, 1, 31))
        self.store.record_payment(
            resident_id, 0, date(2026, 1, 31), date(2026, 2, 13), "USD",
            amount=Decimal("125.50"), payment_method="Binance",
        )
        self.assertEqual(self.store.list_residents(0)[0].next_payment, date(2026, 2, 1))
        self.assertEqual(self.store.payment_history(resident_id, 0), [
            {
                "due_date": "2026-01-31", "paid_on": "2026-02-13",
                "currency": "USD", "payment_method": "Binance",
                "amount": "125.50",
                "amount_usd": None,
                "monthly_amount_usd": "125.50", "late_fee_usd": "0", "late_days": "13",
                "exchange_rate": None, "exchange_rate_date": None,
                "receiving_bank": None,
            }
        ])

    def test_monthly_receipts_preserve_each_payment_and_student_data(self):
        for residence in (0, 1):
            for currency, method in (
                ("USD", "Efectivo"), ("USD", "Binance"), ("USD", "Zelle"), ("VES", None),
            ):
                with self.subTest(residence=residence, currency=currency, method=method):
                    self.values["student_id"] = f"V-{residence}-{currency}-{method}"
                    resident_id = self.add_resident(residence)
                    for due in (date(2026, 1, 1), date(2026, 2, 1)):
                        self.store.record_payment(
                            resident_id, residence, due, date(2026, 2, 1),
                            currency, Decimal("150.50"), payment_method=method,
                            exchange_rate=Decimal("881.0664") if currency == "VES" else None,
                            exchange_rate_date=date(2026, 2, 1) if currency == "VES" else None,
                            receiving_bank="Banco del Tesoro" if currency == "VES" else None,
                        )
                        restored = ResidentStore(self.path)
                        pdf = restored.get_payment_receipt(resident_id, residence, due)
                        self.assertTrue(pdf.startswith(b"%PDF"))
                        with pymupdf.open(stream=pdf, filetype="pdf") as document:
                            self.assertEqual(len(document), 1)
                            page = document[0]
                            text = page.get_text()
                            self.assertEqual(page.rect, pymupdf.Rect(0, 0, 612, 792))
                            self.assertEqual(len(page.search_for("RESIDENTE FICTICIA")), 2)
                            self.assertIn(self.values["student_id"].upper(), text)
                            self.assertNotIn("TUTORA FICTICIA", text)
                            self.assertNotIn("reembolsado", text)
                            self.assertEqual(
                                len(page.search_for("Pago de Mensualidad de Habitación")), 2
                            )
                            self.assertIn(restored.receipt_number(resident_id, residence, due), text)
                            self.assertEqual(
                                len(page.search_for("RECIBO DE PAGO DE MENSUALIDAD")), 2
                            )
                            self.assertNotIn("VENCIMIENTO CUBIERTO", text)
                            if currency == "VES":
                                self.assertIn("Bs. 150,50", text)
                                self.assertIn("BOLÍVARES", text)
                                self.assertIn("881,0664", text)
                                self.assertIn("Fecha de la tasa: 01/02/2026", text)
                            else:
                                self.assertIn("USD 150,50", text)
                                self.assertIn(method.upper(), text)
                        self.assertIsNone(
                            restored.get_payment_receipt(resident_id, 1 - residence, due)
                        )
                    self.assertEqual(len(self.store.payment_history(resident_id, residence)), 2)
                    self.store.discharge(resident_id, residence, date.today(), "Fin")
                    self.assertEqual(
                        self.store.get_payment_receipt(resident_id, residence, due), pdf
                    )

    def test_monthly_receipt_failure_rolls_back_payment_and_due_date(self):
        resident_id = self.add_resident()
        due = self.store.list_residents(0)[0].next_payment
        with patch(
            "resident_store.generate_monthly_receipt",
            side_effect=OSError("Error de prueba"),
        ):
            with self.assertRaises(OSError):
                self.store.record_payment(
                    resident_id, 0, due, date.today(), "USD", Decimal("150"),
                    payment_method="Efectivo",
                )
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)
        self.assertEqual(self.store.payment_history(resident_id, 0), [])
        self.assertIsNone(self.store.get_payment_receipt(resident_id, 0, due))

    def test_bolivar_payment_saves_official_rate_and_date(self):
        resident_id = self.add_resident()
        self.store.record_payment(
            resident_id, 0, date(2026, 1, 1), date(2026, 1, 2), "VES",
            amount=Decimal("125000"),
            exchange_rate=Decimal("881.06640000"),
            receiving_bank="Banco de Venezuela",
            exchange_rate_date=date(2026, 1, 31),
        )
        self.assertEqual(
            self.store.payment_history(resident_id, 0)[0],
            {
                "due_date": "2026-01-01", "paid_on": "2026-01-02",
                "currency": "VES", "payment_method": None,
                "amount": "125000",
                "amount_usd": None,
                "monthly_amount_usd": None, "late_fee_usd": "0", "late_days": "0",
                "exchange_rate": "881.06640000",
                "exchange_rate_date": "2026-01-31",
                "receiving_bank": "Banco de Venezuela",
            },
        )

    def test_payment_rejects_missing_or_inconsistent_details(self):
        resident_id = self.add_resident()
        with self.assertRaisesRegex(ValueError, "tasa y fecha"):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 31), date.today(), "VES",
                amount=Decimal("100"),
                receiving_bank="Banco de Venezuela",
            )
        with self.assertRaisesRegex(ValueError, "efectivo"):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 31), date.today(), "USD",
                amount=Decimal("100"),
            )
        with self.assertRaisesRegex(ValueError, "cantidad recibida"):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 31), date.today(), "VES",
                amount=Decimal("0"),
                exchange_rate=Decimal("881.0664"),
                exchange_rate_date=date(2026, 1, 31),
            )
        with self.assertRaisesRegex(ValueError, "cantidad recibida"):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 31), date.today(), "USD",
                amount=Decimal("-1"),
                payment_method="Efectivo",
            )
        with self.assertRaisesRegex(ValueError, "mayor que cero"):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 31), date.today(), "VES",
                amount=Decimal("100"),
                exchange_rate=Decimal("0"),
                receiving_bank="Banco de Venezuela",
                exchange_rate_date=date(2026, 1, 31),
            )
        self.assertEqual(self.store.payment_history(resident_id, 0), [])

    def test_duplicate_active_identity_is_rejected_without_overwriting(self):
        self.add_resident()
        self.values["student_id"] = "v 12345678"
        with self.assertRaisesRegex(ValueError, "Ya existe"):
            self.add_resident()
        self.assertEqual(len(self.store.list_residents(0)), 1)

    def test_residences_are_isolated(self):
        first_id = self.add_resident()
        second_id = self.add_resident(1)
        self.assertEqual(len(self.store.list_residents(0)), 1)
        self.assertEqual(len(self.store.list_residents(1)), 1)
        self.assertNotEqual(first_id, second_id)
        with self.assertRaises(ValueError):
            self.store.get_pdf(first_id, 1)
        with self.assertRaises(ValueError):
            self.store.discharge(first_id, 1, date.today(), "Prueba")
        with self.assertRaises(ValueError):
            self.store.record_payment(
                first_id, 1, date(2026, 1, 31), date.today(), "USD",
                amount=Decimal("100"), payment_method="Efectivo",
            )

    def test_stale_payment_is_not_applied_twice(self):
        resident_id = self.add_resident()
        self.store.record_payment(
            resident_id, 0, date(2026, 1, 1), date.today(), "USD",
            amount=Decimal("100"), payment_method="Efectivo",
        )
        with self.assertRaises(ValueError):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 1), date.today(), "USD",
                amount=Decimal("100"), payment_method="Efectivo",
            )
        self.assertEqual(len(self.store.payment_history(resident_id, 0)), 1)
        self.assertEqual(self.store.list_residents(0)[0].next_payment, date(2026, 2, 1))

    def test_failed_payment_rolls_back_due_date(self):
        resident_id = self.add_resident()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO payments (resident_id, due_date, paid_on) VALUES (?, ?, ?)",
                (resident_id, "2026-01-01", "2026-01-01"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_payment(
                resident_id, 0, date(2026, 1, 1), date.today(), "USD",
                amount=Decimal("100"), payment_method="Efectivo",
            )
        self.assertEqual(self.store.list_residents(0)[0].next_payment, date(2026, 1, 1))

    def test_discharge_preserves_history_pdf_and_allows_new_admission(self):
        resident_id = self.add_resident()
        self.store.record_payment(
            resident_id, 0, date(2026, 1, 1), date.today(), "VES",
            amount=Decimal("10000"),
            receiving_bank="Banco de Venezuela",
            exchange_rate=Decimal("881.0664"),
            exchange_rate_date=date(2026, 1, 31),
        )
        with self.assertRaises(ValueError):
            self.store.discharge(resident_id, 0, date.today(), " ")
        self.store.discharge(resident_id, 0, date.today(), "Fin de estadía")
        self.assertEqual(self.store.list_residents(0), [])
        former = self.store.list_residents(0, inactive=True)[0]
        self.assertEqual(former.discharge_reason, "Fin de estadía")
        self.assertEqual(self.store.get_pdf(resident_id, 0), b"%PDF-test")
        self.assertEqual(len(self.store.payment_history(resident_id, 0)), 1)
        with self.assertRaises(ValueError):
            self.store.record_payment(
                resident_id, 0, former.next_payment, date.today(), "USD",
                amount=Decimal("100"), payment_method="Zelle",
            )
        self.assertNotEqual(self.add_resident(), resident_id)

    def test_invalid_admission_is_not_saved(self):
        self.values["admission_date"] = "31/02/2026"
        with self.assertRaises(ValueError):
            self.add_resident()
        self.assertEqual(self.store.list_residents(0), [])

    def test_new_admission_requires_room_and_bed_in_storage(self):
        for residence in (0, 1):
            for field in ("room_number", "bed_number"):
                for missing in (None, "", "   "):
                    with self.subTest(residence=residence, field=field, missing=missing):
                        values = admission_values()
                        if missing is None:
                            del values[field]
                        else:
                            values[field] = missing
                        with self.assertRaisesRegex(ValueError, "campos obligatorios"):
                            self.store.add(residence, values, [False] * 8, b"%PDF-test", None)
                        self.assertEqual(self.store.list_residents(residence), [])

    def test_alert_thresholds(self):
        today = date(2026, 1, 1)
        for days, expected in (
            (-1, "Vencido"), (0, "Pago pendiente (plazo del 1 al 5)"), (1, "Al día"),
            (3, "Al día"), (4, "Al día"),
        ):
            with self.subTest(days=days):
                self.assertEqual(payment_status(today + timedelta(days=days), today), expected)


class AppTests(unittest.TestCase):
    def test_monthly_report_can_be_regenerated_for_a_previous_month(self):
        today = date.today()
        previous_month = today.replace(day=1) - timedelta(days=1)
        report_month = previous_month.replace(day=1)
        values = admission_values("01/01/2025")
        values["student_name"] = "MARÍA CAROLINA PÉREZ GÓMEZ"
        resident_id = self.store.add(
            0, values, [False] * 8, b"%PDF-test", None,
        )
        self.store.record_deposit(
            resident_id, 0, previous_month, "Zelle", Decimal("150"),
        )

        with patch(
            "monthly_report.generate_monthly_report",
            wraps=generate_monthly_report,
        ) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            app.button(key="residencia_action_0_4").click().run()
            self.assertFalse(app.exception)
            app.selectbox(key="monthly_received_month_0").select(
                previous_month.strftime("%m/%Y")
            ).run()
            self.assertFalse(app.exception)
            self.assertEqual(
                list(app.tabs[0].table[0].value["Residente"]),
                ["MARÍA C.\nPÉREZ G.", "TOTAL"],
            )

            self.assertTrue(report.called)
            call = next(c for c in report.call_args_list if c.args[1] == report_month)
            with pymupdf.open(
                stream=generate_monthly_report(*call.args, **call.kwargs),
                filetype="pdf",
            ) as document:
                text = " ".join(" ".join(page.get_text().split()) for page in document)
                self.assertIn("MARÍA C. PÉREZ G.", text)
                self.assertNotIn("MARÍA CAROLINA PÉREZ GÓMEZ", text)
                self.assertIn("MARÍA C.\nPÉREZ G.", document[0].get_text())
                self.assertIn(
                    f"PAGOS RECIBIDOS EN EL MES - {report_month:%m/%Y}",
                    text,
                )

    def test_monthly_report_abbreviates_every_resident_name(self):
        full_names = (
            "MARÍA CAROLINA PÉREZ GÓMEZ",
            "ANA LUCÍA RIVAS TORRES",
            "JOSÉ MIGUEL DÍAZ ROJAS",
        )
        for index, full_name in enumerate(full_names):
            values = admission_values(date.today().strftime("%d/%m/%Y"))
            values.update(
                student_name=full_name,
                student_id=f"V-1234567{index}",
            )
            resident_id = self.store.add(
                0, values, [False] * 8, b"%PDF-test", None,
            )
            self.store.record_deposit(
                resident_id, 0, date.today(), "Zelle", Decimal("150"),
            )

        with patch(
            "monthly_report.generate_monthly_report",
            wraps=generate_monthly_report,
        ) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            app.button(key="residencia_action_0_4").click().run()
            self.assertFalse(app.exception)
            names = list(app.tabs[0].table[0].value["Residente"][:-1])
            expected_names = [
                format_resident_display_name(name) for name in full_names
            ]
            self.assertEqual(names, expected_names)

            call = next(c for c in report.call_args_list if c.args[0] == 0)
            with pymupdf.open(
                stream=generate_monthly_report(*call.args, **call.kwargs),
                filetype="pdf",
            ) as document:
                text = " ".join(" ".join(page.get_text().split()) for page in document)
                for full_name, expected_name in zip(full_names, expected_names):
                    self.assertIn(" ".join(expected_name.split()), text)
                    self.assertNotIn(full_name, text)
                self.assertIn("MARÍA C.\nPÉREZ G.", document[0].get_text())

    def test_new_admission_blocks_missing_assignment_before_pdf_generation(self):
        for residence in (0, 1):
            for room, bed, expected in (
                ("", "2", ["Habitación"]), ("1", "", ["Cama"]),
                ("   ", "   ", ["Habitación", "Cama"]),
            ):
                with self.subTest(residence=residence, room=room, bed=bed):
                    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
                    app.button(key=f"residencia_action_{residence}_0").click().run()
                    values = admission_values()
                    prefix = f"admission_form_{residence}"
                    for field, value in values.items():
                        if field == "age":
                            continue
                        if field in ("birth_date", "admission_date"):
                            app.date_input(key=f"{prefix}_{field}").set_value(
                                datetime.strptime(value, "%d/%m/%Y").date(),
                            )
                        elif field in ("city", "state"):
                            app.selectbox(key=f"{prefix}_{field}").select(value.upper())
                        elif field == "student_address":
                            app.text_area(key=f"{prefix}_{field}").set_value(value)
                        else:
                            app.text_input(key=f"{prefix}_{field}").set_value(value)
                    app.text_input(key=f"{prefix}_room_number").set_value(room)
                    app.text_input(key=f"{prefix}_bed_number").set_value(bed)
                    app.checkbox(key=f"{prefix}_accepted_commitments").check()
                    with patch("pymupdf.open") as pdf_open:
                        app.button(key=f"{prefix}_save").click().run()
                        pdf_open.assert_not_called()
                    self.assertFalse(app.exception)
                    message = next(e.value for e in app.error if "campos obligatorios" in e.value)
                    for label in expected:
                        self.assertIn(label, message)
                    self.assertEqual(self.store.list_residents(residence), [])

    def test_service_payment_form_receipts_and_monthly_report_for_both_residences(self):
        today = date.today()
        for residence in (0, 1):
            values = admission_values(today.strftime("%d/%m/%Y"))
            values["student_name"] = "MARÍA CAROLINA PÉREZ GÓMEZ"
            resident_id = self.store.add(
                residence, values, [False] * 8, b"%PDF-test", None,
            )
            due = self.store.list_residents(residence)[0].next_payment
            key = f"resident_{residence}_{resident_id}_False_False"
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            app.button(key=f"residencia_action_{residence}_1").click().run()
            self.assertFalse(app.exception)
            for index, concept in enumerate(SERVICE_CONCEPTS):
                currency = "Bolívares" if index != 1 else "Dólares"
                app.selectbox(key=f"{key}_service_concept").select(concept).run()
                app.selectbox(key=f"{key}_service_currency").select(currency).run()
                app.text_input(key=f"{key}_service_amount").set_value("130,01")
                if currency == "Bolívares":
                    app.selectbox(key=f"{key}_service_bank").select("Banco de Venezuela")
                    app.text_input(key=f"{key}_service_rate").set_value("1000")
                else:
                    app.selectbox(key=f"{key}_service_method").select("Zelle")
                app.run()
                app.text_input(key=f"{key}_service_invoice").set_value("00012-PRUEBA")
                app.checkbox(key=f"{key}_service_confirmed").uncheck()
                panel = next(
                    p for p in app.tabs[residence].get("popover")
                    if p.proto.popover.label == "Recibir o confirmar pago por otros servicios"
                )
                panel.button[0].click().run()
                self.assertTrue(
                    any("Confirma la recepción" in e.value for e in app.error),
                    (residence, index, [e.value for e in app.error],
                     len(self.store.service_payment_history(resident_id, residence))),
                )
                self.assertEqual(
                    len(self.store.service_payment_history(resident_id, residence)), index,
                )
                app.checkbox(key=f"{key}_service_confirmed").check()
                panel = next(
                    p for p in app.tabs[residence].get("popover")
                    if p.proto.popover.label == "Recibir o confirmar pago por otros servicios"
                )
                panel.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertEqual(self.store.list_residents(residence)[0].next_payment, due)
            self.assertEqual(
                len([b for b in app.tabs[residence].get("download_button")
                     if b.label.startswith("Descargar recibo de servicio")]), 3,
            )
            with patch("monthly_report.generate_monthly_report", wraps=generate_monthly_report) as report:
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                collection = tab.table[0].value
                self.assertEqual(
                    list(collection["Residente"][:-1]),
                    ["MARÍA C.\nPÉREZ G."] * 3,
                )
                self.assertEqual(
                    list(collection["IVA (16%) (Bs.)"]),
                    ["20.801,60", "—", "20.801,60", "41.603,20"],
                )
                self.assertEqual(collection.iloc[-1]["Bolívares (Bs.)"], "260.020,00")
                self.assertEqual(collection.iloc[-1]["Dólares (USD)"], "130,01")
                self.assertEqual(
                    list(tab.table[1].value["Bolívares (Bs.)"]),
                    ["0,00", "260.020,00", "260.020,00"],
                )
                bank = tab.table[2].value.iloc[0]
                self.assertEqual(bank["Recibido en el mes (Bs.)"], "260.020,00")
                self.assertEqual(
                    tab.table[3].value.iloc[1]["Recibido en el mes (USD)"], "130,01",
                )
                call = next(c for c in reversed(report.call_args_list) if c.args[0] == residence)
                with pymupdf.open(
                    stream=generate_monthly_report(*call.args, **call.kwargs), filetype="pdf",
                ) as document:
                    text = " ".join(" ".join(p.get_text().split()) for p in document)
                    for concept in SERVICE_CONCEPTS:
                        self.assertIn(f"Otros servicios: {concept}", text)
                    self.assertIn("MARÍA C. PÉREZ G.", text)
                    self.assertIn("MARÍA C.\nPÉREZ G.", document[0].get_text())
                    self.assertNotIn("MARÍA CAROLINA PÉREZ GÓMEZ", text)
                    self.assertIn("41.603,20", text)
                invoice_key = f"invoice_{residence}_Otros servicios: De Nevera_"
                invoice = next(i for i in tab.text_input if i.key.startswith(invoice_key))
                invoice.set_value("00099-CORREGIDA")
                form = next(f for f in tab.get("form") if f.key == invoice.key.removesuffix("_number"))
                form.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertIn(
                    "00099-CORREGIDA",
                    list(app.tabs[residence].table[0].value["N.º de factura"]),
                )

    def test_dollar_summary_shows_real_usd_by_method_in_screen_and_pdf(self):
        today = date.today()
        previous = today.replace(day=1) - timedelta(days=1)
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values("01/01/2025"), [False] * 8, b"%PDF-test", None,
            )
            due = self.store.list_residents(residence)[0].next_payment
            for paid, method, amount in (
                (previous, "Efectivo", "100.10"), (today, "Efectivo", "10.01"),
                (today, "Zelle", "20.02"), (today, "Binance", "30.03"),
                (previous, "Binance", "40.04"), (today, "Binance", "50.05"),
            ):
                self.store.record_payment(
                    resident_id, residence, due, paid, "USD", Decimal(amount),
                    payment_method=method,
                )
                due = next_month_start(due)
            with self.store._connect() as connection:
                connection.execute(
                    "UPDATE payments SET payment_method = NULL WHERE resident_id = ? "
                    "AND amount IN ('40.04', '50.05')", (resident_id,),
                )
            self.store.record_deposit(
                resident_id, residence, today, "Efectivo", Decimal("200"),
            )
            self.store.record_payment(
                resident_id, residence, due, today, "VES", Decimal("7770"),
                exchange_rate=Decimal("10"), exchange_rate_date=today,
                amount_usd=Decimal("777"), receiving_bank="Banco de Venezuela",
            )
            self.store.record_opening_balance(
                residence, previous, "USD", "Efectivo", Decimal("2000"), "Anterior",
            )
            self.store.record_opening_balance(
                residence, previous, "USD", "Zelle", Decimal("1000"), "Anterior",
            )
            self.store.record_opening_balance(
                residence, today, "USD", "Binance", Decimal("8.08"), "Actual",
            )
            self.store.discharge(
                resident_id, residence, today, "Prueba", keys_returned=True,
                inspection_ok=True, refund_method="Efectivo", refund_confirmed=True,
            )
        with patch("monthly_report.generate_monthly_report", wraps=generate_monthly_report) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                dollar_summary = tab.table[3].value
                self.assertEqual(
                    dollar_summary.to_dict("records"),
                    [
                        dict(zip(dollar_summary.columns, row))
                        for row in (
                            ("Efectivo", "2.100,10", "210,01", "2.310,11"),
                            ("Zelle", "1.000,00", "20,02", "1.020,02"),
                            ("Binance", "8,08", "30,03", "38,11"),
                            ("No registrado", "40,04", "50,05", "90,09"),
                        )
                    ],
                )
                for column, label in (
                    ("Saldo inicial / acumulado anterior (USD)", "Acumulado de meses anteriores"),
                    ("Recibido en el mes (USD)", "Total recibido en el mes"),
                    ("Acumulado al cierre (USD)", "Acumulado al cierre del mes"),
                ):
                    total = sum(Decimal(v.replace(".", "").replace(",", ".")) for v in dollar_summary[column])
                    overall = tab.table[1].value
                    value = overall.loc[overall["Resumen de cobros"] == label, "Dólares (USD)"].iloc[0]
                    expected = Decimal(value.replace(".", "").replace(",", "."))
                    if column == "Saldo inicial / acumulado anterior (USD)":
                        expected += Decimal("8.08")
                    self.assertEqual(total, expected)
                self.assertEqual(len(dollar_summary.columns), 4)
                for row in dollar_summary.to_dict("records"):
                    self.assertEqual(
                        Decimal(row["Saldo inicial / acumulado anterior (USD)"].replace(".", "").replace(",", "."))
                        + Decimal(row["Recibido en el mes (USD)"].replace(".", "").replace(",", ".")),
                        Decimal(row["Acumulado al cierre (USD)"].replace(".", "").replace(",", ".")),
                    )
                call = next(c for c in reversed(report.call_args_list) if c.args[0] == residence)
                self.assertEqual(call.kwargs["dollar_summary"], dollar_summary.to_dict("records"))
                pdf = generate_monthly_report(*call.args, **call.kwargs)
                with pymupdf.open(stream=pdf, filetype="pdf") as document:
                    text = " ".join(" ".join(page.get_text().split()) for page in document)
                    self.assertIn("Resumen de dólares Recibidos", text)
                    self.assertNotIn("Saldo inicial del mes", text)
                    self.assertIn("Saldo inicial / acumulado anterior", text)
                    for expected in ("2.310,11", "1.020,02", "38,11", "90,09"):
                        self.assertIn(expected, text)
                    self.assertIn("FIRMA DEL ADMINISTRADOR", document[-1].get_text())
                    for page in document:
                        for word in page.get_text("words"):
                            self.assertTrue(
                                pymupdf.Rect(23.9, 23.9, 768.1, 588.1).contains(pymupdf.Rect(word[:4])),
                                word,
                            )

    def test_dollar_summary_shows_zero_methods_without_usd_collections(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        for residence in (0, 1):
            app.button(key=f"residencia_action_{residence}_4").click().run()
            self.assertFalse(app.exception)
            summary = app.tabs[residence].table[2].value
            self.assertEqual(list(summary["Forma de pago USD"]), ["Efectivo", "Zelle", "Binance"])
            for column in summary.columns[1:]:
                self.assertEqual(list(summary[column]), ["0,00"] * 3)

    def test_bank_summary_matches_received_totals_and_downloaded_pdf(self):
        today = date.today()
        previous = today.replace(day=1) - timedelta(days=1)
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values("01/01/2025"), [False] * 8, b"%PDF-test", None,
            )
            due = self.store.list_residents(residence)[0].next_payment
            for paid_on, bank, amount in (
                (previous, "Banco de Venezuela", "100.10"),
                (previous, "Banco del Tesoro", "200.20"),
                (today, "Banco de Venezuela", "10.01"),
                (today, "Banco del Tesoro", "20.02"),
                (today, "Fondo Común", "30.03"),
                (previous, "Fondo Común", "40.04"),
                (today, "Fondo Común", "50.05"),
            ):
                self.store.record_payment(
                    resident_id, residence, due, paid_on, "VES", Decimal(amount),
                    exchange_rate=Decimal("1"), exchange_rate_date=paid_on,
                    amount_usd=Decimal(amount), receiving_bank=bank,
                )
                due = next_month_start(due)
            with self.store._connect() as connection:
                connection.execute(
                    "UPDATE payments SET receiving_bank = NULL WHERE resident_id = ? "
                    "AND amount IN ('40.04', '50.05')", (resident_id,),
                )
            self.store.record_payment(
                resident_id, residence, due, today, "USD", Decimal("777"),
                payment_method="Zelle",
            )
            due = next_month_start(due)
            self.store.record_payment(
                resident_id, residence, due, today, "VES", Decimal("111"),
                exchange_rate=Decimal("1"), exchange_rate_date=today,
                amount_usd=Decimal("111"), receiving_bank="Fondo Común",
            )
            with self.store._connect() as connection:
                connection.execute(
                    "UPDATE payments SET amount = 'NaN' WHERE resident_id = ? AND due_date = ?",
                    (resident_id, due.isoformat()),
                )
            self.store.record_deposit(
                resident_id, residence, today, "Efectivo", Decimal("200"),
            )
            self.store.record_opening_balance(
                residence, previous, "VES", "Banco de Venezuela", Decimal("7.07"), "Anterior",
            )
            self.store.record_opening_balance(
                residence, today, "VES", "Banco del Tesoro", Decimal("8.08"), "Actual",
            )
            self.store.record_opening_balance(
                residence, previous, "USD", "Zelle", Decimal("999"), "No es bolívares",
            )
        with patch("monthly_report.generate_monthly_report", wraps=generate_monthly_report) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                self.assertTrue(any("no se sumaron" in warning.value for warning in tab.warning))
                collection_rows = tab.table[0].value.to_dict("records")
                bolivar_row = next(
                    row for row in collection_rows if row["Bolívares (Bs.)"] == "10,01"
                )
                self.assertEqual(bolivar_row["Residente"], "RESIDENTE\nFICTICIA")
                self.assertEqual(bolivar_row["Cédula de identidad"], "V- 12.345.678")
                self.assertEqual(bolivar_row["Habitación"], "1")
                self.assertEqual(bolivar_row["Cama"], "2")
                self.assertEqual(bolivar_row["Tasa BCV (Bs./USD)"], "1,0000")
                self.assertEqual(bolivar_row["Fecha de la tasa"], today.strftime("%d/%m/%Y"))
                self.assertEqual(bolivar_row["Banco receptor"], "Banco de Venezuela")
                bank_summary = tab.table[2].value
                self.assertEqual(
                    bank_summary.to_dict("records"),
                    [
                        dict(zip(bank_summary.columns, row))
                        for row in (
                            ("Banco de Venezuela", "107,17", "10,01", "117,18"),
                            ("Banco del Tesoro", "208,28", "20,02", "228,30"),
                            ("Fondo Común", "0,00", "30,03", "30,03"),
                            ("No registrado", "40,04", "50,05", "90,09"),
                        )
                    ],
                )
                summary = tab.table[1].value
                for bank_column, label in (
                    ("Saldo inicial / acumulado anterior (Bs.)", "Acumulado de meses anteriores"),
                    ("Recibido en el mes (Bs.)", "Total recibido en el mes"),
                    ("Acumulado al cierre (Bs.)", "Acumulado al cierre del mes"),
                ):
                    total = sum(Decimal(v.replace(",", ".")) for v in bank_summary[bank_column])
                    overall = summary.loc[summary["Resumen de cobros"] == label, "Bolívares (Bs.)"].iloc[0]
                    expected = Decimal(overall.replace(",", "."))
                    if bank_column == "Saldo inicial / acumulado anterior (Bs.)":
                        expected += Decimal("8.08")
                    self.assertEqual(total, expected)
                self.assertEqual(len(bank_summary.columns), 4)
                for row in bank_summary.to_dict("records"):
                    self.assertEqual(
                        Decimal(row["Saldo inicial / acumulado anterior (Bs.)"].replace(",", "."))
                        + Decimal(row["Recibido en el mes (Bs.)"].replace(",", ".")),
                        Decimal(row["Acumulado al cierre (Bs.)"].replace(",", ".")),
                    )
                call = next(c for c in reversed(report.call_args_list) if c.args[0] == residence)
                self.assertEqual(call.kwargs["bank_summary"], bank_summary.to_dict("records"))
                pdf = generate_monthly_report(*call.args, **call.kwargs)
                with pymupdf.open(stream=pdf, filetype="pdf") as document:
                    text = "".join(page.get_text() for page in document)
                    self.assertIn("Resumen de Bolivares en Bancos", text)
                    self.assertNotIn("Saldo inicial del mes", text)
                    self.assertIn("Resumen de cobros", text)
                    self.assertIn("Tasa BCV (Bs./USD)", " ".join(text.split()))
                    self.assertIn("1,0000", text)
                    self.assertIn(today.strftime("%d/%m/%Y"), text)
                    self.assertIn("FIRMA DEL ADMINISTRADOR", text)
                    for value in ("117,18", "228,30", "30,03", "90,09", "No registrado"):
                        self.assertIn(value, text)

    def test_bank_summary_includes_all_three_banks_when_empty(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        for residence in (0, 1):
            app.button(key=f"residencia_action_{residence}_4").click().run()
            self.assertFalse(app.exception)
            summary = app.tabs[residence].table[1].value
            self.assertEqual(
                list(summary["Banco receptor"]),
                ["Banco de Venezuela", "Banco del Tesoro", "Fondo Común"],
            )
            for column in summary.columns[1:]:
                self.assertEqual(list(summary[column]), ["0,00"] * 3)

    def test_opening_balances_in_monthly_summary_and_pdf_without_double_counting(self):
        for residence in (0, 1):
            self.store.record_opening_balance(
                residence, date(2025, 12, 31), "USD", "Efectivo", Decimal("2000"),
                "Saldo ficticio de prueba",
            )
            self.store.record_opening_balance(
                residence, date(2025, 12, 31), "USD", "Zelle", Decimal("1000"),
                "Saldo ficticio de prueba",
            )
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button(key=f"residencia_action_{residence}_4").click().run()
            self.assertFalse(app.exception)
            summary = app.tabs[residence].table[0].value
            self.assertEqual(
                list(summary["Dólares (USD)"]),
                ["2.000,00", "1.000,00", "3.000,00", "0,00", "3.000,00"],
            )
            self.assertEqual(list(summary["Bolívares (Bs.)"]), ["0,00"] * 5)
            self.assertIn("Saldo inicial", summary["Resumen de cobros"][0])
            pdf = generate_monthly_report(
                residence, date.today(), [], summary.to_dict("records")
            )
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                text = document[0].get_text()
                self.assertIn("Efectivo", text)
                self.assertIn("Zelle", text)
                self.assertIn("31/12/2025", text)
                self.assertEqual(text.count("3.000,00"), 2)

    def test_current_opening_balance_is_not_monthly_resident_income(self):
        self.store.record_opening_balance(
            0, date.today(), "VES", "Banco de Venezuela", Decimal("50.25"),
            "Saldo inicial de prueba",
        )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_4").click().run()
        self.assertFalse(app.exception)
        summary = app.tabs[0].table[0].value
        self.assertEqual(
            list(summary["Bolívares (Bs.)"]), ["50,25", "0,00", "0,00", "50,25"],
        )
        self.assertEqual(list(summary["Dólares (USD)"]), ["0,00"] * 4)

    def test_manual_invoice_number_appears_in_monthly_table_and_pdf(self):
        for residence in (0, 1):
            resident_id = self.store.add(residence, admission_values(), [False] * 8, b"%PDF-test", None)
            self.store.record_deposit(resident_id, residence, date.today(), "Zelle", Decimal("200"))
            due = self.store.list_residents(residence)[0].next_payment
            self.store.record_payment(
                resident_id, residence, due, date.today(), "USD", Decimal("150"),
                payment_method="Efectivo",
            )
            for payment in self.store.received_payments(residence, date.today()):
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key=f"residencia_action_{residence}_4").click().run()
                invoice_key = f"invoice_{residence}_{payment['kind']}_{payment['record_id']}"
                app.text_input(key=f"{invoice_key}_number").set_value("000001-PRUEBA")
                form = next(i for i in app.tabs[residence].get("form") if i.key == invoice_key)
                next(i for i in form.button if i.label == "Guardar número de factura").click().run()
                self.assertFalse(app.exception)
                self.assertTrue(app.tabs[residence].success)
                self.assertIn("000001-PRUEBA", list(app.tabs[residence].table[0].value["N.º de factura"]))
            entries = app.tabs[residence].table[0].value.to_dict("records")
            summary = app.tabs[residence].table[1].value.to_dict("records")
            pdf = generate_monthly_report(residence, date.today(), entries, summary)
            with pymupdf.open(stream=pdf, filetype="pdf") as document:
                text = "".join(p.get_text() for p in document)
                self.assertEqual(text.replace("-\n", "-").count("000001-PRUEBA"), 2)
                self.assertIn("factura", text)

    def test_both_residences_share_payment_refund_and_report_workflow(self):
        for residence in (0, 1):
            for discount in ("0", "10"):
                with self.subTest(residence=residence, discount=discount):
                    values = admission_values(
                        (date.today() - timedelta(days=5)).strftime("%d/%m/%Y")
                    )
                    values["student_id"] = f"V-PARITY-{residence}-{discount}"
                    resident_id = self.store.add(
                        residence, values, [False] * 8, b"%PDF-test", None
                    )
                    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                    app.button(key=f"residencia_action_{residence}_1").click().run()
                    key = f"resident_{residence}_{resident_id}_False_False"
                    app.text_input(key=f"{key}_deposit_amount").set_value("200")
                    app.selectbox(key=f"{key}_deposit_method").select("Zelle")
                    target = app.tabs[residence].expander[0]
                    next(i for i in target.checkbox if i.label == "Confirmo que recibí el depósito").check()
                    next(i for i in target.button if i.label == "Guardar depósito").click().run()
                    self.assertFalse(app.exception)
                    app.button(key=f"residencia_action_{residence}_2").click().run()
                    pay_key = f"resident_{residence}_{resident_id}_True_False"
                    app.selectbox(key=f"{pay_key}_payment_currency").select("Bolívares").run()
                    app.text_input(key=f"{pay_key}_payment_amount").set_value("150")
                    app.text_input(key=f"{pay_key}_payment_late_fee").set_value("10")
                    app.text_input(key=f"{pay_key}_payment_exchange_rate").set_value("10")
                    app.selectbox(key=f"{pay_key}_payment_bank").select("Fondo Común").run()
                    target = app.tabs[residence].expander[0]
                    next(i for i in target.checkbox if i.label == "Confirmo que recibí este pago").check()
                    next(i for i in target.button if i.label == "Registrar pago").click().run()
                    self.assertFalse(app.exception)
                    self.assertFalse(app.tabs[residence].error)
                    self.assertTrue(any(
                        i.label == "Descargar recibo de pago de mensualidad (PDF)"
                        for i in app.tabs[residence].get("download_button")
                    ))
                    app.button(key=f"residencia_action_{residence}_1").click().run()
                    if discount == "0":
                        app.checkbox(key=f"{key}_refund_keys").check().run()
                    app.checkbox(key=f"{key}_refund_inspection").check().run()
                    self.assertEqual(
                        any(i.label == "Descuento del depósito (USD)" for i in app.tabs[residence].text_input),
                        discount != "0",
                    )
                    if discount != "0":
                        app.text_input(key=f"{key}_refund_deduction").set_value(discount)
                        app.text_area(key=f"{key}_refund_reason").set_value("Llaves no entregadas").run()
                    target = app.tabs[residence].expander[0]
                    next(i for i in target.text_input if i.label == "Motivo de la baja").set_value("Fin de prueba")
                    next(i for i in target.selectbox if i.label == "Forma de devolución del depósito en dólares").select("Binance")
                    for label in (
                        "Confirmo la entrega del depósito y el descuento aplicado",
                        "Confirmo la baja; se conservarán sus datos y planilla",
                    ):
                        next(i for i in target.checkbox if i.label == label).check()
                    next(i for i in target.button if i.label == "Dar de baja").click().run()
                    self.assertFalse(app.exception)
                    self.assertFalse(app.tabs[residence].error)
                    self.assertEqual(app.session_state[f"residence_view_{residence}"], 3)
                    target = app.tabs[residence].expander[0]
                    self.assertTrue(target.proto.expanded)
                    self.assertIn(values["student_id"], target.label)
                    refund = self.store.deposit_refund(resident_id, residence)
                    self.assertEqual(Decimal(refund["deduction"]), Decimal(discount))
                    self.assertEqual(Decimal(refund["amount"]), Decimal("200") - Decimal(discount))
                    self.assertTrue(any(
                        i.label == "**Descargar constancia de entrega de depósito (PDF)**"
                        for i in target.get("download_button")
                    ))
                    app.button(key=f"residencia_action_{residence}_4").click().run()
                    self.assertFalse(app.exception)
                    self.assertTrue(any(
                        i.label == "Descargar lista de pagos del mes (PDF)"
                        for i in app.tabs[residence].get("download_button")
                    ))

    def test_history_places_each_receipt_immediately_after_its_payment(self):
        resident_id = self.store.add(0, admission_values(), [False] * 8, b"%PDF-test", None)
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        due = self.store.list_residents(0)[0].next_payment
        for _ in range(2):
            self.store.record_payment(
                resident_id, 0, due, date.today(),
                "USD", Decimal("100"), payment_method="Efectivo",
            )
            due = next_month_start(due)
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        self.assertFalse(app.exception)
        children = list(app.tabs[0].expander[0].children.values())
        start = next(
            index for index, item in enumerate(children)
            if item.type == "markdown" and item.value == "**Historial de pagos**"
        )
        history = children[start + 1:]
        self.assertEqual(
            [item.type for item in history],
            ["markdown", "table", "download_button"] * 3,
        )
        self.assertEqual(history[0].value, "**Pago de depósito**")
        self.assertEqual(history[2].label, "Descargar recibo de depósito (PDF)")
        for index in (3, 6):
            self.assertEqual(history[index].value, "**Pago de mensualidad**")
            self.assertIn("Descargar recibo de mensualidad", history[index + 2].label)

    def test_monthly_bolivar_format_is_consistent_across_all_summaries_and_pdf(self):
        today = date.today()
        previous = today.replace(day=1) - timedelta(days=1)
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values(previous.strftime("%d/%m/%Y")),
                [False] * 8, b"%PDF-test", None,
            )
            self.store.record_opening_balance(
                residence, previous, "VES", "Banco de Venezuela",
                Decimal("130456.98"), "Prueba",
            )
            self.store.record_payment(
                resident_id, residence, previous, today, "VES", Decimal("130456.98"),
                exchange_rate=Decimal("1234.5678"), exchange_rate_date=today,
                receiving_bank="Banco de Venezuela",
            )
        with patch("monthly_report.generate_monthly_report", wraps=generate_monthly_report) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                self.assertEqual(tab.table[0].value["Bolívares (Bs.)"].iloc[0], "130.456,98")
                self.assertEqual(tab.table[0].value["Tasa BCV (Bs./USD)"].iloc[0], "1.234,5678")
                collection_total = tab.table[0].value.iloc[-1]
                self.assertEqual(collection_total["Residente"], "TOTAL")
                self.assertEqual(collection_total["Bolívares (Bs.)"], "130.456,98")
                self.assertEqual(collection_total["IVA (16%) (Bs.)"], "20.873,12")
                self.assertEqual(collection_total["Dólares (USD)"], "0,00")
                self.assertEqual(
                    list(tab.table[1].value["Bolívares (Bs.)"]),
                    ["130.456,98", "130.456,98", "130.456,98", "260.913,96"],
                )
                bank = tab.table[2].value.iloc[0]
                self.assertEqual(bank["Saldo inicial / acumulado anterior (Bs.)"], "130.456,98")
                self.assertEqual(bank["Recibido en el mes (Bs.)"], "130.456,98")
                self.assertEqual(bank["Acumulado al cierre (Bs.)"], "260.913,96")
                self.assertTrue(any(
                    "VES 130.456,98" in item.value for item in tab.markdown
                ))
                call = next(c for c in reversed(report.call_args_list) if c.args[0] == residence)
                with pymupdf.open(
                    stream=generate_monthly_report(*call.args, **call.kwargs), filetype="pdf",
                ) as document:
                    text = " ".join(" ".join(page.get_text().split()) for page in document)
                    for amount in ("130.456,98", "260.913,96", "1.234,5678"):
                        self.assertIn(amount, text)
                    self.assertNotIn("130456,98", text)

    def test_monthly_received_vat_column_matches_receipts_without_changing_totals(self):
        today = date.today()
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values(today.strftime("%d/%m/%Y")),
                [False] * 8, b"%PDF-test", None,
            )
            due = today
            for amount in ("160000", "100.03", "0.04"):
                self.store.record_payment(
                    resident_id, residence, due, today, "VES", Decimal(amount),
                    exchange_rate=Decimal("10"), exchange_rate_date=today,
                    receiving_bank="Banco de Venezuela",
                )
                due = next_month_start(due)
            self.store.record_payment(
                resident_id, residence, due, today, "USD", Decimal("100"),
                payment_method="Zelle",
            )
            self.store.record_deposit(
                resident_id, residence, today, "Zelle", Decimal("150"),
            )
            with self.store._connect() as connection:
                connection.execute(
                    "INSERT INTO payments (resident_id, due_date, paid_on, currency, amount) "
                    "VALUES (?, ?, ?, 'VES', NULL)",
                    (resident_id, next_month_start(due).isoformat(), today.isoformat()),
                )
        with patch("monthly_report.generate_monthly_report", wraps=generate_monthly_report) as report:
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                rows = tab.table[0].value
                self.assertEqual(
                    list(rows["IVA (16%) (Bs.)"]),
                    ["25.600,00", "16,00", "0,01", "—", "—", "—", "25.616,01"],
                )
                total_row = rows.iloc[-1]
                self.assertEqual(total_row["Residente"], "TOTAL")
                self.assertEqual(total_row["Bolívares (Bs.)"], "160.100,07")
                self.assertEqual(total_row["Dólares (USD)"], "250,00")
                self.assertEqual(total_row["N.º de factura"], "")
                self.assertEqual(len(tab.text_input), 6)
                totals = tab.table[1].value
                self.assertEqual(
                    list(totals["Bolívares (Bs.)"]), ["0,00", "160.100,07", "160.100,07"],
                )
                self.assertEqual(
                    list(totals["Dólares (USD)"]), ["0,00", "250,00", "250,00"],
                )
                call = next(c for c in reversed(report.call_args_list) if c.args[0] == residence)
                self.assertEqual(
                    [row["IVA (16%) (Bs.)"] for row in call.args[2]],
                    list(rows["IVA (16%) (Bs.)"]),
                )
                with pymupdf.open(
                    stream=generate_monthly_report(*call.args, **call.kwargs),
                    filetype="pdf",
                ) as document:
                    text = " ".join(" ".join(page.get_text().split()) for page in document)
                    self.assertIn("IVA (16%) (Bs.)", text)
                    self.assertIn("25.600,00", text)
                    self.assertIn("0,01", text)
                    self.assertIn("25.616,01", text)
                    self.assertIn("TOTAL", text)

    def test_monthly_received_list_separate_totals_and_previous_carry(self):
        today = date.today()
        previous = today.replace(day=1) - timedelta(days=1)
        values = admission_values("01/01/2025")
        resident_id = self.store.add(0, values, [False] * 8, b"%PDF-test", None)
        self.store.record_deposit(resident_id, 0, previous, "Binance", Decimal("50.25"))
        due = self.store.list_residents(0)[0].next_payment
        self.store.record_payment(
            resident_id, 0, due, previous, "USD", Decimal("100.10"), payment_method="Zelle",
        )
        due = next_month_start(due)
        self.store.record_payment(
            resident_id, 0, due, today, "USD", Decimal("25.20"), payment_method="Efectivo",
        )
        due = next_month_start(due)
        self.store.record_payment(
            resident_id, 0, due, today, "VES", Decimal("1000.30"),
            exchange_rate=Decimal("10.003"), exchange_rate_date=today,
            amount_usd=Decimal("100"), receiving_bank="Banco del Tesoro",
        )
        values["student_id"] = "V-CURRENT-DEPOSIT"
        fresh = self.store.add(0, values, [False] * 8, b"%PDF-test", None)
        self.store.record_deposit(fresh, 0, today, "Zelle", Decimal("70.05"))
        self.store.discharge(
            resident_id, 0, today, "Fin", keys_returned=True, inspection_ok=True,
            refund_method="Zelle", refund_confirmed=True,
        )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_4").click().run()
        self.assertFalse(app.exception)
        tab = app.tabs[0]
        self.assertEqual(tab.subheader[0].value, "Pagos recibidos en el mes")
        self.assertTrue(any(
            item.label == "Descargar lista de pagos del mes (PDF)"
            for item in tab.get("download_button")
        ))
        rows = tab.table[0].value
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows.iloc[-1]["Residente"], "TOTAL")
        self.assertEqual(rows.iloc[-1]["Dólares (USD)"], "95,25")
        rows = rows.iloc[:-1]
        self.assertIn("Banco del Tesoro", list(rows["Banco receptor"]))
        self.assertEqual(set(rows["Tipo de pago"]), {"Mensualidad", "Depósito de ingreso"})
        self.assertIn("RESIDENTE\nFICTICIA", list(rows["Residente"]))
        self.assertEqual(set(rows["Habitación"]), {"1"})
        self.assertEqual(set(rows["Cama"]), {"2"})
        totals = tab.table[1].value
        self.assertEqual(list(totals["Bolívares (Bs.)"]), ["0,00", "1.000,30", "1.000,30"])
        self.assertEqual(list(totals["Dólares (USD)"]), ["150,35", "95,25", "245,60"])
        app.button(key="residencia_action_1_4").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("No hay pagos" in i.value for i in app.tabs[1].info))
        empty_totals = app.tabs[1].table[0].value
        self.assertEqual(list(empty_totals["Dólares (USD)"]), ["0,00"] * 3)

    def test_monthly_received_legacy_unknown_bank_and_amount_warning(self):
        resident_id = self.store.add(0, admission_values(), [False] * 8, b"%PDF-test", None)
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO payments (resident_id,due_date,paid_on,currency,amount) "
                "VALUES (?, ?, ?, 'VES', '123.45')",
                (resident_id, "2026-01-01", date.today().isoformat()),
            )
            connection.execute(
                "INSERT INTO deposits (resident_id,paid_on) VALUES (?,?)",
                (resident_id, date.today().isoformat()),
            )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_4").click().run()
        self.assertFalse(app.exception)
        tab = app.tabs[0]
        self.assertTrue(any("no se sumaron" in w.value for w in tab.warning))
        self.assertIn("No registrado", list(tab.table[0].value["Banco receptor"]))
        self.assertEqual(list(tab.table[1].value["Bolívares (Bs.)"]), ["0,00", "123,45", "123,45"])

    def test_monthly_received_missing_optional_fields_keeps_view_visible(self):
        today = date.today()
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values(), [False] * 8, b"%PDF-test", None,
            )
            due = self.store.list_residents(residence)[0].next_payment
            self.store.record_payment(
                resident_id, residence, due, today, "VES", Decimal("1000"),
                exchange_rate=Decimal("10"), exchange_rate_date=today,
                amount_usd=Decimal("100"), receiving_bank="Banco del Tesoro",
            )
        received_payments = self.store.received_payments

        def legacy_payments(residence, through):
            payments = received_payments(residence, through)
            for payment in payments:
                for field in (
                    "exchange_rate", "exchange_rate_date", "receiving_bank",
                    "payment_method", "room_number", "bed_number",
                    "invoice_number", "record_id",
                ):
                    del payment[field]
            return payments

        with patch.object(self.store, "received_payments", side_effect=legacy_payments):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_4").click().run()
                self.assertFalse(app.exception)
                tab = app.tabs[residence]
                self.assertEqual(
                    list(tab.table[0].value["Fecha de la tasa"]), ["No registrada", ""]
                )
                for column in ("Tasa BCV (Bs./USD)", "Banco receptor"):
                    self.assertEqual(
                        list(tab.table[0].value[column]), ["No registrado", ""]
                    )
                self.assertTrue(any(
                    "identificador" in warning.value for warning in tab.warning
                ))
                self.assertEqual(
                    list(tab.table[1].value["Bolívares (Bs.)"]),
                    ["0,00", "1.000,00", "1.000,00"],
                )
                self.assertTrue(any(
                    button.label == "Descargar lista de pagos del mes (PDF)"
                    for button in tab.get("download_button")
                ))

    def test_active_residents_highlight_due_thresholds_in_red_for_both_residences(self):
        today = date.today()
        resident_ids = {}
        identities = {}
        for residence in (0, 1):
            for remaining in (-60, -1, 0, 1, 2, 3, 4):
                values = admission_values(
                    (today + timedelta(days=remaining)).strftime("%d/%m/%Y"),
                )
                identities[residence, remaining] = (
                    f"V-COLOR-{residence}-{'LATE' if remaining < 0 else 'DUE'}-{abs(remaining)}"
                )
                values["student_id"] = identities[residence, remaining]
                resident_ids[residence, remaining] = self.store.add(
                    residence, values, [False] * 8, b"%PDF-test", None,
                )
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        for residence in (0, 1):
            app.button(key=f"residencia_action_{residence}_1").click().run()
            self.assertFalse(app.exception)
            tab = app.tabs[residence]
            self.assertEqual(len(tab.expander), 7)
            for remaining in (-60, -1, 0, 1, 2, 3, 4):
                label = next(
                    item.label for item in tab.expander
                    if identities[residence, remaining] in item.label
                )
                self.assertEqual(label.startswith(":red["), remaining <= 0)
                if remaining <= 0:
                    self.assertIn(
                        (today + timedelta(days=remaining)).strftime("%d/%m/%Y"), label,
                    )
                    expected_status = payment_status(today + timedelta(days=remaining), today)
                    self.assertIn(expected_status, label)
            self.store.record_payment(
                resident_ids[residence, -1], residence, today - timedelta(days=1),
                today, "USD", Decimal("100"), payment_method="Efectivo",
            )
            app.run()
            label = next(
                item.label for item in app.tabs[residence].expander
                if identities[residence, -1] in item.label
            )
            self.assertFalse(label.startswith(":red["))
            app.button(key=f"residencia_action_{residence}_2").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(any(
                item.label.startswith(":red[") for item in app.tabs[residence].expander
            ))

    def test_pending_thresholds_do_not_remove_residents_from_active_list(self):
        expected_pending = []
        for remaining in (4, 3, 2, 1, 0, -1, -60):
            values = admission_values(
                (date.today() + timedelta(days=remaining)).strftime("%d/%m/%Y")
            )
            values["student_id"] = f"V-THRESHOLD-{'LATE' if remaining < 0 else 'DUE'}-{abs(remaining)}"
            resident_id = self.store.add(0, values, [False] * 8, b"%PDF-test", None)
            if remaining <= 0:
                expected_pending.append(values["student_id"])
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        self.assertEqual(len(app.expander), 7)
        app.button(key="residencia_action_0_2").click().run()
        self.assertEqual(len(app.expander), 3)
        for identity in expected_pending:
            self.assertTrue(any(identity in i.label for i in app.expander))
        self.assertEqual(len(self.store.list_residents(0)), 7)
        key = f"resident_0_{resident_id}_True_False"
        app.selectbox(key=f"{key}_payment_currency").select("Dólares").run()
        app.text_input(key=f"{key}_payment_amount").set_value("100")
        app.text_input(key=f"{key}_payment_late_fee").set_value("0")
        app.selectbox(key=f"{key}_payment_method").select("Efectivo").run()
        target = next(i for i in app.expander if "V-THRESHOLD-LATE-60" in i.label)
        next(i for i in target.checkbox if i.label == "Confirmo que recibí este pago").check()
        next(i for i in target.button if i.label == "Registrar pago").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.expander), 3)
        self.assertTrue(any("V-THRESHOLD-LATE-60" in i.label for i in app.expander))
        self.assertEqual(
            next(r.next_payment for r in self.store.list_residents(0) if r.id == resident_id),
            next_month_start(date.today() - timedelta(days=60)),
        )

    def test_pending_payment_with_manual_fee_stays_active_and_clears_pending(self):
        for index, currency in enumerate(("Dólares", "Bolívares")):
            with self.subTest(currency=currency):
                values = admission_values((date.today() - timedelta(days=5)).strftime("%d/%m/%Y"))
                values["student_id"] = f"V-PENDING-{index}"
                resident_id = self.store.add(0, values, [False] * 8, b"%PDF-test", None)
                due = date.today() - timedelta(days=5)
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key="residencia_action_0_1").click().run()
                self.assertTrue(any(values["student_id"] in i.label for i in app.expander))
                app.button(key="residencia_action_0_2").click().run()
                self.assertEqual(len(app.expander), 1)
                key = f"resident_0_{resident_id}_True_False"
                app.selectbox(key=f"{key}_payment_currency").select(currency).run()
                app.text_input(key=f"{key}_payment_amount").set_value("150")
                app.text_input(key=f"{key}_payment_late_fee").set_value("10,50").run()
                if currency == "Dólares":
                    app.selectbox(key=f"{key}_payment_method").select("Zelle").run()
                else:
                    app.selectbox(key=f"{key}_payment_bank").select("Fondo Común").run()
                    app.text_input(key=f"{key}_payment_exchange_rate").set_value("10,1").run()
                    self.assertTrue(any("1.621,05" in i.value for i in app.info))
                self.assertTrue(any("Total a recibir: USD 160,50" in i.value for i in app.info))
                next(i for i in app.checkbox if i.label == "Confirmo que recibí este pago").check()
                self.submit(app, "Registrar pago")
                self.assertFalse(app.error)
                self.assertEqual(len(app.expander), 0)
                self.assertTrue(any(
                    item.label == "Descargar recibo de pago de mensualidad (PDF)"
                    for item in app.get("download_button")
                ))
                app.run()
                self.assertFalse(app.exception)
                self.assertTrue(any(
                    item.label == "Descargar recibo de pago de mensualidad (PDF)"
                    for item in app.get("download_button")
                ))
                history = self.store.payment_history(resident_id, 0)[0]
                self.assertEqual(history["late_days"], "5")
                self.assertEqual(history["late_fee_usd"], "10.50")
                self.assertEqual(
                    Decimal(history["amount"]),
                    Decimal("160.50") if currency == "Dólares" else Decimal("1621.05"),
                )
                self.assertIsNotNone(self.store.get_payment_receipt(resident_id, 0, due))
                app.button(key="residencia_action_0_1").click().run()
                self.assertTrue(any(values["student_id"] in i.label for i in app.expander))
                self.assertEqual(len(self.store.list_residents(0)), index + 1)

    def test_residentes_de_baja_button_is_scoped_and_preserves_receipt_access(self):
        for residence in (0, 1):
            values = admission_values()
            values["student_name"] = f"BAJA RESIDENCIA {residence}"
            resident_id = self.store.add(residence, values, [False] * 8, b"%PDF-test", None)
            self.store.record_deposit(
                resident_id, residence, date.today(), "Zelle", Decimal("150")
            )
            self.store.discharge(
                resident_id, residence, date.today(), "Fin de estadía",
                keys_returned=True, inspection_ok=True, refund_method="Zelle",
                refund_confirmed=True,
            )
            values["student_name"] = f"ACTIVA RESIDENCIA {residence}"
            self.store.add(residence, values, [False] * 8, b"%PDF-test", None)
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        for residence in (0, 1):
            app.button(key=f"residencia_action_{residence}_3").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(
                app.session_state[f"residence_view_{residence}"], 3
            )
            tab = app.tabs[residence]
            self.assertEqual(tab.subheader[0].value, "Residentes de Baja")
            self.assertEqual(len(tab.expander), 1)
            self.assertIn(f"BAJA RESIDENCIA {residence}", tab.expander[0].label)
            self.assertFalse(any("ACTIVA" in item.label for item in tab.expander))
            self.assertFalse(any(item.label == "Ver historial de bajas" for item in tab.checkbox))
            self.assertFalse(any(item.label in ("Dar de baja", "Registrar pago") for item in tab.button))
            self.assertTrue(any(
                item.label == "**Descargar constancia de entrega de depósito (PDF)**"
                for item in tab.get("download_button")
            ))
            self.assertFalse(tab.warning)
        app.button(key="residencia_action_0_1").click().run()
        self.assertEqual(len(app.tabs[0].expander), 1)
        self.assertIn("ACTIVA RESIDENCIA 0", app.tabs[0].expander[0].label)

    def test_discharge_refund_full_and_discounted_download_from_history(self):
        for discount in ("00,00", "25,50"):
            with self.subTest(discount=discount):
                values = admission_values()
                values["student_id"] = f"V-{discount}"
                values = normalize_admission_values(values)
                resident_id = self.store.add(0, values, [False] * 8, b"%PDF-test", None)
                self.store.record_deposit(
                    resident_id, 0, date.today(), "Zelle", Decimal("150.50")
                )
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key="residencia_action_0_1").click().run()
                key = f"resident_0_{resident_id}_False_False"
                self.assertEqual(app.text_input(key=f"{key}_refund_deduction").value, "00,00")
                app.text_input(key=f"{key}_refund_deduction").set_value(discount).run()
                expected = "150.50" if discount == "00,00" else "125.00"
                self.assertTrue(any(
                    f"Total a reembolsar: USD {expected.replace('.', ',')}" in info.value for info in app.info
                ))
                next(i for i in app.text_input if i.label == "Motivo de la baja").set_value("Fin")
                next(i for i in app.checkbox if i.label.startswith("Confirmo la baja")).check()
                self.submit(app, "Dar de baja")
                self.assertTrue(app.error)
                self.assertIsNone(self.store.deposit_refund(resident_id, 0))
                if discount == "00,00":
                    app.text_input(key=f"{key}_refund_deduction").set_value("25,50").run()
                    app.text_area(key=f"{key}_refund_reason").set_value("Motivo anterior").run()
                    app.checkbox(key=f"{key}_refund_keys").check().run()
                    self.assertTrue(any(i.label == "Descuento del depósito (USD)" for i in app.text_input))
                    app.checkbox(key=f"{key}_refund_inspection").check().run()
                    self.assertFalse(any(i.label == "Descuento del depósito (USD)" for i in app.text_input))
                    self.assertFalse(any(i.label == "Descuento por (motivo manual)" for i in app.text_area))
                    self.assertTrue(any("Total a reembolsar: USD 150,50" in i.value for i in app.info))
                    app.checkbox(key=f"{key}_refund_keys").uncheck().run()
                    self.assertTrue(any(i.label == "Descuento del depósito (USD)" for i in app.text_input))
                    self.assertTrue(any(i.label == "Descuento por (motivo manual)" for i in app.text_area))
                    app.checkbox(key=f"{key}_refund_keys").check().run()
                    self.assertFalse(any(i.label == "Descuento del depósito (USD)" for i in app.text_input))
                    next(i for i in app.text_input if i.label == "Motivo de la baja").set_value("Fin")
                    next(i for i in app.checkbox if i.label.startswith("Confirmo la baja")).check()
                else:
                    next(i for i in app.text_area if i.label == "Descuento por (motivo manual)").set_value(
                        "Llaves no entregadas y reparación"
                    )
                next(i for i in app.selectbox if i.label == "Forma de devolución del depósito en dólares").select("Binance")
                next(i for i in app.checkbox if i.label == "Confirmo la entrega del depósito y el descuento aplicado").check()
                self.submit(app, "Dar de baja")
                self.assertFalse(app.error)
                refund = self.store.deposit_refund(resident_id, 0)
                if discount == "00,00":
                    self.assertEqual(refund["deduction_reason"], "")
                self.assertEqual(Decimal(refund["deduction"]), Decimal(discount.replace(",", ".")))
                self.assertEqual(Decimal(refund["amount"]), Decimal(expected))
                self.assertEqual(app.session_state["residence_view_0"], 3)
                self.assertEqual(app.tabs[0].subheader[0].value, "Residentes de Baja")
                discharged = next(
                    item for item in app.tabs[0].expander if values["student_id"] in item.label
                )
                self.assertTrue(discharged.proto.expanded)
                first_items = list(discharged.children.values())
                self.assertEqual(first_items[0].value, "**CONSTANCIA DE ENTREGA DE DEPÓSITO**")
                self.assertEqual(first_items[1].type, "info")
                self.assertIn(refund["receipt_number"], first_items[1].value)
                self.assertIn(
                    "Descuento: USD " + f"{Decimal(refund['deduction']):.2f}".replace(".", ","),
                    first_items[1].value,
                )
                self.assertEqual(first_items[2].type, "download_button")
                self.assertEqual(app.session_state["latest_discharged_resident_0"], resident_id)
                self.assertTrue(any(
                    values["student_id"] in item.label for item in app.tabs[0].expander
                ))
                self.assertFalse(app.exception)
                self.assertTrue(any(
                    i.label == "**Descargar constancia de entrega de depósito (PDF)**"
                    for i in app.get("download_button")
                ))
                self.assertEqual(self.store.deposit_refund(resident_id, 0), refund)

    def test_refund_storage_error_keeps_resident_active_and_reports_failure(self):
        resident_id = self.store.add(0, admission_values(), [False] * 8, b"%PDF-test", None)
        self.store.record_deposit(resident_id, 0, date.today(), "Zelle", Decimal("150"))
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        key = f"resident_0_{resident_id}_False_False"
        app.checkbox(key=f"{key}_refund_keys").check().run()
        app.checkbox(key=f"{key}_refund_inspection").check().run()
        next(i for i in app.text_input if i.label == "Motivo de la baja").set_value("Fin")
        for label in (
            "Confirmo la entrega del depósito y el descuento aplicado",
            "Confirmo la baja; se conservarán sus datos y planilla",
        ):
            next(i for i in app.checkbox if i.label == label).check()
        next(i for i in app.selectbox if i.label == "Forma de devolución del depósito en dólares").select("Zelle")
        with patch("resident_store.generate_refund_receipt", side_effect=sqlite3.OperationalError("test")):
            self.submit(app, "Dar de baja")
        self.assertTrue(any("No se registró la baja ni la devolución" in i.value for i in app.error))
        self.assertEqual(len(self.store.list_residents(0)), 1)
        self.assertIsNone(self.store.deposit_refund(resident_id, 0))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ResidentStore(Path(self.directory.name) / "test.sqlite3")
        self.mock_store = patch("resident_store.ResidentStore", return_value=self.store)
        self.mock_store.start()
        self.addCleanup(self.mock_store.stop)
        login = patch("portal_auth.require_login", return_value=SimpleNamespace(
            role="administrator", can_write=True, require_write=lambda: None,
        ))
        login.start()
        self.addCleanup(login.stop)
        area = patch("portal_auth.select_residence_area", return_value=(0, 1))
        area.start()
        self.addCleanup(area.stop)

    def open_form(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_0").click().run()
        self.assertFalse(app.exception)
        return app

    def test_pending_payments_does_not_request_email_configuration(self):
        with (
            patch("payment_reminders.MailConfig.from_environment") as mail_config,
            patch("bcv_rate.fetch_bcv_rate", return_value=(Decimal("100"), date.today())),
        ):
            mail_config.side_effect = ValueError("Falta REMINDER_SMTP_PASSWORD")
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            for residence in (0, 1):
                app.button(key=f"residencia_action_{residence}_2").click().run()
                self.assertFalse(app.exception)
                messages = [
                    item.value for kind in ("info", "warning", "error", "caption")
                    for item in app.tabs[residence].get(kind)
                ]
                self.assertFalse(any(
                    "REMINDER_SMTP_PASSWORD" in message
                    or "Recordatorios por correo pendientes de configuración" in message
                    for message in messages
                ))
            mail_config.assert_not_called()

    def test_active_payment_forms_are_inside_closed_popovers_in_both_residences(self):
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values(date.today().strftime("%d/%m/%Y")),
                [False] * 8, b"%PDF-test", None,
            )
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button(key=f"residencia_action_{residence}_1").click().run()
            self.assertFalse(app.exception)
            panels = app.tabs[residence].get("popover")
            self.assertEqual(
                [panel.proto.popover.label for panel in panels],
                ["Recibir o confirmar depósito", "Recibir o confirmar mensualidad",
                 "Recibir o confirmar pago por otros servicios", "Baja y entrega de depósito"],
            )
            self.assertTrue(all(not panel.proto.popover.open for panel in panels))
            key = f"resident_{residence}_{resident_id}_False_False"
            self.assertTrue(any(
                form.key == f"{key}_deposit" for form in panels[0].get("form")
            ))
            self.assertTrue(any(
                form.key == f"{key}_payment" for form in panels[1].get("form")
            ))
            self.assertTrue(any(
                form.key == f"{key}_service" for form in panels[2].get("form")
            ))
            self.assertTrue(any(
                form.key == f"{key}_discharge" for form in panels[3].get("form")
            ))
            self.assertIsNone(self.store.list_residents(residence)[0].discharged_on)
            self.assertEqual(
                panels[1].selectbox(key=f"{key}_payment_currency").value,
                "Selecciona una moneda",
            )
            self.assertEqual(self.store.payment_history(resident_id, residence), [])
            self.assertIsNone(self.store.deposit_date(resident_id, residence))
            app.button(key=f"residencia_action_{residence}_2").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(
                [panel.proto.popover.label for panel in app.tabs[residence].get("popover")],
                ["Baja y entrega de depósito"],
            )
            self.assertTrue(any(
                form.key.endswith("_payment") for form in app.tabs[residence].get("form")
            ))

    def test_delete_admission_requires_confirmation_in_both_residences(self):
        for residence in (0, 1):
            with self.subTest(residence=residence):
                resident_id = self.store.add(
                    residence, admission_values(date.today().strftime("%d/%m/%Y")),
                    [False] * 8, b"%PDF-test", b"photo",
                )
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key=f"residencia_action_{residence}_1").click().run()
                self.assertTrue(any(
                    "Vence hoy" in message.value for message in app.tabs[residence].warning
                ))
                key = f"resident_{residence}_{resident_id}_False_False"
                form = next(
                    form for form in app.get("form")
                    if form.key == f"{key}_delete_admission"
                )
                form.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertEqual(len(self.store.list_residents(residence)), 1)
                self.assertTrue(any(
                    "Confirma que deseas eliminar" in error.value for error in app.error
                ))
                app.checkbox(key=f"{key}_delete_confirmed").check()
                form = next(
                    form for form in app.get("form")
                    if form.key == f"{key}_delete_admission"
                )
                form.button[0].click().run()
                self.assertFalse(app.exception)
                self.assertEqual(self.store.list_residents(residence), [])
                self.assertTrue(any(
                    "Ingreso eliminado" in message.value for message in app.success
                ))

    def test_delete_admission_hidden_after_deposit_or_monthly_payment(self):
        for kind in ("deposit", "monthly"):
            with self.subTest(kind=kind):
                values = admission_values()
                values["student_id"] = f"TEST-{kind}"
                resident_id = self.store.add(
                    0, values, [False] * 8, b"%PDF-test", None,
                )
                if kind == "deposit":
                    self.store.record_deposit(
                        resident_id, 0, date.today(), "Zelle", Decimal("150"),
                    )
                else:
                    self.store.record_payment(
                        resident_id, 0, date(2026, 1, 1), date.today(),
                        "USD", Decimal("100"), payment_method="Efectivo",
                    )
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key="residencia_action_0_1").click().run()
                self.assertFalse(app.exception)
                self.assertFalse(any(
                    button.label == "Eliminar planilla de ingreso" for button in app.button
                ))

    def test_delete_admission_storage_error_preserves_record_and_reports_failure(self):
        resident_id = self.store.add(
            0, admission_values(), [False] * 8, b"%PDF-test", None,
        )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        key = f"resident_0_{resident_id}_False_False"
        app.checkbox(key=f"{key}_delete_confirmed").check()
        form = next(
            form for form in app.get("form")
            if form.key == f"{key}_delete_admission"
        )
        with patch.object(
            self.store, "delete_admission", side_effect=sqlite3.OperationalError("test"),
        ):
            form.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.success)
        self.assertTrue(any(
            "No se pudo eliminar el ingreso" in error.value for error in app.error
        ))
        self.assertEqual(self.store.get_pdf(resident_id, 0), b"%PDF-test")

    def test_manual_invoice_on_monthly_payment_form_is_saved_and_printed(self):
        for residence in (0, 1):
            resident_id = self.store.add(
                residence, admission_values(date.today().strftime("%d/%m/%Y")),
                [False] * 8, b"%PDF-test", None,
            )
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button(key=f"residencia_action_{residence}_2").click().run()
            key = f"resident_{residence}_{resident_id}_True_False"
            app.selectbox(key=f"{key}_payment_currency").select("Dólares").run()
            app.text_input(key=f"{key}_payment_amount").set_value("150")
            app.selectbox(key=f"{key}_payment_method").select("Zelle").run()
            invoice = f"000001-{residence}"
            app.text_input(key=f"{key}_payment_invoice_number").set_value(invoice)
            form = next(
                form for form in app.tabs[residence].get("form")
                if form.key == f"{key}_payment"
            )
            form.checkbox[0].check()
            form.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs[residence].error)
            with pymupdf.open(
                stream=self.store.get_payment_receipt(resident_id, residence, date.today()),
                filetype="pdf",
            ) as document:
                self.assertEqual(document[0].get_text().count(invoice), 2)
            app.button(key=f"residencia_action_{residence}_4").click().run()
            self.assertIn(
                invoice, list(app.tabs[residence].table[0].value["N.º de factura"])
            )

    def test_admission_forms_are_open_by_default_in_both_residences(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        self.assertFalse(app.exception)
        for residence in (0, 1):
            self.assertEqual(
                app.text_input(key=f"admission_form_{residence}_student_name").value,
                "",
            )
            self.assertIsNone(
                app.date_input(key=f"admission_form_{residence}_birth_date").value
            )

    def fill_form(self, app, values):
        for field, value in values.items():
            if field == "age":
                continue
            if field in ("birth_date", "admission_date"):
                app.date_input(key=f"admission_form_0_{field}").set_value(
                    datetime.strptime(value, "%d/%m/%Y").date() if value else None
                )
            elif field == "student_address":
                app.text_area(key="admission_form_0_student_address").set_value(value)
            elif field in ("city", "state"):
                app.selectbox(key=f"admission_form_0_{field}").select(value.upper())
            else:
                app.text_input(key=f"admission_form_0_{field}").set_value(value)
        app.checkbox(key="admission_form_0_accepted_commitments").check()
        return app

    def test_location_selectors_cover_catalog_in_both_residences(self):
        catalog = json.loads(
            APP_PATH.with_name("venezuela_locations.json").read_text(encoding="utf-8")
        )["states"]
        expected_states = {
            "AMAZONAS", "ANZOÁTEGUI", "APURE", "ARAGUA", "BARINAS", "BOLÍVAR",
            "CARABOBO", "COJEDES", "DELTA AMACURO", "DISTRITO CAPITAL", "FALCÓN",
            "GUÁRICO", "LA GUAIRA", "LARA", "MÉRIDA", "MIRANDA", "MONAGAS",
            "NUEVA ESPARTA", "PORTUGUESA", "SUCRE", "TÁCHIRA", "TRUJILLO",
            "YARACUY", "ZULIA",
        }
        expected_cities = {
            city.upper() for cities in catalog.values() for city in cities
        }
        self.assertEqual({state.upper() for state in catalog}, expected_states)
        self.assertGreaterEqual(len(expected_cities), 560)
        self.assertTrue(
            {"CARACAS", "MARACAIBO", "MÉRIDA", "LA GUAIRA", "TUCUPITA",
             "PUERTO AYACUCHO", "SAN CARLOS DE RÍO NEGRO"} <= expected_cities
        )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        for residence in (0, 1):
            state = app.selectbox(key=f"admission_form_{residence}_state")
            city = app.selectbox(key=f"admission_form_{residence}_city")
            self.assertEqual(state.value, "")
            self.assertEqual(city.value, "")
            self.assertEqual(set(state.options), {"", *expected_states})
            self.assertEqual(set(city.options), {"", *expected_cities})
            self.assertEqual(len(state.options), 25)
            self.assertEqual(len(city.options), len(expected_cities) + 1)
        self.assertFalse(app.exception)

    def test_location_selectors_preserve_draft_and_require_selections(self):
        app = self.fill_form(self.open_form(), admission_values())
        app.selectbox(key="admission_form_0_city").select("")
        app.selectbox(key="admission_form_0_state").select("")
        self.submit(app, "Guardar ingreso y generar planilla")
        self.assertEqual(self.store.list_residents(0), [])
        self.assertTrue(
            any("Ciudad, Estado" in error.value for error in app.error)
        )
        app.selectbox(key="admission_form_0_city").select("LA GUAIRA")
        app.selectbox(key="admission_form_0_state").select("LA GUAIRA")
        app.button(key="admission_form_0_close").click().run()
        app.button(key="residencia_action_0_0").click().run()
        self.assertEqual(
            app.selectbox(key="admission_form_0_city").value, "LA GUAIRA"
        )
        self.assertEqual(
            app.selectbox(key="admission_form_0_state").value, "LA GUAIRA"
        )
        self.assertEqual(app.selectbox(key="admission_form_1_city").value, "")
        self.submit(app, "Guardar ingreso y generar planilla")
        resident = self.store.list_residents(0)[0]
        self.assertEqual(resident.values["city"], "LA GUAIRA")
        self.assertEqual(resident.values["state"], "LA GUAIRA")
        with pymupdf.open(
            stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
        ) as document:
            self.assertEqual(document[0].get_text().count("LA GUAIRA"), 2)

    def test_location_selectors_preserve_existing_values_outside_catalog(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20)
        app.session_state["admission_draft_0"] = {
            "city": "Localidad de prueba", "state": "Vargas",
        }
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(
            app.selectbox(key="admission_form_0_city").value, "LOCALIDAD DE PRUEBA"
        )
        self.assertEqual(app.selectbox(key="admission_form_0_state").value, "VARGAS")

    def test_parent_address_selectors_follow_resident_in_both_residences(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        for residence in (0, 1):
            prefix = f"admission_form_{residence}"
            app.text_area(key=f"{prefix}_student_address").set_value(
                f"Calle inicial {residence}"
            ).run()
            for field in ("mother_address", "father_address"):
                selector = app.selectbox(key=f"{prefix}_{field}_source")
                self.assertEqual(
                    selector.options, ["Otra dirección", "La misma de la residente"]
                )
                self.assertEqual(selector.value, "Otra dirección")
                selector.select("La misma de la residente").run()
                self.assertTrue(app.text_input(key=f"{prefix}_{field}").disabled)
                self.assertEqual(
                    app.text_input(key=f"{prefix}_{field}").value,
                    f"CALLE INICIAL {residence}",
                )
            app.text_area(key=f"{prefix}_student_address").set_value(
                f"Calle nueva {residence}"
            ).run()
            for field in ("mother_address", "father_address"):
                self.assertEqual(
                    app.text_input(key=f"{prefix}_{field}").value,
                    f"CALLE NUEVA {residence}",
                )
            app.selectbox(key=f"{prefix}_mother_address_source").select(
                "Otra dirección"
            ).run()
            self.assertFalse(app.text_input(key=f"{prefix}_mother_address").disabled)
            app.text_input(key=f"{prefix}_mother_address").set_value(
                "Direccion distinta"
            ).run()
            self.assertEqual(
                app.text_input(key=f"{prefix}_mother_address").value,
                "DIRECCION DISTINTA",
            )
            self.assertEqual(
                app.text_input(key=f"{prefix}_father_address").value,
                f"CALLE NUEVA {residence}",
            )
        self.assertFalse(app.exception)
        self.assertEqual(
            app.text_input(key="admission_form_0_father_address").value, "CALLE NUEVA 0"
        )

    def test_same_parent_address_survives_closing_and_is_saved_in_pdf(self):
        app = self.fill_form(self.open_form(), admission_values()).run()
        for field in ("mother_address", "father_address"):
            app.selectbox(key=f"admission_form_0_{field}_source").select(
                "La misma de la residente"
            ).run()
        app.button(key="admission_form_0_close").click().run()
        app.button(key="residencia_action_0_0").click().run()
        for field in ("mother_address", "father_address"):
            self.assertEqual(
                app.selectbox(key=f"admission_form_0_{field}_source").value,
                "La misma de la residente",
            )
            self.assertTrue(
                app.text_input(key=f"admission_form_0_{field}").disabled
            )
        self.submit(app, "Guardar ingreso y generar planilla")
        resident = self.store.list_residents(0)[0]
        for field in ("mother_address", "father_address"):
            self.assertEqual(
                resident.values[field], resident.values["student_address"]
            )
            self.assertNotIn(f"{field}_source", resident.values)
        with pymupdf.open(
            stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
        ) as document:
            self.assertEqual(
                document[0].get_text().count(resident.values["student_address"]), 3
            )

    def submit(self, app, label):
        next(button for button in app.button if button.label == label).click().run()
        self.assertFalse(app.exception)
        return app

    def test_save_new_session_download_pay_discharge_and_history(self):
        admission = date.today().strftime("%d/%m/%Y")
        values = admission_values(admission)
        values.update({
            "student_id": "14648043",
            "student_mobile": "4148248527",
            "emergency_phone": "04148248527",
            "representative_id": "E-9134677",
        })
        app = self.fill_form(self.open_form(), values)
        self.submit(app, "Guardar ingreso y generar planilla")
        resident = self.store.list_residents(0)[0]
        self.assertEqual(resident.values["student_name"], "RESIDENTE FICTICIA")
        self.assertEqual(resident.values["student_id"], "V- 14.648.043")
        self.assertEqual(resident.values["student_mobile"], "0414-8248527")
        expected_age = calculate_age(date(2005, 1, 1), date.today())
        self.assertEqual(resident.values["age"], expected_age)
        self.assertEqual(resident.next_payment, date.today())
        self.assertEqual(len(app.get("download_button")), 2)
        self.assertTrue(any(
            item.label == "Descargar lista de residentes activas (PDF)"
            for item in app.get("download_button")
        ))
        self.assertTrue(any("Vence hoy" in warning.value for warning in app.warning))
        pdf = self.store.get_pdf(resident.id, 0)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 2)
            self.assertIn("RESIDENTE FICTICIA", document[0].get_text())
            self.assertIn("V- 14.648.043", document[0].get_text())
            self.assertIn("0414-8248527", document[0].get_text())
            self.assertIn("E-09.134.677", document[1].get_text())
            self.assertIn("E-09.134.677", document[1].get_text())
            self.assertIn(expected_age, document[0].get_text())
            self.assert_uniform_pdf_text(document)

        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        self.assertEqual(len(app.get("download_button")), 2)
        resident_key = f"resident_0_{resident.id}_False_False"
        app.selectbox(key=f"{resident_key}_payment_currency").select("Dólares").run()
        app.text_input(key=f"{resident_key}_payment_amount").set_value("125,50")
        self.submit(app, "Registrar pago")
        self.assertTrue(app.error)
        self.assertEqual(self.store.payment_history(resident.id, 0), [])
        app.selectbox(key=f"{resident_key}_payment_method").select("Zelle").run()
        next(box for box in app.checkbox if box.label == "Confirmo que recibí este pago").check()
        self.submit(app, "Registrar pago")
        self.assertEqual(
            self.store.list_residents(0)[0].next_payment, next_month_start(date.today())
        )
        payment = self.store.payment_history(resident.id, 0)[0]
        self.assertEqual(payment["currency"], "USD")
        self.assertEqual(payment["amount"], "125.50")
        self.assertEqual(payment["payment_method"], "Zelle")
        self.assertTrue(
            any("Descargar recibo de mensualidad" in item.label for item in app.get("download_button"))
        )
        next(item for item in app.text_input if item.label == "Motivo de la baja").set_value(
            "Fin de prueba"
        )
        next(box for box in app.checkbox if box.label.startswith("Confirmo la baja")).check()
        self.submit(app, "Dar de baja")
        self.assertEqual(self.store.list_residents(0), [])
        self.assertEqual(app.session_state["residence_view_0"], 3)
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 3)
        self.assertTrue(any(
            item.label == "Descargar lista de residentes de baja (PDF)"
            for item in app.get("download_button")
        ))
        self.assertTrue(any("Fin de prueba" in info.value for info in app.info))
        self.assertEqual(len(self.store.payment_history(resident.id, 0)), 1)

    def test_bolivar_payment_displays_and_saves_bcv_rate(self):
        admission = date.today().strftime("%d/%m/%Y")
        resident_id = self.store.add(
            0, admission_values(admission), [False] * 8, b"%PDF-test", None
        )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        resident_key = f"resident_0_{resident_id}_False_False"
        app.selectbox(key=f"{resident_key}_payment_currency").select(
            "Bolívares"
        ).run()
        rate_input = app.text_input(key=f"{resident_key}_payment_exchange_rate")
        self.assertEqual(rate_input.label, "Tasa del día BCV (Bs. por USD)")
        amount_input = app.text_input(key=f"{resident_key}_payment_amount")
        self.assertEqual(amount_input.label, "Monto de mensualidad en dólares (USD)")
        amount_input.set_value("150,00")
        rate_input.set_value("881,0664")
        app.run()
        app.selectbox(key=f"{resident_key}_payment_bank").select("Banco de Venezuela").run()
        self.assertTrue(any("Bs. 132.159,96" in item.value for item in app.info))
        next(
            box for box in app.checkbox
            if box.label == "Confirmo que recibí este pago"
        ).check()
        self.submit(app, "Registrar pago")

        payment = self.store.payment_history(resident_id, 0)[0]
        self.assertEqual(payment["currency"], "VES")
        self.assertEqual(payment["amount"], "132159.96")
        self.assertEqual(payment["amount_usd"], "150.00")
        self.assertEqual(payment["exchange_rate"], "881.0664")
        self.assertEqual(payment["exchange_rate_date"], date.today().isoformat())
        pdf = self.store.get_payment_receipt(resident_id, 0, date.today())
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            text = document[0].get_text()
            self.assertIn("Bs. 132.159,96", text)
            self.assertIn("Monto base: USD 150,00", text)
        self.assertTrue(
            any("Descargar recibo de mensualidad" in item.label for item in app.get("download_button"))
        )

    def test_legacy_payment_history_without_payment_details_renders(self):
        resident_id = self.store.add(
            0,
            admission_values((date.today() - timedelta(days=30)).strftime("%d/%m/%Y")),
            [False] * 8,
            b"%PDF-test",
            None,
        )
        with patch.object(
            self.store,
            "payment_history",
            return_value=[{"due_date": "2026-01-31", "paid_on": "2026-01-31"}],
        ):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button(key="residencia_action_0_1").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Historial de pagos" in item.value for item in app.markdown))

    def test_deposit_date_is_saved_once_without_changing_monthly_due(self):
        resident_id = self.store.add(
            0, admission_values(), [False] * 8, b"%PDF-test", None
        )
        due = self.store.list_residents(0)[0].next_payment
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_1").click().run()
        self.assertTrue(
            any(item.value == "**Pago Recibido Deposito**" for item in app.markdown)
        )
        deposit_notice = (
            "Cobro único de depósito por ingreso. Este monto le será reembolsado "
            "al retirarse de la residencia, en el mismo tipo de moneda recibido. "
            "Algunas condiciones aplican."
        )
        self.assertTrue(any(item.value == deposit_notice for item in app.caption))
        self.submit(app, "Guardar depósito")
        self.assertIsNone(self.store.deposit_date(resident_id, 0))
        self.assertTrue(any("Confirma la recepción" in error.value for error in app.error))
        paid_on = date.today() - timedelta(days=1)
        next(
            item for item in app.date_input if item.label == "Fecha del pago del depósito"
        ).set_value(paid_on)
        next(
            box for box in app.checkbox if box.label == "Confirmo que recibí el depósito"
        ).check()
        app.text_input(
            key=f"resident_0_{resident_id}_False_False_deposit_amount"
        ).set_value("150,50")
        self.submit(app, "Guardar depósito")
        self.assertIsNone(self.store.deposit_date(resident_id, 0))
        self.assertTrue(any("Binance o Zelle" in item.value for item in app.error))
        app.selectbox(
            key=f"resident_0_{resident_id}_False_False_deposit_method"
        ).select("Zelle")
        self.submit(app, "Guardar depósito")
        self.assertEqual(self.store.deposit_date(resident_id, 0), paid_on)
        self.assertTrue(
            any("Depósito único guardado." in item.value for item in app.success)
        )
        self.assertTrue(any(item.value == deposit_notice for item in app.caption))
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)
        self.assertEqual(self.store.payment_history(resident_id, 0), [])
        self.assertEqual(
            self.store.deposit_details(resident_id, 0)["payment_method"], "Zelle"
        )
        self.assertEqual(
            self.store.deposit_details(resident_id, 0)["amount"], "150.50"
        )
        self.assertTrue(
            any("Monto recibido del depósito: USD 150,50" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("Forma de pago del depósito: Zelle" in item.value for item in app.markdown)
        )
        self.assertFalse(any(button.label == "Guardar depósito" for button in app.button))
        self.assertEqual(len(app.get("download_button")), 3)
        self.assertFalse(
            any(button.label == "Generar recibo de depósito (PDF)" for button in app.button)
        )
        self.assertEqual(self.store.list_residents(0)[0].next_payment, due)
        self.assertTrue(
            any(paid_on.strftime("%d/%m/%Y") in item.value for item in app.info)
        )

    def test_invalid_date_and_duplicate_do_not_overwrite_resident(self):
        app = self.fill_form(self.open_form(), admission_values(""))
        self.submit(app, "Guardar ingreso y generar planilla")
        self.assertTrue(any("Fecha de ingreso" in error.value for error in app.error))
        self.assertEqual(self.store.list_residents(0), [])
        app.date_input(key="admission_form_0_admission_date").set_value(date(2026, 1, 1))
        self.submit(app, "Guardar ingreso y generar planilla")
        original = self.store.list_residents(0)[0]
        app.button(key="residencia_action_0_0").click().run()
        self.assertEqual(
            app.text_input(key="admission_form_0_student_name").value, "RESIDENTE FICTICIA"
        )
        self.submit(app, "Guardar ingreso y generar planilla")
        self.assertTrue(any("Ya existe" in error.value for error in app.error))
        self.assertEqual(self.store.list_residents(0), [original])

    def test_pending_filter_excludes_discharged_other_residence_and_later_due(self):
        for days, residence in ((30, 0), (26, 0), (40, 0), (40, 1)):
            values = admission_values((date.today() + timedelta(days=30 - days)).strftime("%d/%m/%Y"))
            values["student_id"] = f"V-{days}-{residence}"
            resident_id = self.store.add(residence, values, [False] * 8, b"%PDF-test", None)
            if days == 40 and residence == 0:
                self.store.discharge(resident_id, 0, date.today(), "Baja")
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button(key="residencia_action_0_2").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.expander), 1)
        expected_identity = normalize_admission_values({"student_id": "V-30-0"})["student_id"]
        self.assertIn(expected_identity, app.expander[0].label)

    def test_storage_error_does_not_report_success_or_save(self):
        app = self.fill_form(self.open_form(), admission_values())
        with patch.object(self.store, "add", side_effect=sqlite3.OperationalError("test")):
            self.submit(app, "Guardar ingreso y generar planilla")
        self.assertEqual(self.store.list_residents(0), [])
        self.assertTrue(any("No se pudo guardar" in error.value for error in app.error))
        self.assertFalse(app.success)
        self.assertEqual(app.text_input(key="admission_form_0_student_name").value, "RESIDENTE FICTICIA")

    def test_mother_id_is_optional_in_both_residences_and_long_address_fits(self):
        values = admission_values()
        values["representative_id"] = ""
        values["student_address"] = (
            "URBANIZACION DE EJEMPLO, FINAL DE LA SEGUNDA CALLE, EDIFICIO "
            "DE PRUEBA, PISO 2, APARTAMENTO H23, DIRECCION FICTICIA"
        )
        for residence in (0, 1):
            with self.subTest(residence=residence):
                app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
                app.button(key=f"residencia_action_{residence}_0").click().run()
                for field, value in values.items():
                    key = f"admission_form_{residence}_{field}"
                    if field == "age":
                        continue
                    if field in ("birth_date", "admission_date"):
                        app.date_input(key=key).set_value(
                            datetime.strptime(value, "%d/%m/%Y").date()
                        )
                    elif field == "student_address":
                        app.text_area(key=key).set_value(value)
                    elif field in ("city", "state"):
                        app.selectbox(key=key).select(value.upper())
                    else:
                        app.text_input(key=key).set_value(value)
                self.assertEqual(
                    app.text_input(
                        key=f"admission_form_{residence}_representative_id"
                    ).label,
                    "Cédula de la madre",
                )
                app.checkbox(
                    key=f"admission_form_{residence}_accepted_commitments"
                ).check()
                app.session_state[f"show_inactive_{residence}"] = True
                app.session_state[f"resident_search_{residence}_False_False"] = "NO COINCIDE"
                app.button(key=f"admission_form_{residence}_save").click().run()
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state[f"residence_view_{residence}"], 1)
                self.assertFalse(app.session_state[f"show_inactive_{residence}"])
                self.assertEqual(
                    app.session_state[f"resident_search_{residence}_False_False"], ""
                )
                self.assertEqual(app.tabs[residence].subheader[0].value, "Residentes Activas")
                self.assertEqual(len(app.tabs[residence].expander), 1)
                self.assertTrue(any(
                    button.label == "Descargar planilla completada (PDF)"
                    for button in app.tabs[residence].expander[0].get("download_button")
                ))
                residents = self.store.list_residents(residence)
                self.assertEqual(len(residents), 1)
                self.assertEqual(residents[0].values["representative_id"], "")
                with pymupdf.open(
                    stream=self.store.get_pdf(residents[0].id, residence),
                    filetype="pdf",
                ) as document:
                    self.assertIn(
                        values["student_address"], " ".join(document[0].get_text().split())
                    )
                    self.assert_uniform_pdf_text(document)
                app.button(key=f"residencia_action_{residence}_0").click().run()
                self.assertFalse(app.tabs[residence].get("download_button"))

    def test_student_address_continuation_uses_full_line_without_indent(self):
        for length, expected_lines in ((20, 1), (130, 2), (160, 0)):
            with self.subTest(length=length):
                values = admission_values()
                values["student_id"] = str(12000000 + length)
                values["student_address"] = "A" * length
                app = self.fill_form(self.open_form(), values)
                self.submit(app, "Guardar ingreso y generar planilla")
                resident = next(
                    resident for resident in self.store.list_residents(0)
                    if resident.values["student_address"] == values["student_address"]
                )
                with pymupdf.open(
                    stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
                ) as document:
                    spans = [
                        span
                        for block in document[0].get_text("dict")["blocks"]
                        for line in block.get("lines", [])
                        for span in line["spans"]
                        if span["font"] == "Helvetica"
                        and 376 < span["origin"][1] < 400
                    ]
                    if not expected_lines:
                        self.assertEqual(document.page_count, 3)
                        self.assertEqual([span["text"] for span in spans], ["Anexo 1"])
                        annex = "".join(document[2].get_text().split())
                        self.assertIn(values["student_address"], annex)
                        continue
                    self.assertEqual(document.page_count, 2)
                    self.assertEqual(len(spans), expected_lines)
                    self.assertEqual(
                        "".join(span["text"] for span in spans),
                        values["student_address"],
                    )
                    self.assertAlmostEqual(spans[0]["origin"][0], 180.5, places=2)
                    if expected_lines == 2:
                        self.assertAlmostEqual(spans[1]["origin"][0], 53.664, places=2)
                        self.assertAlmostEqual(spans[1]["origin"][1], 399.10, places=2)
                    self.assertTrue(all(span["bbox"][2] <= 560.6 for span in spans))
                    self.assert_uniform_pdf_text(document)

    def test_admission_photo_fills_exact_frame_with_centered_crop(self):
        for width, height in ((120, 240), (240, 120), (120, 120)):
            with self.subTest(width=width, height=height):
                image = pymupdf.Pixmap(
                    pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False
                )
                image.set_rect(image.irect, (255, 0, 0))
                ratio = 107.30 / 90.38
                if width / height > ratio:
                    crop_width = height * ratio
                    image.set_rect(
                        pymupdf.IRect(
                            int((width - crop_width) / 2), 0,
                            int((width + crop_width) / 2) + 1, height,
                        ), (0, 180, 0),
                    )
                else:
                    crop_height = width / ratio
                    image.set_rect(
                        pymupdf.IRect(
                            0, int((height - crop_height) / 2),
                            width, int((height + crop_height) / 2) + 1,
                        ), (0, 180, 0),
                    )
                photo = image.tobytes("png")
                values = admission_values()
                values["student_id"] = str(10000000 + width * 1000 + height)
                app = self.fill_form(self.open_form(), values)
                with patch("streamlit.file_uploader", return_value=BytesIO(photo)):
                    self.submit(app, "Guardar ingreso y generar planilla")
                resident = next(
                    resident for resident in self.store.list_residents(0)
                    if resident.values["student_id"]
                    == normalize_admission_values(values)["student_id"]
                )
                with pymupdf.open(
                    stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
                ) as document, pymupdf.open(
                    APP_PATH.with_name("Planilla_de_Ingreso_Residencia_Carmen_Luisa.pdf")
                ) as original:
                    rendered = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
                    original_render = original[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
                    for x in (454.94, 507.59, 560.24):
                        for y in (44.92, 89.11, 133.30):
                            self.assertEqual(
                                rendered.pixel(round(x * 2), round(y * 2)),
                                (0, 180, 0),
                            )
                    for x, y in ((452, 90), (563, 90), (507, 42), (507, 136)):
                        self.assertEqual(
                            rendered.pixel(x * 2, y * 2),
                            original_render.pixel(x * 2, y * 2),
                        )
    def assert_uniform_pdf_text(self, document):
        inserted_spans = [
            span
            for page in document
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["font"].startswith("Helvetica")
        ]
        self.assertTrue(inserted_spans)
        self.assertEqual({span["font"] for span in inserted_spans}, {"Helvetica"})
        self.assertEqual(len({round(span["size"], 2) for span in inserted_spans}), 1)
        self.assertEqual({round(span["size"], 2) for span in inserted_spans}, {10})
        self.assertTrue(all(not span["flags"] & 16 for span in inserted_spans))

    def test_age_updates_immediately_and_survives_navigation(self):
        app = self.open_form()
        app.date_input(key="admission_form_0_birth_date").set_value(date(1978, 11, 20)).run()
        age = calculate_age(date(1978, 11, 20), date.today())
        self.assertEqual(app.text_input(key="admission_form_0_age").value, age)
        self.assertTrue(app.text_input(key="admission_form_0_age").disabled)
        app.button(key="residencia_action_0_1").click().run()
        app.button(key="residencia_action_0_0").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.date_input(key="admission_form_0_birth_date").value, date(1978, 11, 20))
        self.assertEqual(app.text_input(key="admission_form_0_age").value, age)

    def test_single_email_field_and_uppercase_data_are_saved_in_pdf(self):
        app = self.open_form()
        app.text_input(key="admission_form_0_email").set_value("Prueba@").run()
        self.assertFalse(
            any("dominio" in selector.label.lower() for selector in app.selectbox)
        )
        app.text_input(key="admission_form_0_email").set_value("Prueba@Ejemplo.COM").run()
        self.assertEqual(app.text_input(key="admission_form_0_email").value, "prueba@ejemplo.com")
        values = admission_values()
        values.update(student_name="María pérez", email="PRUEBA@Ejemplo.COM", city="Mérida")
        self.fill_form(app, values)
        self.submit(app, "Guardar ingreso y generar planilla")
        resident = self.store.list_residents(0)[0]
        self.assertEqual(resident.values, normalize_admission_values({
            **values, "age": calculate_age(date(2005, 1, 1), date.today()),
        }))
        with pymupdf.open(
            stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
        ) as document:
            text = document[0].get_text()
            self.assertIn("MARÍA PÉREZ", text)
            self.assertIn("MÉRIDA", text)
            self.assertIn("prueba@ejemplo.com", text)
    def test_long_parent_address_is_complete_in_annex_at_ten_points(self):
        values = admission_values()
        values["mother_address"] = "DIRECCION FICTICIA MUY LARGA " * 12
        app = self.fill_form(self.open_form(), values)
        self.submit(app, "Guardar ingreso y generar planilla")
        resident = self.store.list_residents(0)[0]
        with pymupdf.open(
            stream=self.store.get_pdf(resident.id, 0), filetype="pdf"
        ) as document:
            self.assertEqual(document.page_count, 3)
            self.assertIn("Anexo 1", document[0].get_text())
            self.assertIn(
                " ".join(values["mother_address"].split()),
                " ".join(document[2].get_text().split()),
            )
            self.assert_uniform_pdf_text(document)


if __name__ == "__main__":
    unittest.main()
