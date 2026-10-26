"""Адаптер Skinport API.

Документация: https://docs.skinport.com/items и https://docs.skinport.com/sales/history.
Оба endpoint'а публичные (без ключа), требуют ``Accept-Encoding: br``,
ограничены 8 запросами за 5 минут и кешируются на стороне Skinport 5 минут.

``/v1/items`` отдаёт сводку по названию предмета: минимальную, максимальную,
среднюю и медианную цену предложений и количество лотов. Конкретных лотов,
float и фаз в ответе нет, поэтому котировки Skinport имеют тип ``AGGREGATE``.
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

from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind, SalesStats, SalesWindow
from skinedge.sources.base import RateLimiter, SourceError, request_with_retries

log = logging.getLogger(__name__)

BASE_URL = "https://api.skinport.com/v1"
CACHE_SECONDS = 300.0
_WINDOWS = {
    "last_24_hours": SalesWindow.H24,
    "last_7_days": SalesWindow.D7,
    "last_30_days": SalesWindow.D30,
    "last_90_days": SalesWindow.D90,
}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise ValueError(f"ожидалось число, получено {value!r}")


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


class SkinportSource:
    """Источник цен Skinport.

    Args:
        client: HTTP-клиент; по умолчанию создаётся свой.
        currency: Валюта цен.
        clock: Источник времени для кеша (для тестов).
        sleep: Функция ожидания для лимита и повторов (для тестов).
    """

    name = "skinport"

    def __init__(
        self,
        client: httpx.Client | None = None,
        currency: Currency = Currency.EUR,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or httpx.Client(timeout=60.0)
        self.currency = currency
        self._clock = clock
        self._sleep = sleep
        # Лимит у каждого endpoint'а свой: 8 запросов за 5 минут.
        self._limiters = {
            path: RateLimiter(8, 300.0, clock=clock, sleep=sleep)
            for path in ("/items", "/sales/history")
        }
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}

    def _get(self, path: str, params: dict[str, str]) -> Any:
        key = (path, tuple(sorted(params.items())))
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        response = request_with_retries(
            self.client,
            "GET",
            f"{BASE_URL}{path}",
            limiter=self._limiters[path],
            sleep=self._sleep,
            params=params,
            headers={"Accept-Encoding": "br", "User-Agent": "SkinEdge/0.1 (+price monitor)"},
        )
        try:
            data = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise SourceError(f"Skinport {path}: ответ не JSON") from exc
        if not isinstance(data, list):
            raise SourceError(f"Skinport {path}: ожидался список, получено {type(data).__name__}")
        self._cache[key] = (self._clock(), data)
        return data

    def fetch_quotes(self, app_id: int = 730) -> list[Quote]:
        """Сводные котировки по всем предметам игры, только лоты, доступные к обмену.

        Args:
            app_id: Steam app_id игры.

        Returns:
            Котировки. Предметы без лотов возвращаются с ``min_price=None``.
        """
        params = {"app_id": str(app_id), "currency": self.currency.value, "tradable": "1"}
        rows = self._get("/items", params)
        fetched_at = datetime.now(UTC)
        quotes: list[Quote] = []
        skipped = 0
        for row in rows:
            try:
                if row["currency"] != self.currency.value:
                    raise ValueError(f"валюта {row['currency']}")
                quotes.append(
                    Quote(
                        source=self.name,
                        app_id=app_id,
                        market_hash_name=row["market_hash_name"],
                        currency=self.currency,
                        kind=QuoteKind.AGGREGATE,
                        min_price=_decimal(row.get("min_price")),
                        median_price=_decimal(row.get("median_price")),
                        quantity=int(row.get("quantity") or 0),
                        url=row.get("item_page") or row.get("market_page"),
                        source_updated_at=_timestamp(row.get("updated_at")),
                        fetched_at=fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("Skinport: пропущена строка %r: %s", row, exc)
        if skipped:
            log.warning(
                "Skinport /items: пропущено %d некорректных строк из %d", skipped, len(rows)
            )
        if rows and not quotes:
            raise SourceError("Skinport /items: ни одной корректной строки")
        return quotes

    def fetch_sales(self, app_id: int = 730, names: list[str] | None = None) -> list[SalesStats]:
        """Статистика продаж за 24 часа, 7, 30 и 90 дней.

        Args:
            app_id: Steam app_id игры.
            names: Названия предметов; ``None`` значит все предметы.

        Returns:
            По одной записи на предмет, вариант и окно.
        """
        params = {"app_id": str(app_id), "currency": self.currency.value}
        if names:
            params["market_hash_name"] = ",".join(names)
        rows = self._get("/sales/history", params)
        fetched_at = datetime.now(UTC)
        stats: list[SalesStats] = []
        skipped = 0
        for row in rows:
            try:
                for field, window in _WINDOWS.items():
                    block = row.get(field) or {}
                    stats.append(
                        SalesStats(
                            source=self.name,
                            app_id=app_id,
                            market_hash_name=row["market_hash_name"],
                            version=row.get("version"),
                            currency=self.currency,
                            window=window,
                            min=_decimal(block.get("min")),
                            max=_decimal(block.get("max")),
                            avg=_decimal(block.get("avg")),
                            median=_decimal(block.get("median")),
                            volume=int(block.get("volume") or 0),
                            fetched_at=fetched_at,
                        )
                    )
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("Skinport: пропущена строка %r: %s", row, exc)
        if skipped:
            log.warning("Skinport /sales/history: пропущено %d строк из %d", skipped, len(rows))
        return stats
