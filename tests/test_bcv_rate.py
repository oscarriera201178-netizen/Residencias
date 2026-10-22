import unittest
from datetime import date
from decimal import Decimal
from io import BytesIO
import ssl
from unittest.mock import patch

from bcv_rate import BCV_RATE_URL, fetch_bcv_rate, parse_bcv_rate


BCV_HTML = """
<div id="euro"><strong>993,21105346</strong></div>
<div id="dolar" class="col-sm-12">
  <div class="field-content">
    <div class="row">
      <span>USD</span>
      <strong class="strong-tb">884,74960000</strong>
    </div>
  </div>
</div>
<span class="date-display-single" property="dc:date"
      content="2026-10-22T00:00:00-04:00">Jueves, 22 Octubre 2026</span>
"""


class BCVRateTests(unittest.TestCase):
    def test_parse_rate_and_publication_date_from_usd_section(self):
        self.assertEqual(
            parse_bcv_rate(BCV_HTML),
            (Decimal("884.74960000"), date(2026, 10, 22)),
        )

    def test_rejects_missing_or_invalid_rate_data(self):
        for html in (
            "<div id='dolar'><strong>884,75</strong></div>",
            BCV_HTML.replace("884,74960000", "no disponible"),
            BCV_HTML.replace("884,74960000", "0,0000"),
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_bcv_rate(html)

    def test_fetches_the_official_page_with_a_timeout(self):
        response = BytesIO(BCV_HTML.encode())
        with patch("bcv_rate.urlopen", return_value=response) as open_url:
            self.assertEqual(
                fetch_bcv_rate(),
                (Decimal("884.74960000"), date(2026, 10, 22)),
            )
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, BCV_RATE_URL)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 5)
        context = open_url.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
