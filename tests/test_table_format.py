from datetime import date

import pymupdf
import pytest

from expense_export import generate_table_report
from monthly_report import generate_monthly_report
from table_format import is_numeric_column, style_table


@pytest.mark.parametrize("label", [
    "Fecha", "Fecha de registro del gasto", "Fecha de factura / gasto",
    "Fecha de la tasa", "Nro.", "Nro. de factura", "N.º de factura",
    "Cédula de identidad", "Teléfono celular", "Habitación", "Cama", "Edad",
    "Base imponible", "IVA 16%", "Total pagado", "Importe (Bs.)",
    "Débito fiscal (Bs.)", "Crédito fiscal (Bs.)", "Bolívares (Bs.)", "Dólares (USD)",
    "Tasa BCV (Bs./USD)", "Saldo disponible al cierre", "Pagos recibidos en el mes",
    "Gastos del mes", "Cantidad recibida", "Monto base (USD)", "Mensualidad (USD)",
    "Mora (USD)", "Días de retardo", "Vencimiento cubierto", "Pago registrado",
    "Recibido en el mes (USD)", "Acumulado al cierre (Bs.)", "Referencia",
])
def test_numeric_column_policy(label):
    assert is_numeric_column(label)


@pytest.mark.parametrize("label", [
    "Residente", "Nombres y apellidos", "Motivos del egreso", "Concepto",
    "Banco / medio", "Banco receptor", "Forma de pago USD", "Moneda",
    "Recaudo", "Consignado", "Dato", "Resumen de cobros", "Actividad",
])
def test_text_column_policy(label):
    assert not is_numeric_column(label)


def test_screen_styles_headers_numeric_text_and_personal_values():
    styled = style_table([{
        "Fecha": "02/01/2026", "Residente": "ANA 2", "Importe (Bs.)": "1.234,56",
        "Banco": "BANCO 123",
    }])
    styled._compute()
    for column in (0, 2):
        assert ("text-align", "center") in styled.ctx[(0, column)]
    for column in (1, 3):
        assert ("text-align", "left") in styled.ctx[(0, column)]
    assert styled.table_styles == [{
        "selector": "th", "props": [("text-align", "center"), ("font-weight", "bold")],
    }]
    personal = style_table([
        {"Dato": "Cédula de identidad", "Valor": "V-123"},
        {"Dato": "Nombres y apellidos", "Valor": "ANA 2"},
        {"Dato": "Teléfono celular", "Valor": "Sin indicar"},
        {"Dato": "Dirección actual", "Valor": "CALLE 123"},
    ])
    personal._compute()
    assert all(("text-align", "center") in personal.ctx[(row, 1)] for row in (0, 2))
    assert all(("text-align", "left") in personal.ctx[(row, 1)] for row in (1, 3))


@pytest.mark.parametrize("residence", [0, 1])
@pytest.mark.parametrize("monthly", [False, True])
def test_rendered_pdf_alignments_for_dates_amounts_identity_and_text(residence, monthly):
    headers = ["Fecha", "Concepto", "Cédula de identidad", "Importe (Bs.)"]
    values = ["02/01/2026", "SERVICIO 123", "V-12345678", "1.234,56"]
    today = date.today()
    if monthly:
        pdf = generate_monthly_report(
            residence, today, [dict(zip(headers, values))],
            [{"Concepto": "TOTAL", "Importe (Bs.)": "1.234,56"}],
        )
    else:
        pdf = generate_table_report(
            residence, headers, [values], "", "PRUEBA", today,
        )
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[0]
        cells = page.find_tables(strategy="lines_strict", snap_tolerance=0.5).tables[0].rows[0].cells
        spans = [
            span for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", []) for span in line["spans"]
        ]
        for index, cell in enumerate(cells):
            left, top, right, _ = cell
            column_spans = [
                span for span in spans
                if left <= span["bbox"][0] < right and top <= span["bbox"][1] < top + 35
            ]
            header = next(span for span in column_spans if "Bold" in span["font"])
            value = next(span for span in column_spans if "Bold" not in span["font"])
            midpoint = (left + right) / 2
            assert (header["bbox"][0] + header["bbox"][2]) / 2 == pytest.approx(midpoint, abs=0.6)
            if index != 1:
                assert (value["bbox"][0] + value["bbox"][2]) / 2 == pytest.approx(midpoint, abs=0.6)
            else:
                assert value["bbox"][0] == pytest.approx(left + (4.25 if monthly else 3.25), abs=0.6)
