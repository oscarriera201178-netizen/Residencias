import re
from decimal import Decimal, InvalidOperation


def format_money(amount: Decimal) -> str:
    return f"{amount:,.2f}".translate(str.maketrans(",.", ".,"))


def parse_money(value: str) -> Decimal:
    value = value.strip()
    if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?", value):
        return Decimal(value.replace(".", "").replace(",", "."))
    if re.fullmatch(r"[+-]?\d+\.\d{1,2}", value):
        return Decimal(value)
    raise InvalidOperation("Usa punto de miles y coma decimal. Ejemplo: 1.234,56.")
