"""Адаптер CSFloat Market API (только чтение).

Документация: https://docs.csfloat.com/. Используется один метод: список лотов
``GET https://csfloat.com/api/v1/listings`` с фильтром по ``market_hash_name`` и
сортировкой ``lowest_price``. Ключ передаётся в заголовке ``Authorization``.

Проверено живым запросом 27.10.2026 (в документации этого нет):

* без ключа ответ 403;
* ответ — объект ``{"cursor": ..., "data": [лоты]}``;
* ``price`` — целые центы USD (2380 = $23.80);
* лимит 200 запросов в час: заголовки ``x-ratelimit-limit``,
  ``x-ratelimit-remaining`` и ``x-ratelimit-reset`` (Unix-время сброса).

Истории продаж в публичной документации нет, поэтому CSFloat используется только
как площадка покупки: продажа оценивается по реальным сделкам других площадок.
Часть часового лимита адаптер оставляет в запасе и при его исчерпании прекращает
запросы до следующего прохода, а не ждёт сброса.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import RateLimiter, SourceError

log = logging.getLogger(__name__)

URL = "https://csfloat.com/api/v1/listings"
RESERVE = 20  # запросов из часового лимита, которые не тратим


class CSFloatSource:
    """Самые дешёвые лоты CSFloat.

    Args:
        api_key: Ключ из профиля CSFloat (вкладка Developers).
        client: HTTP-клиент; по умолчанию создаётся свой.
        sleep: Функция ожидания (для тестов).
        clock: Монотонные часы для паузы между запросами (для тестов).
        wall: Unix-время для сброса лимита (для тестов).
        reserve: Сколько запросов часового лимита оставлять в запасе.
    """

    name = "csfloat"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        reserve: int = RESERVE,
    ) -> None:
        if not api_key:
            raise ValueError("Нужен ключ CSFloat API")
        self._key = api_key
        self.client = client or httpx.Client(timeout=30.0)
        self._limiter = RateLimiter(1, 1.0, clock=clock, sleep=sleep)
        self._wall = wall
        self.reserve = reserve
        self.remaining: int | None = None  # последнее x-ratelimit-remaining
        self.reset_at: float | None = None  # последнее x-ratelimit-reset

    def budget_left(self) -> bool:
        """Можно ли ещё делать запросы, не трогая запас лимита."""
        if self.remaining is None or self.remaining > self.reserve:
            return True
        return self.reset_at is not None and self._wall() >= self.reset_at

    def fetch_lowest(self, names: list[str], app_id: int = 730) -> list[Quote]:
        """Самый дешёвый лот «купить сейчас» по каждому названию.

        Ошибка или исчерпанный лимит останавливают запросы: возвращается то, что
        успели получить. Неверный ключ — ошибка.

        Args:
            names: Точные названия предметов (market_hash_name).
            app_id: Только 730 (CS2).

        Returns:
            Котировки; название без лотов приходит с ``min_price=None``.

        Raises:
            SourceError: Если ключ не принят или игра не поддерживается.
        """
        if app_id != 730:
            raise SourceError(f"CSFloat: поддерживается только CS2, а не app_id={app_id}")
        quotes: list[Quote] = []
        for name in names:
            if not self.budget_left():
                log.warning(
                    "CSFloat: в часовом лимите осталось %s запросов, остальные %d предметов "
                    "проверю в следующих проходах",
                    self.remaining,
                    len(names) - len(quotes),
                )
                break
            try:
                rows = self._listings(name)
            except _Stop as exc:
                log.warning("CSFloat: запросы остановлены (%s)", exc)
                break
            quotes.append(self._quote(name, rows, app_id))
        return quotes

    def _listings(self, name: str) -> list[Any]:
        self._limiter.acquire()
        params = {
            "market_hash_name": name,
            "sort_by": "lowest_price",
            "type": "buy_now",
            "limit": "1",
        }
        headers = {
            "Authorization": self._key,
            "Accept": "application/json",
            "User-Agent": "SkinEdge/0.1 (+price monitor)",
        }
        try:
            response = self.client.get(URL, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise _Stop(f"сетевая ошибка {type(exc).__name__}") from exc
        self._track(response.headers)
        if response.status_code in (401, 403):
            raise SourceError(f"CSFloat: ключ API не принят (HTTP {response.status_code})")
        if response.status_code == 429:
            raise _Stop("HTTP 429, лимит запросов исчерпан")
        if response.status_code != 200:
            raise _Stop(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise _Stop("ответ не JSON") from exc
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise _Stop("в ответе нет списка data")
        return rows

    def _track(self, headers: httpx.Headers) -> None:
        remaining = _int(headers.get("x-ratelimit-remaining"))
        reset_at = _int(headers.get("x-ratelimit-reset"))
        if remaining is not None:
            self.remaining = remaining
        if reset_at is not None:
            self.reset_at = float(reset_at)

    def _quote(self, name: str, rows: list[Any], app_id: int) -> Quote:
        fetched_at = datetime.now(UTC)
        price: Decimal | None = None
        listing_id: str | None = None
        for row in rows:
            try:
                if row["item"]["market_hash_name"] != name or row.get("type") != "buy_now":
                    continue
                cents = row["price"]
                if not isinstance(cents, int) or cents <= 0:
                    raise ValueError(f"цена {cents!r}")
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("CSFloat: пропущен лот %s: %s", name, exc)
                continue
            price = Decimal(cents) / 100
            listing_id = str(row.get("id") or "") or None
            break
        return Quote(
            source=self.name,
            app_id=app_id,
            market_hash_name=name,
            currency=Currency.USD,
            kind=QuoteKind.LISTING,
            min_price=price,
            quantity=0 if price is None else 1,
            listing_id=listing_id,
            fetched_at=fetched_at,
        )


def _int(value: str | None) -> int | None:
    try:
        return None if value is None else int(value)
    except ValueError:
        return None


class _Stop(Exception):
    """Запросы к CSFloat в этом проходе нужно прекратить."""
