"""Адаптер White.market (только чтение, без ключа).

Документация для партнёров: https://api.white.market/docs_partner/5-skins-buy/prices.html.
Остальной API требует токен партнёра; SkinEdge использует только публичную
выгрузку «Get all at the lowest price» без ключа:
``https://export.white.market/v1/prices/730.json`` и её описание
``730.json.meta.json``.

По документации: ``price`` — цена в USD, ``market_product_count`` — число лотов,
``market_product_link`` — ссылка на самый дешёвый лот; ключ строки — пара
(``market_hash_name``, ``paint_index``), фазы Doppler идут отдельными строками.
Выгрузка — снимок, а не поток: ``data_as_of`` в описании говорит, на какой момент
цены.

Проверено живыми запросами 28.10.2026 (в документации этого нет):

* выгрузка — список из 20 829 строк (около 10 МБ), ``price`` — строка с тремя
  знаками (``"26.160"``), цена и ссылка есть у каждой строки;
* в ``market_product_link`` амперсанды экранированы как в HTML (``&amp;``);
* одно название встречается в нескольких строках (624 строки): фазы Doppler и
  другие варианты. Сканер сравнивает по названию, поэтому берётся самая дешёвая
  строка, а лоты складываются;
* описание — ``{"file": "730.json", "data_as_of", "generated_at", "items"}``;
* выгрузки есть только для CS2: для Dota 2 (570) и Rust (252490) ответ 404.

Продаж в выгрузке нет, поэтому White.market — только площадка покупки, как Lis-Skins.
"""

from __future__ import annotations

import html
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from skinedge.games import CS2
from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import (
    RateLimiter,
    SourceError,
    positive_price,
    request_with_retries,
)

log = logging.getLogger(__name__)

EXPORT = "https://export.white.market/v1/prices"
SITE = "https://white.market/"
GAMES = frozenset({CS2})
CURRENCY = Currency.USD


def _file(app_id: int) -> str:
    if app_id not in GAMES:
        raise SourceError(f"White.market: игра app_id={app_id} не поддерживается")
    return f"{app_id}.json"


class WhiteMarketSource:
    """Минимальные цены White.market по названиям (только CS2).

    Args:
        client: HTTP-клиент; по умолчанию создаётся свой.
        clock: Монотонные часы для лимита (для тестов).
        sleep: Функция ожидания (для тестов).
    """

    name = "whitemarket"

    def __init__(
        self,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or httpx.Client(timeout=60.0)
        self._sleep = sleep
        # Лимиты не описаны; за проход нужны два файла.
        self._limiter = RateLimiter(1, 1.0, clock=clock, sleep=sleep)

    def _get(self, url: str) -> Any:
        response = request_with_retries(
            self.client,
            "GET",
            url,
            limiter=self._limiter,
            sleep=self._sleep,
            headers={"Accept": "application/json", "User-Agent": "SkinEdge/0.1 (+price monitor)"},
        )
        try:
            return json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise SourceError(f"White.market {url}: ответ не JSON") from exc

    def _as_of(self, file: str) -> datetime:
        """Момент, на который собраны цены выгрузки."""
        url = f"{EXPORT}/{file}.meta.json"
        meta = self._get(url)
        if not isinstance(meta, dict) or meta.get("file") != file:
            raise SourceError(f"White.market {url}: описание не к файлу {file}")
        try:
            as_of = datetime.fromisoformat(meta["data_as_of"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"White.market {url}: нет даты data_as_of") from exc
        if as_of.tzinfo is None:
            raise SourceError(f"White.market {url}: data_as_of без часового пояса")
        return as_of

    def fetch_quotes(self, app_id: int = CS2) -> list[Quote]:
        """Минимальная цена лота по каждому названию, в USD.

        Время котировок — ``data_as_of`` из описания выгрузки: устаревшие цены
        сканер отбросит по возрасту.

        Raises:
            SourceError: Если файл недоступен или ответ не в ожидаемом формате.
        """
        return self._snapshot(app_id)[1]

    def _snapshot(self, app_id: int) -> tuple[datetime, list[Quote]]:
        file = _file(app_id)
        # Описание раньше файла: если выгрузку обновят между запросами, цены
        # окажутся свежее записанного времени, а не наоборот.
        as_of = self._as_of(file)
        url = f"{EXPORT}/{file}"
        rows = self._get(url)
        if not isinstance(rows, list):
            raise SourceError(f"White.market {url}: ожидался список предметов")
        # Название → (цена, лоты, ссылка): самый дешёвый вариант, лоты всех вариантов.
        offers: dict[str, tuple[Decimal, int, str | None]] = {}
        skipped = 0
        for row in rows:
            try:
                name, price, count, link = _parse(row)
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("White.market: пропущена строка %r: %s", row, exc)
                continue
            seen = offers.get(name)
            if seen is None:
                offers[name] = (price, count, link)
            elif price < seen[0]:
                offers[name] = (price, seen[1] + count, link)
            else:
                offers[name] = (seen[0], seen[1] + count, seen[2])
        if skipped:
            log.warning("White.market: пропущено %d строк выгрузки с неверными данными", skipped)
        if rows and not offers:
            raise SourceError(f"White.market {url}: ни одной пригодной строки")
        quotes = [
            Quote(
                source=self.name,
                app_id=app_id,
                market_hash_name=name,
                currency=CURRENCY,
                kind=QuoteKind.AGGREGATE,
                min_price=price,
                quantity=count,
                url=link,
                fetched_at=as_of,
            )
            for name, (price, count, link) in offers.items()
        ]
        return as_of, quotes

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        """Котировки ровно по запрошенным названиям.

        Для названия без лотов возвращается котировка с ``min_price=None``,
        чтобы в базе не осталась прежняя цена исчезнувшего лота.
        """
        as_of, found = self._snapshot(app_id)
        quotes = {q.market_hash_name: q for q in found}
        return [
            quotes.get(title)
            or Quote(
                source=self.name,
                app_id=app_id,
                market_hash_name=title,
                currency=CURRENCY,
                kind=QuoteKind.AGGREGATE,
                min_price=None,
                quantity=0,
                fetched_at=as_of,
            )
            for title in dict.fromkeys(titles)
        ]


def _parse(row: dict[str, Any]) -> tuple[str, Decimal, int, str | None]:
    """Название, цена, число лотов и ссылка из строки выгрузки."""
    name = row["market_hash_name"]
    if not isinstance(name, str) or not name:
        raise ValueError("пустое название")
    price = positive_price(row["price"])
    count = row["market_product_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"число лотов {count!r}")
    link = row.get("market_product_link")
    url = html.unescape(link) if isinstance(link, str) else None
    return name, price, count, url if url and url.startswith(SITE) else None
