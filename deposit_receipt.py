from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pymupdf
from num2words import num2words

from resident_format import format_identity, normalize_invoice_number
from money_format import format_money
from payment_schedule import late_payment_days


SERVICE_CONCEPTS = ("De Nevera", "De Aire Acondicionado", "Llaves")


def format_bolivars(amount: Decimal) -> str:
    return format_money(amount)


def calculate_receipt_vat(amount: Decimal) -> Decimal:
    return (amount * Decimal("0.16")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_deposit_receipt(
    resident_id: int,
    residence: int,
    values: dict[str, str],
    paid_on: date,
    amount: Decimal,
    payment_method: str,
    *,
    receipt_number: str,
) -> bytes:
    return _generate_receipt(
        receipt_number, "RECIBO DE PAGO DE DEPOSITO",
        residence, values, paid_on, amount, "USD",
        payment_method, "DEPÓSITO ÚNICO POR LLAVES Y HABITACIÓN",
        "Este monto le será reembolsado al retirarse de la residencia, "
        "en el mismo tipo de moneda recibido. Algunas condiciones aplican.",
        fiscal_notice=True,
    )


def generate_monthly_receipt(
    resident_id: int,
    residence: int,
    values: dict[str, str],
    due_date: date,
    paid_on: date,
    amount: Decimal,
    currency: str,
    payment_method: str | None,
    exchange_rate: Decimal | None,
    exchange_rate_date: date | None,
    amount_usd: Decimal | None = None,
    *,
    receipt_number: str,
    monthly_amount_usd: Decimal | None = None,
    late_fee_usd: Decimal = Decimal("0"),
    receiving_bank: str | None = None,
    invoice_number: str = "",
    service_concept: str | None = None,
) -> bytes:
    if service_concept is not None and service_concept not in SERVICE_CONCEPTS:
        raise ValueError("Selecciona un concepto de servicio válido.")
    fields: list[tuple[str, str]] = []
    late_days = late_payment_days(due_date, paid_on)
    if late_days:
        monthly = (
            f"USD {format_money(monthly_amount_usd)}"
            if monthly_amount_usd is not None else
            f"USD {format_money(amount_usd - late_fee_usd)}" if amount_usd is not None else
            f"USD {format_money(amount - late_fee_usd)}" if currency == "USD" else
            f"Bs. {format_bolivars(amount - late_fee_usd)}"
        )
        fields.extend([
            ("Mensualidad:", monthly),
            ("Días de retardo:", str(late_days)),
            ("Mora:", f"USD {format_money(late_fee_usd)}"),
        ])
    if currency == "VES":
        if exchange_rate is None or exchange_rate_date is None:
            raise ValueError("El recibo en bolívares requiere la tasa BCV y su fecha.")
        if amount_usd is not None:
            fields.append(("Monto base:", f"USD {format_money(amount_usd)}"))
        fields.extend([
            ("Tasa BCV:", f"Bs. {str(exchange_rate).replace('.', ',')} por USD."),
            ("Fecha de la tasa:", f"{exchange_rate_date:%d/%m/%Y}."),
        ])
        if receiving_bank:
            fields.append(("Banco receptor:", receiving_bank))
    return _generate_receipt(
        receipt_number, (
            "RECIBO DE PAGO POR OTROS SERVICIOS" if service_concept
            else "RECIBO DE PAGO DE MENSUALIDAD"
        ),
        residence, values, paid_on,
        amount, currency, payment_method,
        service_concept or "Pago de Mensualidad de Habitación", "", fields,
        fields_y=250 if late_days else 260,
        signature_y=355 if late_days else 327,
        invoice_number=normalize_invoice_number(invoice_number),
        fiscal_notice=True,
        show_vat=currency == "VES",
    )


def generate_refund_receipt(
    residence: int,
    values: dict[str, str],
    refunded_on: date,
    deposit_amount: Decimal,
    deduction: Decimal,
    deduction_reason: str,
    keys_returned: bool,
    inspection_ok: bool,
    payment_method: str,
    *,
    receipt_number: str,
) -> bytes:
    fields = [
        ("DEPÓSITO ORIGINAL:", f"USD {format_money(deposit_amount)}"),
        ("DESCUENTO:", f"USD {format_money(deduction)}"),
        ("LLAVES ENTREGADAS:", "Sí" if keys_returned else "No"),
        ("REVISIÓN:", "SIN NOVEDAD" if inspection_ok else "Con novedades"),
    ]
    if deduction:
        fields.append(("DESCUENTO POR:", deduction_reason))
    return _generate_receipt(
        receipt_number, "CONSTANCIA DE ENTREGA DE DEPOSITO",
        residence, values, refunded_on, deposit_amount - deduction, "USD",
        payment_method, "ENTREGA DE DEPOSITO", "", fields,
        fields_y=250, final_field_height=32, signature_y=345,
    )


def _generate_receipt(
    receipt_number: str,
    title: str,
    residence: int,
    values: dict[str, str],
    paid_on: date,
    amount: Decimal,
    currency: str,
    payment_method: str | None,
    concept: str,
    note: str,
    fields: list[tuple[str, str]] | None = None,
    *,
    fields_y: int = 260,
    final_field_height: int = 28,
    signature_y: int = 327,
    invoice_number: str = "",
    fiscal_notice: bool = False,
    show_vat: bool = False,
) -> bytes:
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("El monto del recibo debe tener como máximo dos decimales.")
    if currency not in {"USD", "VES"}:
        raise ValueError("La moneda del recibo debe ser USD o VES.")
    formatted_amount = format_money(amount)
    total = f"{'USD' if currency == 'USD' else 'Bs.'} {formatted_amount}"
    words = (
        f"{num2words(int(amount), lang='es').upper()} "
        f"{'DÓLARES' if currency == 'USD' else 'BOLÍVARES'} "
        f"CON {int((amount % 1) * 100):02d}/100"
    )
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        residence_name = (
            "PENSIÓN QUINTA CARMEN LUISA, C.A"
            if residence == 0 else "RESIDENCIAS LAS ACACIAS, C.A"
        )

        for offset, copy_label in (
            (0, ""),
            (396, "COPIA PARA EL ARCHIVO DE LA RESIDENCIA"),
        ):
            def fill(
                text: str, x: float, y: float, width: float,
                height: float = 28, bold: bool = False,
            ) -> None:
                rect = pymupdf.Rect(x, y + offset, x + width, y + offset + height)
                if page.insert_textbox(
                    rect, text, fontsize=10,
                    fontname="hebo" if bold else "helv", color=(0, 0, 0)
                ) < 0:
                    raise ValueError("Un dato no cabe en el recibo.")

            def field(
                label: str, value: str, x: float, y: float, width: float,
                height: float = 28,
            ) -> None:
                label_width = pymupdf.Font("hebo").text_length(label + " ", fontsize=10)
                fill(label, x, y, label_width, bold=True)
                fill(value, x + label_width, y, width - label_width, height)

            fill(residence_name, 140, 10, 440, bold=True)
            field("RIF:", "504912700" if residence == 0 else "50793608", 140, 24, 440)
            fill(title, 140, 39, 440, bold=True)
            field("RECIBO Nro.", receipt_number, 345, 55, 230)
            if copy_label:
                fill(copy_label, 30, 73, 550, bold=True)
            field("FECHA:", f"{paid_on:%d/%m/%Y}", 345, 73, 230)
            if invoice_number:
                field("FACTURA Nro.", invoice_number, 30, 86, 550)
            fill("NOMBRES Y APELLIDOS:", 30, 101, 155, bold=True)
            fill(values["student_name"], 185, 101, 395)
            fill("CÉDULA DE IDENTIDAD:", 30, 132, 155, bold=True)
            fill(format_identity(values["student_id"]), 185, 132, 395)
            fill("HE RECIBIDO LA CANTIDAD DE (EN LETRAS):", 30, 156, 550, bold=True)
            fill(words, 30, 174, 550)
            field("POR CONCEPTO DE:", concept, 30, 208, 550)
            if show_vat:
                vat = calculate_receipt_vat(amount)
                subtotal = amount - vat
                field(
                    "SUBTOTAL:", "Bs. " + format_bolivars(subtotal),
                    30, 234, 200,
                )
                field(
                    "IVA (16%):", "Bs. " + format_bolivars(vat),
                    230, 234, 170,
                )
                field("TOTAL:", total, 400, 234, 180)
            elif payment_method:
                field("FORMA DE PAGO:", payment_method.upper(), 30, 234, 300)
            else:
                field("MONEDA:", "BOLÍVARES (VES)", 30, 234, 300)
            if not show_vat:
                field("TOTAL:", total, 345, 234, 235)
            if note:
                fill(note, 30, 260, 550, 32)
            for index, (label, value) in enumerate(fields or []):
                field(
                    label, value, 30, fields_y + index * 14, 550,
                    final_field_height if index == len(fields or []) - 1 else 28,
                )
            page.draw_line((45, signature_y + offset), (255, signature_y + offset))
            page.draw_line((350, signature_y + offset), (560, signature_y + offset))
            fill("RECIBIDO CONFORME", 75, signature_y + 5, 210, bold=True)
            fill("ENTREGA CONFORME", 380, signature_y + 5, 200, bold=True)
            if fiscal_notice:
                notice = "NO ES VALIDO PARA FACTURA FISCAL"
                font_size = 7
                text_width = pymupdf.get_text_length(
                    notice, fontname="helv", fontsize=font_size,
                )
                page.insert_text(
                    ((page.rect.width - text_width) / 2, 382 + offset),
                    notice, fontsize=font_size, fontname="helv", color=(0, 0, 0),
                )

        cut_color = (0.4, 0.4, 0.4)
        page.draw_line((20, 396), (290, 396), dashes="[4 3]", color=cut_color)
        page.draw_line((322, 396), (592, 396), dashes="[4 3]", color=cut_color)
        page.draw_oval((294, 389, 301, 395), color=cut_color, width=0.8)
        page.draw_oval((294, 397, 301, 403), color=cut_color, width=0.8)
        page.draw_line((300, 393), (317, 401), color=cut_color, width=0.8)
        page.draw_line((300, 399), (317, 391), color=cut_color, width=0.8)
        return document.tobytes(garbage=4, deflate=True)
