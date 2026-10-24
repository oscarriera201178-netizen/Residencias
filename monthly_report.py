from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO

import pymupdf

from deposit_receipt import format_bolivars
from resident_format import format_identity
from table_format import is_numeric_column


def calculate_monthly_available_summary(
    bank_totals: dict[str, dict[str, Decimal]],
    dollar_totals: dict[str, dict[str, Decimal]],
    expenses: list[dict[str, str]],
    initial_balances: list[dict[str, str | Decimal]],
    start: date,
    through: date,
) -> list[dict[str, str]]:
    expense_totals: dict[tuple[str, str], Decimal] = {}
    for expense in expenses:
        paid_on = date.fromisoformat(expense["paid_on"])
        if not start <= paid_on <= through:
            continue
        try:
            amount = Decimal(expense["amount"])
        except InvalidOperation as error:
            raise ValueError("Hay un gasto con un importe no válido.") from error
        if not amount.is_finite() or amount < 0:
            raise ValueError("Hay un gasto con un importe no válido.")
        key = (expense["currency"], expense["payment_method"])
        expense_totals[key] = expense_totals.get(key, Decimal("0")) + amount

    starting_totals: dict[tuple[str, str], Decimal] = {}
    for balance in initial_balances:
        key = (str(balance["currency"]), str(balance["payment_method"]))
        starting_totals[key] = starting_totals.get(key, Decimal("0")) + Decimal(balance["amount"])

    def format_amount(currency: str, amount: Decimal) -> str:
        return format_bolivars(amount)

    rows = []
    for currency, groups in (("VES", bank_totals), ("USD", dollar_totals)):
        for method, totals in groups.items():
            key = (currency, method)
            starting = (
                starting_totals.get(key, Decimal("0"))
                + totals.get("opening", Decimal("0"))
                - totals.get("opening_previous", Decimal("0"))
            )
            received = totals["current"]
            spent = expense_totals.get(key, Decimal("0"))
            available = starting + received - spent
            if available < 0:
                raise ValueError(
                    f"Los movimientos de {method} ({currency}) no cuadran: "
                    f"saldo inicial {format_amount(currency, starting)} + "
                    f"cobros {format_amount(currency, received)} - "
                    f"gastos {format_amount(currency, spent)}. "
                    "Revisa el saldo inicial y los movimientos de esta cuenta."
                )
            rows.append({
                "Banco / medio de pago": method,
                "Moneda": currency,
                "Saldo inicial / meses anteriores": format_amount(currency, starting),
                "Pagos recibidos en el mes": format_amount(currency, received),
                "Gastos del mes": format_amount(currency, spent),
                "Saldo disponible al cierre": format_amount(currency, available),
            })
    return rows


def generate_monthly_report(
    residence: int,
    month: date,
    entries: list[dict[str, str]],
    summary: list[dict[str, str]],
    warning: str = "",
    *,
    bank_summary: list[dict[str, str]] | None = None,
    dollar_summary: list[dict[str, str]] | None = None,
    available_summary: list[dict[str, str]] | None = None,
) -> bytes:
    def cell(tag: str, label: str, value: str) -> str:
        if tag == "th" and label.casefold() == "cédula de identidad":
            value = "Cédula de Identidad"
        formatted = (
            format_identity(value)
            if tag == "td" and "cédula" in label.casefold()
            else value
        )
        content = (
            "<br>".join(escape(line) for line in formatted.splitlines())
            if tag == "td" and label == "Residente"
            else escape(formatted)
        )
        css_classes = []
        if tag == "th" or is_numeric_column(label):
            css_classes.append("centered")
        if label == "Residente":
            css_classes.append("resident-divider")
        elif label.casefold() == "cédula de identidad":
            css_classes.append("identity-divider")
        css_class = f' class="{" ".join(css_classes)}"' if css_classes else ""
        return f"<{tag}{css_class}>{content}</{tag}>"

    def table(rows: list[dict[str, str]]) -> str:
        headers = list(rows[0])
        head = "".join(cell("th", label, label) for label in headers)
        body = "".join(
            "<tr>" + "".join(
                cell("td", label, row[label])
                for label in headers
            ) + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    css = (
        "body { font-family: sans-serif; font-size: 8pt; }"
        "table { width: 100%; border-collapse: collapse; margin-bottom: 12pt; }"
        "th, td { border: 0.5pt solid #777; padding: 4pt; }"
        "td { text-align: left; }"
        "th.centered, td.centered { text-align: center; }"
        "th.resident-divider, td.resident-divider { border-right: 0.5pt solid #777; }"
        "th.identity-divider, td.identity-divider { border-left: 0.5pt solid #777; }"
        "th { background-color: #eee; font-weight: bold; }"
    )
    output = BytesIO()
    page_rect = pymupdf.Rect(0, 0, 792, 612)
    content_rect = pymupdf.Rect(24, 90, 768, 516)
    pages: list[str] = []
    html = ""

    def fits(candidate: str) -> bool:
        candidate_story = pymupdf.Story(html=candidate, user_css=css)
        more, filled = candidate_story.place(content_rect)
        return not more and content_rect.contains(pymupdf.Rect(filled))

    def append_block(block: str) -> None:
        nonlocal html
        if fits(html + block):
            html += block
            return
        if not fits(block):
            raise ValueError("Un dato de la lista no cabe completo en una hoja del PDF.")
        if html:
            pages.append(html)
        html = block

    def append_table(rows: list[dict[str, str]], title: str = "") -> None:
        nonlocal html
        prefix = html
        page_rows: list[dict[str, str]] = []
        heading = f"<h3>{escape(title)}</h3>" if title else ""
        for row in rows:
            candidate = prefix + heading + table(page_rows + [row])
            if not fits(candidate):
                candidate = heading + table([row])
                if not fits(candidate):
                    raise ValueError("Una fila de la lista no cabe completa en una hoja del PDF.")
                if html:
                    pages.append(html)
                prefix = ""
                page_rows = []
            page_rows.append(row)
            html = candidate

    if entries:
        append_table(entries, "Resumen de cobros")
    else:
        append_table(
            [{"Pagos recibidos": "No hay pagos recibidos en este mes."}],
            "Resumen de cobros",
        )
    append_table(summary, "Totales y acumulados")
    if bank_summary:
        append_table(bank_summary, "Resumen de Bolivares en Bancos")
        append_block(
            "<p>Saldo inicial / acumulado anterior reúne los cobros anteriores y "
            "los saldos de arranque registrados hasta este mes, una sola vez.</p>"
            "<p>Los pagos sin banco identificado se muestran como «No registrado».</p>"
        )
    if dollar_summary:
        append_table(dollar_summary, "Resumen de dólares Recibidos")
        append_block(
            "<p>Saldo inicial / acumulado anterior reúne los cobros anteriores y "
            "los saldos de arranque registrados hasta este mes, una sola vez.</p>"
            "<p>Solo incluye dólares realmente recibidos y saldos iniciales en USD. "
            "No incluye la base en dólares de pagos recibidos en bolívares. "
            "Los cobros sin forma de pago identificada se muestran como «No registrado».</p>"
        )
    append_block(
        "<p>Acumulado de saldos iniciales y cobros recibidos, no saldo disponible. "
        "No descuenta gastos ni devoluciones.</p>"
    )
    if available_summary:
        append_table(available_summary, "Saldos disponibles al cierre del informe")
        append_block(
            "<p>Total de pagos recibidos en el mes menos gastos realizados en el mes, "
            "más el saldo anterior y los saldos de arranque del mes, separado por moneda y banco "
            "o medio de pago.</p>"
        )
    if warning:
        append_block(f"<p><b>Advertencia:</b> {escape(warning)}</p>")
    if html:
        pages.append(html)
    with pymupdf.DocumentWriter(output) as writer:
        for page_html in pages:
            story = pymupdf.Story(html=page_html, user_css=css)
            more, filled = story.place(content_rect)
            if more or not content_rect.contains(pymupdf.Rect(filled)):
                raise ValueError("La información de la lista excede el área imprimible.")
            device = writer.begin_page(page_rect)
            story.draw(device)
            writer.end_page()
    name, rif = (
        ("PENSIÓN QUINTA CARMEN LUISA, C.A", "504912700")
        if residence == 0 else ("RESIDENCIAS LAS ACACIAS, C.A", "50793608")
    )
    with pymupdf.open(stream=output.getvalue(), filetype="pdf") as document:
        printed_on = date.today()
        for index, page in enumerate(document):
            page.insert_text((24, 36), name, fontsize=11, fontname="hebo")
            page.insert_text((24, 52), f"RIF: {rif}", fontsize=9, fontname="helv")
            page.insert_text(
                (540, 52), f"Fecha de impresión: {printed_on:%d/%m/%Y}",
                fontsize=9, fontname="helv",
            )
            if index == len(document) - 1:
                page.draw_line((260, 548), (532, 548))
                page.insert_text(
                    (303, 566), "FIRMA DEL ADMINISTRADOR",
                    fontsize=10, fontname="hebo",
                )
            page.insert_text(
                (24, 73), f"PAGOS RECIBIDOS EN EL MES - {month:%m/%Y}",
                fontsize=11, fontname="hebo",
            )
            page.insert_text(
                (24, 584), f"Página {index + 1} de {len(document)}",
                fontsize=8, fontname="helv",
            )
        return document.tobytes(garbage=4, deflate=True)
