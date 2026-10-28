"""Адаптер Market.CSGO (только чтение, без ключа).

Документация API v2: https://market.dota2.net/docs-v2. У площадки отдельный сайт
для каждой игры с одинаковым API: market.csgo.com (CS2), market.dota2.net (Dota 2)
и rust.tm (Rust). Используются только публичные статические файлы:

* ``/api/v2/prices/RUB.json`` — «Список цен (лучшие предложения)»: минимальная
  цена лота по каждому названию;
* ``/api/v2/full-history/all.json`` — номер истории продаж по названию;
* ``/api/v2/full-history/{id}.json`` — до 6000 последних продаж предмета.

Проверено живыми запросами 27.10.2026 (в документации этого нет или пример другой):

* все три сайта отвечают без ключа; прайс-лист — ``{"success": true, "time": ...,
  "currency": "RUB", "items": [{"market_hash_name", "volume", "price"}]}``,
  ``price`` и ``volume`` — строки, цена в рублях с двумя знаками (``"899.00"``,
  проверено 28.10.2026; в EUR.json цены с тремя знаками);
* ``volume`` в документации не описан. Он не совпадает с числом продаж
  (у Prisma Case 1179 при 1677 продажах за 7 дней), поэтому считается числом лотов;
* файл истории — ``{"time": ..., "data": {..., "history": [...]}}`` (история лежит
  в ``data``, а не на верхнем уровне, как в примере документации); запись
  ``[unix_time, цена_RUB, цена_USD, цена_EUR]``, свежие продажи первыми; каждая
  проданная штука — отдельная запись, массовые покупки дают одинаковое время.

Цены берутся в рублях: валюта аккаунта Market.CSGO выбирается при регистрации,
у пользователя это рубли. В EUR цены пересчитываются по курсу ЦБ РФ
(:mod:`skinedge.sources.cbr`).
Документация грозит удалить ключ за больше чем 5 запросов в секунду; адаптер
делает не больше 2 запросов в секунду.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from skinedge.games import CS2, DOTA2, RUST
from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind, Sale
from skinedge.sources.base import (
    RateLimiter,
    SourceError,
    positive_price,
    request_with_retries,
)

log = logging.getLogger(__name__)

HOSTS = {CS2: "https://market.csgo.com", DOTA2: "https://market.dota2.net", RUST: "https://rust.tm"}
CURRENCY = Currency.RUB
# Номера историй меняются только у новых предметов; all.json обновляется редко.
INDEX_SECONDS = 6 * 3600.0
_PRICE = 1  # индекс цены в рублях в записи истории (RUB, USD, EUR)


def _host(app_id: int) -> str:
    host = HOSTS.get(app_id)
    if host is None:
        raise SourceError(f"Market.CSGO: игра app_id={app_id} не поддерживается")
    return host


class MarketCSGOSource:
    """Цены и продажи Market.CSGO.

    Args:
        client: HTTP-клиент; по умолчанию создаётся свой.
        clock: Монотонные часы для лимита и кеша (для тестов).
        sleep: Функция ожидания (для тестов).
    """

    name = "marketcsgo"

    def __init__(
        self,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or httpx.Client(timeout=60.0)
        self._clock = clock
        self._sleep = sleep
        self._limiter = RateLimiter(2, 1.0, clock=clock, sleep=sleep)
        self._index: dict[int, tuple[float, dict[str, int]]] = {}

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
            raise SourceError(f"Market.CSGO {url}: ответ не JSON") from exc

    def fetch_quotes(self, app_id: int = CS2) -> list[Quote]:
        """Минимальная цена лота по каждому предмету игры, в рублях.

        В прайс-листе только предметы, у которых есть лоты.

        Raises:
            SourceError: Если файл недоступен или ответ не в ожидаемом формате.
        """
        url = f"{_host(app_id)}/api/v2/prices/{CURRENCY.value}.json"
        data = self._get(url)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise SourceError(f"Market.CSGO {url}: ответ без success=true")
        if data.get("currency") != CURRENCY.value:
            raise SourceError(
                f"Market.CSGO {url}: валюта {data.get('currency')!r}, ожидался {CURRENCY.value}"
            )
        rows = data.get("items")
        if not isinstance(rows, list):
            raise SourceError(f"Market.CSGO {url}: нет списка items")
        fetched_at = datetime.now(UTC)
        quotes: list[Quote] = []
        skipped = 0
        for row in rows:
            try:
                name = row["market_hash_name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("пустое название")
                quotes.append(
                    Quote(
                        source=self.name,
                        app_id=app_id,
                        market_hash_name=name,
                        currency=CURRENCY,
                        kind=QuoteKind.AGGREGATE,
                        min_price=positive_price(row["price"]),
                        quantity=int(row["volume"]),
                        fetched_at=fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("Market.CSGO: пропущена строка %r: %s", row, exc)
        if skipped:
            log.warning("Market.CSGO: пропущено %d строк прайс-листа с неверными данными", skipped)
        if rows and not quotes:
            raise SourceError(f"Market.CSGO {url}: ни одной пригодной строки")
        return quotes

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        """Котировки ровно по запрошенным названиям.

        Предмета без лотов нет в прайс-листе; для него возвращается котировка с
        ``min_price=None``, чтобы в базе не осталась прежняя цена исчезнувшего лота.
        """
        quotes = {q.market_hash_name: q for q in self.fetch_quotes(app_id)}
        fetched_at = datetime.now(UTC)
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
                fetched_at=fetched_at,
            )
            for title in dict.fromkeys(titles)
        ]

    def _ids(self, app_id: int) -> dict[str, int]:
        cached = self._index.get(app_id)
        if cached and self._clock() - cached[0] < INDEX_SECONDS:
            return cached[1]
        url = f"{_host(app_id)}/api/v2/full-history/all.json"
        data = self._get(url)
        index = data.get("history") if isinstance(data, dict) else None
        if not isinstance(index, dict):
            raise SourceError(f"Market.CSGO {url}: нет словаря history")
        ids = {
            name: item_id
            for name, item_id in index.items()
            if isinstance(item_id, int) and not isinstance(item_id, bool)
        }
        self._index[app_id] = (self._clock(), ids)
        return ids

    def fetch_last_sales(self, title: str, app_id: int = CS2) -> list[Sale]:
        """Последние продажи предмета (до 6000), свежие первыми, цены в рублях.

        Returns:
            Продажи; пустой список, если у предмета нет истории.

        Raises:
            SourceError: Если файл недоступен или ответ не в ожидаемом формате.
        """
        item_id = self._ids(app_id).get(title)
        if item_id is None:
            return []
        url = f"{_host(app_id)}/api/v2/full-history/{item_id}.json"
        data = self._get(url)
        body = data.get("data") if isinstance(data, dict) else None
        history = body.get("history") if isinstance(body, dict) else None
        if not isinstance(history, list):
            raise SourceError(f"Market.CSGO {url}: нет data.history")
        fetched_at = datetime.now(UTC)
        sales: list[Sale] = []
        skipped = 0
        for row in history:
            try:
                stamp = row[0]
                if isinstance(stamp, bool) or not isinstance(stamp, int):
                    raise ValueError(f"время {stamp!r}")
                sales.append(
                    Sale(
                        source=self.name,
                        app_id=app_id,
                        market_hash_name=title,
                        currency=CURRENCY,
                        price=positive_price(row[_PRICE]),
                        sold_at=datetime.fromtimestamp(stamp, tz=UTC),
                        fetched_at=fetched_at,
                    )
                )
            except (IndexError, TypeError, ValueError, OverflowError, OSError) as exc:
                skipped += 1
                log.debug("Market.CSGO: пропущена продажа %r: %s", row, exc)
        if skipped:
            log.warning("Market.CSGO: %s — пропущено %d продаж с неверными данными", title, skipped)
        return sales
