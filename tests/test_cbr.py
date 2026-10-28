from decimal import Decimal

import httpx
import pytest

from skinedge.money import Currency
from skinedge.sources import cbr
from skinedge.sources.base import SourceError

# Формат как в живом ответе 28.10.2026 (кодировка windows-1251).
TEXT = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="28.10.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>\
<Name>Доллар США</Name><Value>80,5307</Value><VunitRate>80,5307</VunitRate></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal>\
<Name>Евро</Name><Value>90,0977</Value><VunitRate>90,0977</VunitRate></Valute>
</ValCurs>"""
XML = TEXT.encode("cp1251")


def test_parse() -> None:
    fx = cbr.parse_rates(XML, Decimal("0.01"))
    assert fx.rates == {Currency.RUB: Decimal("90.0977")}
    assert fx.as_of is not None and fx.as_of.date().isoformat() == "2026-10-28"
    assert fx.source == "ЦБ РФ" and fx.spread == Decimal("0.01")


def test_nominal() -> None:
    xml = TEXT.replace(
        "<Nominal>1</Nominal><Name>Евро</Name><Value>90,0977",
        "<Nominal>10</Nominal><Name>Евро</Name><Value>900,977",
    )
    assert "900,977" in xml
    assert cbr.parse_rates(xml.encode("cp1251"), Decimal(0)).rates[Currency.RUB] == Decimal(
        "90.0977"
    )


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("<CharCode>EUR</CharCode>", "<CharCode>GBP</CharCode>", "нет курса EUR"),
        ("<Value>90,0977</Value>", "<Value>abc</Value>", "Value"),
        ("<Value>90,0977</Value>", "<Value>0,0000</Value>", "Value"),
        ('Date="28.10.2026"', 'Date="2026-10-28"', "даты"),
    ],
)
def test_bad_file(old: str, new: str, match: str) -> None:
    assert old in TEXT
    with pytest.raises(SourceError, match=match):
        cbr.parse_rates(TEXT.replace(old, new).encode("cp1251"), Decimal(0))


def test_bad_xml() -> None:
    with pytest.raises(SourceError):
        cbr.parse_rates(b"<oops", Decimal(0))


def test_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == cbr.URL
        return httpx.Response(
            200, content=XML, headers={"content-type": "application/xml; charset=windows-1251"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert cbr.fetch_rates(Decimal("0.01"), client).rates[Currency.RUB] == Decimal("90.0977")
    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(SourceError):
        cbr.fetch_rates(Decimal("0.01"), failing)
