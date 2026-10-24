from collections.abc import Mapping, Sequence
from unicodedata import combining, normalize

import pandas as pd
from pandas.io.formats.style import Styler


def is_numeric_column(label: str) -> bool:
    label = "".join(
        character for character in normalize("NFKD", label.casefold())
        if not combining(character)
    )
    return label.startswith((
        "fecha", "nro.", "n.º", "n.o", "numero", "cedula", "telefono",
        "habitacion", "cama", "edad", "monto", "importe", "base imponible",
        "iva", "debito", "credito", "saldo", "cantidad", "total pagado",
        "bolivares", "dolares", "tasa", "mensualidad", "mora", "dias",
        "vencimiento", "pago registrado", "referencia", "recibido en el mes",
        "pagos recibidos en el mes", "gastos del mes", "acumulado al cierre",
        "deposito original", "descuento (",
    ))


def style_table(rows: Sequence[Mapping[str, object]]) -> Styler:
    frame = pd.DataFrame(list(rows))
    styles = pd.DataFrame("text-align: left", index=frame.index, columns=frame.columns)
    for column in frame.columns:
        if is_numeric_column(str(column)):
            styles[column] = "text-align: center"
    if "Dato" in frame and "Valor" in frame:
        styles["Valor"] = frame["Dato"].map(
            lambda label: "text-align: center" if is_numeric_column(str(label))
            else "text-align: left"
        )
    return frame.style.apply(lambda _: styles, axis=None).set_table_styles([
        {"selector": "th", "props": [("text-align", "center"), ("font-weight", "bold")]},
    ])
