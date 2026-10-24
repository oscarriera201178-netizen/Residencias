from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

from deposit_receipt import calculate_receipt_vat, format_bolivars
from expense_export import generate_table_report


@dataclass(frozen=True)
class VatReport:
    entries: list[dict[str, str]]
    debit: Decimal
    credit: Decimal

    @property
    def net(self) -> Decimal:
        return self.debit - self.credit


def calculate_vat_report(movements: list[dict[str, str]]) -> VatReport:
    entries = []
    debit = credit = Decimal("0")
    for movement in movements:
        try:
            amount = Decimal(movement["amount"])
            vat = (
                calculate_receipt_vat(amount) if movement["kind"] == "Cobro"
                else Decimal(movement["vat"])
            )
        except (InvalidOperation, TypeError) as error:
            raise ValueError("Hay una factura con un importe o IVA no válido.") from error
        if not amount.is_finite() or amount <= 0 or not vat.is_finite() or vat < 0:
            raise ValueError("Hay una factura con un importe o IVA no válido.")
        is_debit = movement["kind"] == "Cobro"
        debit += vat if is_debit else Decimal("0")
        credit += vat if not is_debit else Decimal("0")
        entries.append({
            "Fecha": date.fromisoformat(movement["paid_on"]).strftime("%d/%m/%Y"),
            "Actividad": movement["kind"],
            "Nro. de factura": movement["invoice_number"],
            "Concepto": movement["concept"],
            "Importe (Bs.)": format_bolivars(amount),
            "Débito fiscal (Bs.)": format_bolivars(vat if is_debit else Decimal("0")),
            "Crédito fiscal (Bs.)": format_bolivars(vat if not is_debit else Decimal("0")),
        })
    return VatReport(entries, debit, credit)


def generate_vat_pdf(
    residence: int, month: date, report: VatReport,
    payment: dict[str, str] | None = None,
) -> bytes:
    headers = [
        "Fecha", "Actividad", "Nro. de factura", "Concepto", "Importe (Bs.)",
        "Débito fiscal (Bs.)", "Crédito fiscal (Bs.)",
    ]
    rows = [[entry[header] for header in headers] for entry in report.entries]
    if not rows:
        rows = [["", "", "", "No hay facturas en este mes.", "", "", ""]]
    summary = (
        f"<p>Débito fiscal: {escape(format_bolivars(report.debit))} Bs. | "
        f"Crédito fiscal: {escape(format_bolivars(report.credit))} Bs.</p>"
        f"<p>IVA A PAGAR: {escape(format_bolivars(report.net))} Bs.</p>"
    )
    if payment:
        summary += (
            f"<p>Pago registrado: {escape(payment['paid_on'])} | "
            f"{escape(payment['payment_method'])} | "
            f"Referencia: {escape(payment['reference'])} | "
            f"Bs. {escape(format_bolivars(Decimal(payment['amount'])))}</p>"
            f"<p>Débito al registrar el pago: {escape(format_bolivars(Decimal(payment['debit'])))} | "
            f"Crédito al registrar el pago: {escape(format_bolivars(Decimal(payment['credit'])))}</p>"
        )
    elif report.net < 0:
        summary += "<p>Saldo a favor del mes. No se traslada automáticamente a otros meses.</p>"
    return generate_table_report(
        residence, headers, rows, summary, f"PAGO DE IVA (16%) - {month:%m/%Y}", date.today(),
    )
