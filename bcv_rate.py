from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
import ssl
from urllib.request import Request, urlopen


BCV_WEBSITE_URL = "https://www.bcv.org.ve/"
BCV_RATE_URL = "https://www.bcv.org.ve/estadisticas/tipo-de-cambio-de-referencia-0"
BCV_ISSUING_CA = Path(__file__).with_name("sectigo_r36.pem")


class _BCVRateParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.dollar_depth = 0
        self.in_rate = False
        self.rate_parts = []
        self.rate_text = None
        self.rate_date = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == "dolar":
            self.dollar_depth = 1
        elif tag == "div" and self.dollar_depth:
            self.dollar_depth += 1

        if tag == "strong" and self.dollar_depth:
            self.in_rate = True
            self.rate_parts = []

        if (
            tag == "span"
            and attributes.get("property") == "dc:date"
            and attributes.get("content")
        ):
            self.rate_date = attributes["content"][:10]

    def handle_endtag(self, tag):
        if tag == "strong" and self.in_rate:
            self.rate_text = "".join(self.rate_parts).strip()
            self.in_rate = False

        if tag == "div" and self.dollar_depth:
            self.dollar_depth -= 1

    def handle_data(self, data):
        if self.in_rate:
            self.rate_parts.append(data)


def parse_bcv_rate(html):
    parser = _BCVRateParser()
    parser.feed(html)
    if not parser.rate_text or not parser.rate_date:
        raise ValueError("La página del BCV no contiene la tasa USD y su fecha.")

    try:
        rate = Decimal(parser.rate_text.replace(".", "").replace(",", "."))
        published_on = date.fromisoformat(parser.rate_date)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("La tasa publicada por el BCV no tiene un formato válido.") from error

    if not rate.is_finite() or rate <= 0:
        raise ValueError("La tasa publicada por el BCV debe ser mayor que cero.")
    return rate, published_on


def fetch_bcv_rate():
    request = Request(
        BCV_RATE_URL,
        headers={"User-Agent": "Residencias portal; official BCV rate display"},
    )
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=BCV_ISSUING_CA)
    with urlopen(request, timeout=5, context=context) as response:
        html = response.read(2_000_000).decode("utf-8", errors="replace")
    return parse_bcv_rate(html)
