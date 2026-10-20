import re


_IDENTITY_PREFIX = re.compile(r"^([VE])\s*[-.]?\s*", re.IGNORECASE)
_IDENTITY_SEPARATORS = re.compile(r"[\d\s.,-]+")
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?58[\s.-]?)?0?((?:2|4)\d{2})"
    r"[\s().-]*(\d(?:[\s().-]*\d){6})(?!\d)"
)


def normalize_invoice_number(value: str) -> str:
    number = value.strip()
    if len(number) > 40 or any(ord(character) < 32 for character in number):
        raise ValueError("El número de factura admite hasta 40 caracteres en una sola línea.")
    return number


def format_identity(value: str) -> str:
    value = value.strip().upper()
    prefix_match = _IDENTITY_PREFIX.match(value)
    prefix = prefix_match.group(1) if prefix_match else "V"
    number = value[prefix_match.end():] if prefix_match else value
    if not number or not _IDENTITY_SEPARATORS.fullmatch(number):
        return value

    digits = "".join(character for character in number if character.isdigit())
    if not digits:
        return value
    if int(digits) < 10_000_000:
        digits = digits.zfill(8)
    groups = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    formatted_number = ".".join(reversed(groups))
    separator = " " if prefix == "V" else ""
    return f"{prefix}-{separator}{formatted_number}"


def format_phone(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        subscriber = "".join(character for character in match.group(2) if character.isdigit())
        return f"0{match.group(1)}-{subscriber}"

    return _PHONE_PATTERN.sub(replace, value.strip())
