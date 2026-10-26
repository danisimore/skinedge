from decimal import Decimal

import httpx
import pytest

from skinedge.money import Currency
from skinedge.sources import ecb
from skinedge.sources.base import SourceError

XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <gesmes:subject>Reference rates</gesmes:subject>
 <Cube>
  <Cube time='2026-10-26'>
   <Cube currency='USD' rate='1.0812'/>
   <Cube currency='JPY' rate='160.1'/>
  </Cube>
 </Cube>
</gesmes:Envelope>"""


def test_parse() -> None:
    fx = ecb.parse_rates(XML, Decimal("0.01"))
    assert fx.rates == {Currency.USD: Decimal("1.0812")}
    assert fx.as_of is not None and fx.as_of.date().isoformat() == "2026-10-26"
    assert fx.source == "ECB"


def test_missing_usd() -> None:
    with pytest.raises(SourceError, match="USD"):
        ecb.parse_rates(XML.replace("USD", "GBP"), Decimal("0"))


def test_bad_xml() -> None:
    with pytest.raises(SourceError):
        ecb.parse_rates("<oops", Decimal("0"))


def test_fetch() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=XML)))
    assert ecb.fetch_rates(Decimal("0.01"), client).rates[Currency.USD] == Decimal("1.0812")
