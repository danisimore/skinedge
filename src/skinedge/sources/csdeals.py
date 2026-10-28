"""Адаптер CS.Deals (только чтение цен, с ключом).

Документация: https://cs.deals/docs/reference/market-data/overview. Ключ из
настроек аккаунта даёт полный доступ к нему, включая покупку и вывод
криптовалюты, поэтому адаптер вызывает только два метода чтения из
``ALLOWED`` и только ``GET``:

* ``/prices/all?app_id=`` — сводка по всем предметам игры: ``lowest_listing_price``
  (самый дешёвый живой лот, центы USD или ``null``) и ``stock`` (число лотов).
  Сводка пересобирается раз в минуту, ``generated_at`` — момент сборки;
* ``/listings?app_id=&limit=1000`` — лоты, новые первыми, с ``trade_locked_until``
  (лот нельзя вывести в Steam до этого момента).

Проверено живыми запросами 28.10.2026 (в документации этого нет):

* ``lowest_listing_price`` и ``stock`` совпали с минимумом и числом всех лотов
  ``/listings`` у всех предметов с лотами (3 254 в CS2, 2 941 в Dota 2), то есть
  сводка живая;
* сводка считает и лоты под трейд-локом: у 253 предметов CS2 самый дешёвый лот был
  заблокирован (у 200 из них других лотов не было), и сводка показывала его цену;
* у каждого лота ``amount`` = 1; всего в CS2 44 849 лотов (45 страниц), в Dota 2 —
  8 216, в Rust — около 85 тысяч;
* ``id`` лотов строго убывают от страницы к странице. Все заблокированные лоты были
  выставлены не раньше чем за 8 дней до конца лока (максимум ровно 192 часа):
  314 в CS2, все на первой странице, 35 в Dota 2, в Rust ни одного. После первого
  лота старше 7, 8 или 10 дней ни в одной игре не нашлось лота новее этой границы
  (дальше по списку даты идут не по порядку: лоты 2020 года стоят среди лотов 2026-го);
* окно в 9 дней — это 1 страница в CS2, 7 в Dota 2 и 12–13 в Rust.

Поэтому сводка даёт цену, а лоты за последние ``LOCK_WINDOW`` показывают, какие
из них заблокированы. Если заблокированный лот не дороже самого дешёвого в сводке,
цена доступного лота неизвестна, и предмет пропускается: как и у Lis-Skins, цена
покупки — только по лоту, который можно вывести сейчас.

Продаж на CS.Deals мало, поэтому это только площадка покупки.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from skinedge.games import CS2, DOTA2, RUST
from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import RateLimiter, SourceError, request_with_retries

log = logging.getLogger(__name__)

API = "https://api.cs.deals/public/v1"
# Только чтение рыночных данных: ключ умеет покупать и выводить деньги.
ALLOWED = frozenset({"/prices/all", "/listings"})
GAMES = frozenset({CS2, DOTA2, RUST})
CURRENCY = Currency.USD
PAGE = 1000
# Лок заканчивается не позже чем через 8 дней после выставления лота; день запаса.
LOCK_WINDOW = timedelta(days=9)
MAX_PAGES = 30


class CSDealsSource:
    """Минимальные цены лотов CS.Deals, которые можно вывести сейчас.

    Args:
        api_key: Ключ из настроек CS.Deals (Settings → API).
        client: HTTP-клиент; по умолчанию создаётся свой.
        clock: Монотонные часы для лимита (для тестов).
        sleep: Функция ожидания (для тестов).
        now: Текущее время UTC (для тестов).
    """

    name = "csdeals"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not api_key:
            raise ValueError("Нужен ключ CS.Deals API")
        self._key = api_key
        self.client = client or httpx.Client(timeout=60.0)
        self._sleep = sleep
        self._now = now
        # /listings — 1 запрос в секунду на ключ, /prices/all — 30 в минуту.
        self._limiter = RateLimiter(1, 1.25, clock=clock, sleep=sleep)

    def _get(self, path: str, params: dict[str, str]) -> Any:
        if path not in ALLOWED:
            raise SourceError(f"CS.Deals: метод {path} не разрешён")
        response = request_with_retries(
            self.client,
            "GET",
            f"{API}{path}",
            limiter=self._limiter,
            sleep=self._sleep,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "SkinEdge/0.1 (+price monitor)",
                "Authorization": f"Bearer {self._key}",
            },
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"CS.Deals {path}: ответ не JSON") from exc

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        """Котировки ровно по запрошенным названиям, в USD.

        Для названия без доступных лотов (нет лотов или самый дешёвый под
        трейд-локом) возвращается котировка с ``min_price=None``, чтобы в базе не
        осталась прежняя цена.

        Raises:
            SourceError: Если ответ не в ожидаемом формате или не удалось
                просмотреть лоты за ``LOCK_WINDOW``.
        """
        if app_id not in GAMES:
            raise SourceError(f"CS.Deals: игра app_id={app_id} не поддерживается")
        fetched_at, summary = self._summary(app_id)
        locked = self._locked(app_id)
        quotes = []
        hidden = 0
        for title in dict.fromkeys(titles):
            price, stock = summary.get(title, (None, 0))
            blocked = locked.get(title, [])
            if price is not None and any(p <= price for p in blocked):
                # Самый дешёвый лот может быть заблокирован: цену доступного не знаем.
                hidden += 1
                price = None
            quotes.append(
                Quote(
                    source=self.name,
                    app_id=app_id,
                    market_hash_name=title,
                    currency=CURRENCY,
                    kind=QuoteKind.AGGREGATE,
                    min_price=price,
                    quantity=max(1, stock - len(blocked)) if price is not None else 0,
                    fetched_at=fetched_at,
                )
            )
        if hidden:
            log.info("CS.Deals: у %d предметов самый дешёвый лот под трейд-локом", hidden)
        return quotes

    def _summary(self, app_id: int) -> tuple[datetime, dict[str, tuple[Decimal, int]]]:
        """Момент сборки сводки и название → (цена самого дешёвого лота, число лотов)."""
        data = self._get("/prices/all", {"app_id": str(app_id)})
        if not isinstance(data, dict) or not isinstance(data.get("prices"), list):
            raise SourceError("CS.Deals /prices/all: ожидался объект со списком prices")
        fetched_at = _time(data.get("generated_at"), "generated_at")
        offers: dict[str, tuple[Decimal, int]] = {}
        skipped = 0
        for row in data["prices"]:
            try:
                parsed = _offer(row)
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("CS.Deals: пропущена строка %r: %s", row, exc)
                continue
            if parsed is None:
                continue
            name, price, stock = parsed
            seen = offers.get(name)
            if seen is not None:
                price, stock = min(seen[0], price), seen[1] + stock
            offers[name] = (price, stock)
        if skipped:
            log.warning("CS.Deals: пропущено %d строк сводки с неверными данными", skipped)
        if data["prices"] and not offers and skipped:
            raise SourceError("CS.Deals /prices/all: ни одной пригодной строки")
        return fetched_at, offers

    def _locked(self, app_id: int) -> dict[str, list[Decimal]]:
        """Цены заблокированных лотов по названиям.

        Лоты идут от новых к старым; страницы читаются, пока не начнутся лоты
        старше ``LOCK_WINDOW``: среди них заблокированных не бывает.
        """
        now = self._now()
        since = now - LOCK_WINDOW
        locked: dict[str, list[Decimal]] = defaultdict(list)
        cursor: int | None = None
        for _ in range(MAX_PAGES):
            params = {"limit": str(PAGE), "app_id": str(app_id)}
            if cursor is not None:
                params["cursor"] = str(cursor)
            data = self._get("/listings", params)
            if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
                raise SourceError("CS.Deals /listings: ожидался объект со списком listings")
            oldest: datetime | None = None
            for row in data["listings"]:
                name, price, created, until = _listing(row)
                oldest = created if oldest is None else min(oldest, created)
                if until is not None and until > now:
                    locked[name].append(price)
            cursor = data.get("next_cursor")
            if cursor is None or (oldest is not None and oldest < since):
                return locked
            if isinstance(cursor, bool) or not isinstance(cursor, int):
                raise SourceError(f"CS.Deals /listings: next_cursor {cursor!r}")
        raise SourceError(
            f"CS.Deals /listings: за {MAX_PAGES} страниц не дошёл до лотов старше "
            f"{LOCK_WINDOW.days} дней"
        )


def _cents(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"цена в центах {value!r}")
    return Decimal(value).scaleb(-2)


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"CS.Deals: нет даты {field}")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"CS.Deals: неверная дата {field}={value!r}") from exc
    if moment.tzinfo is None:
        raise SourceError(f"CS.Deals: дата {field} без часового пояса")
    return moment


def _offer(row: dict[str, Any]) -> tuple[str, Decimal, int] | None:
    """Название, цена и число лотов из строки сводки; ``None`` — лотов нет."""
    name = row["market_hash_name"]
    if not isinstance(name, str) or not name:
        raise ValueError("пустое название")
    if row["lowest_listing_price"] is None:
        return None
    price = _cents(row["lowest_listing_price"])
    stock = row["stock"]
    if isinstance(stock, bool) or not isinstance(stock, int) or stock < 1:
        raise ValueError(f"число лотов {stock!r} при цене {price}")
    return name, price, stock


def _listing(row: dict[str, Any]) -> tuple[str, Decimal, datetime, datetime | None]:
    """Название, цена, время выставления и конец трейд-лока лота.

    Raises:
        SourceError: Если лот не в ожидаемом формате: пропустить его нельзя,
            иначе заблокированный лот сойдёт за доступный.
    """
    try:
        name = row["market_hash_name"]
        if not isinstance(name, str) or not name:
            raise ValueError("пустое название")
        price = _cents(row["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"CS.Deals /listings: неверный лот {row!r:.200}") from exc
    created = _time(row.get("created_at"), "created_at")
    until = row.get("trade_locked_until")
    return name, price, created, None if until is None else _time(until, "trade_locked_until")
