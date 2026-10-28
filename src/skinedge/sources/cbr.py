"""Официальный курс рубля ЦБ РФ.

ЕЦБ курс рубля не публикует, поэтому рубли (цены Market.CSGO) пересчитываются в
EUR по официальному курсу ЦБ РФ. Файл https://www.cbr.ru/scripts/XML_daily.asp
публичный, без ключа, обновляется в рабочие дни. Проверено живым запросом
28.10.2026: ``<ValCurs Date="28.10.2026">`` со списком ``<Valute>``, у каждой
``CharCode``, ``Nominal`` и ``Value`` (рублей за ``Nominal`` единиц, десятичная
запятая: ``90,0977``), кодировка windows-1251.

Как и у ЕЦБ, это справочный курс: курс банка или площадки хуже, поэтому поверх
него :class:`skinedge.fx.FxRates` применяет спред.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from skinedge.fx import FxRates
from skinedge.money import Currency
from skinedge.sources.base import RateLimiter, SourceError, request_with_retries

URL = "https://www.cbr.ru/scripts/XML_daily.asp"
SOURCE = "ЦБ РФ"


def _number(text: str | None, what: str) -> Decimal:
    try:
        value = Decimal((text or "").strip().replace(",", "."))
    except InvalidOperation as exc:
        raise SourceError(f"ЦБ РФ: неверное значение {what}: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise SourceError(f"ЦБ РФ: неверное значение {what}: {text!r}")
    return value


def parse_rates(xml: bytes, spread: Decimal) -> FxRates:
    """Разбирает файл ЦБ РФ.

    Args:
        xml: Содержимое XML_daily.asp как есть (кодировку указывает сам файл).
        spread: Спред конвертации.

    Returns:
        Курс рубля за 1 EUR с датой курса.

    Raises:
        SourceError: Если файл не разобрать или в нём нет курса евро.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceError(f"ЦБ РФ: неверный XML: {exc}") from exc
    if root.tag != "ValCurs":
        raise SourceError(f"ЦБ РФ: неожиданный корень {root.tag!r}")
    try:
        as_of = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").replace(tzinfo=UTC)
    except (KeyError, ValueError) as exc:
        raise SourceError("ЦБ РФ: нет даты курса") from exc
    for valute in root.iter("Valute"):
        if (valute.findtext("CharCode") or "").strip() != Currency.EUR.value:
            continue
        nominal = _number(valute.findtext("Nominal"), "Nominal")
        value = _number(valute.findtext("Value"), "Value")
        rate = value / nominal
        return FxRates(rates={Currency.RUB: rate}, spread=spread, as_of=as_of, source=SOURCE)
    raise SourceError("ЦБ РФ: нет курса EUR")


def fetch_rates(spread: Decimal, client: httpx.Client | None = None) -> FxRates:
    """Загружает текущий курс ЦБ РФ."""
    own = client is None
    http = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        response = request_with_retries(http, "GET", URL, limiter=RateLimiter(1, 1.0))
    finally:
        if own:
            http.close()
    if response.status_code != 200:
        raise SourceError(f"ЦБ РФ: HTTP {response.status_code}")
    return parse_rates(response.content, spread)
