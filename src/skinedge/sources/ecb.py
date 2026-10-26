"""Официальные курсы ЕЦБ (euro foreign exchange reference rates).

Файл https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml публичный,
обновляется в рабочие дни около 16:00 CET и содержит курс валюты за 1 EUR.
Это справочный курс: курс площадки или банка хуже, поэтому поверх него
:class:`skinedge.fx.FxRates` применяет спред.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from skinedge.fx import FxRates
from skinedge.money import Currency
from skinedge.sources.base import RateLimiter, SourceError, request_with_retries

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def parse_rates(xml: str, spread: Decimal) -> FxRates:
    """Разбирает файл ЕЦБ.

    Args:
        xml: Содержимое eurofxref-daily.xml.
        spread: Спред конвертации.

    Returns:
        Курсы поддерживаемых валют с датой публикации.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError(f"ЕЦБ: неверный XML: {exc}") from exc
    rates: dict[Currency, Decimal] = {}
    as_of: datetime | None = None
    for cube in root.iter():
        if not cube.tag.endswith("Cube"):
            continue
        if "time" in cube.attrib:
            as_of = datetime.fromisoformat(cube.attrib["time"]).replace(tzinfo=UTC)
        code = cube.attrib.get("currency")
        if code in {c.value for c in Currency} and code != Currency.EUR.value:
            try:
                rates[Currency(code)] = Decimal(cube.attrib["rate"])
            except (KeyError, InvalidOperation) as exc:
                raise SourceError(f"ЕЦБ: неверный курс {code}") from exc
    missing = {c for c in Currency if c is not Currency.EUR} - rates.keys()
    if missing:
        raise SourceError(f"ЕЦБ: нет курса {', '.join(sorted(missing))}")
    return FxRates(rates=rates, spread=spread, as_of=as_of, source="ECB")


def fetch_rates(spread: Decimal, client: httpx.Client | None = None) -> FxRates:
    """Загружает текущие курсы ЕЦБ."""
    own = client is None
    http = client or httpx.Client(timeout=30)
    try:
        response = request_with_retries(http, "GET", URL, limiter=RateLimiter(1, 1.0))
    finally:
        if own:
            http.close()
    if response.status_code != 200:
        raise SourceError(f"ЕЦБ: HTTP {response.status_code}")
    return parse_rates(response.text, spread)
