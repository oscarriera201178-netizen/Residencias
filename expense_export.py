from datetime import date
from decimal import Decimal
from html import escape
from io import BytesIO

import pymupdf
from money_format import format_money
from table_format import is_numeric_column


HEADERS = [
    "Fecha de registro", "Fecha de factura / gasto",
    "Nro. de factura", "Concepto", "Base imponible", "IVA 16%",
    "Total pagado", "Tipo de moneda", "Banco / medio",
]


def generate_expense_report(
    residence: int, expenses: list[dict[str, str]], printed_on: date,
) -> bytes:
    rows = [_expense_row(expense) for expense in expenses]
    if not rows:
        rows = ["", "", "", "No hay gastos registrados en este período.", "", "", "", "", ""]
        rows = [rows]
    totals = {
        currency: sum(
            (Decimal(expense["amount"]) for expense in expenses if expense["currency"] == currency),
            Decimal("0"),
        )
        for currency in ("VES", "USD")
    }
    totals_html = (
        "<p><b>Totales:</b> "
        f"VES {escape(_format_amount(totals['VES']))} &nbsp; | &nbsp; "
        f"USD {escape(_format_amount(totals['USD']))}</p>"
    )
    return generate_table_report(
        residence, HEADERS, rows, totals_html, "CONTROL DE GASTOS", printed_on,
    )


def generate_table_report(
    residence: int, headers: list[str], rows: list[list[str]],
    totals_html: str, title: str, printed_on: date,
    *, centered_columns: tuple[int, ...] | None = None,
) -> bytes:
    if centered_columns is None:
        centered_columns = tuple(
            index for index, header in enumerate(headers) if is_numeric_column(header)
        )
    css = (
        "body { font-family: sans-serif; font-size: 8pt; }"
        "table { width: 100%; border-collapse: collapse; margin-bottom: 8pt; }"
        "th, td { border: 0.5pt solid #777; padding: 3pt; }"
        "th { background-color: #eee; font-weight: bold; text-align: center; }"
        "td { text-align: left; }"
    )
    page_rect = pymupdf.Rect(0, 0, 792, 612)
    content_rect = pymupdf.Rect(24, 100, 768, 516)
    pages: list[str] = []
    page_html = ""
    page_rows: list[list[str]] = []

    def table(items: list[list[str]]) -> str:
        header = "".join(f"<th>{escape(value)}</th>" for value in headers)
        body = "".join(
            "<tr>" + "".join(
                f'<td style="text-align: {"center" if index in centered_columns else "left"}">'
                f"{escape(value)}</td>"
                for index, value in enumerate(row)
            ) + "</tr>"
            for row in items
        )
        return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"

    def fits(candidate: str) -> bool:
        story = pymupdf.Story(html=candidate, user_css=css)
        more, filled = story.place(content_rect)
        return not more and content_rect.contains(pymupdf.Rect(filled))

    for row in rows:
        candidate_rows = [*page_rows, row]
        candidate = table(candidate_rows)
        if fits(candidate):
            page_rows = candidate_rows
            page_html = candidate
            continue
        if not fits(table([row])):
            raise ValueError("Una fila no cabe completa en una hoja del PDF.")
        if page_html:
            pages.append(page_html)
        page_rows = [row]
        page_html = table(page_rows)

    if fits(page_html + totals_html):
        page_html += totals_html
    else:
        pages.append(page_html)
        page_html = totals_html
    if page_html:
        pages.append(page_html)

    output = BytesIO()
    with pymupdf.DocumentWriter(output) as writer:
        for block in pages:
            story = pymupdf.Story(html=block, user_css=css)
            more, filled = story.place(content_rect)
            if more or not content_rect.contains(pymupdf.Rect(filled)):
                raise ValueError("La información del informe excede el área imprimible.")
            device = writer.begin_page(page_rect)
            story.draw(device)
            writer.end_page()

    name, rif = (
        ("PENSIÓN QUINTA CARMEN LUISA, C.A", "504912700")
        if residence == 0 else ("RESIDENCIAS LAS ACACIAS, C.A", "50793608")
    )
    with pymupdf.open(stream=output.getvalue(), filetype="pdf") as document:
        for index, page in enumerate(document):
            page.insert_text((24, 36), name, fontsize=11, fontname="hebo")
            page.insert_text((24, 52), f"RIF: {rif}", fontsize=9, fontname="helv")
            page.insert_text(
                (540, 52), f"Fecha de impresión: {printed_on:%d/%m/%Y}",
                fontsize=9, fontname="helv",
            )
            page.insert_text((24, 76), title, fontsize=11, fontname="hebo")
            if index == len(document) - 1:
                page.draw_line((260, 548), (532, 548))
                page.insert_text(
                    (303, 566), "FIRMA DEL ADMINISTRADOR",
                    fontsize=10, fontname="hebo",
                )
            page.insert_text(
                (24, 584), f"Página {index + 1} de {len(document)}",
                fontsize=8, fontname="helv",
            )
        return document.tobytes(garbage=4, deflate=True)


def _expense_row(expense: dict[str, str]) -> list[str]:
    total = Decimal(expense["amount"])
    vat = Decimal(expense["vat"])
    has_invoice = bool(expense["invoice_number"])
    return [
        date.fromisoformat(expense["registered_on"]).strftime("%d/%m/%Y")
        if expense["registered_on"] else "No disponible",
        date.fromisoformat(expense["paid_on"]).strftime("%d/%m/%Y"),
        expense["invoice_number"] or "Sin factura",
        expense["concept"],
        _format_amount(total - vat) if has_invoice else "No aplica",
        _format_amount(vat),
        _format_amount(total),
        expense["currency"],
        expense["payment_method"],
    ]


def _format_amount(amount: Decimal) -> str:
    return format_money(amount)
