"""Адаптер Lis-Skins (только чтение, без ключа).

Документация: https://lis-skins.stoplight.io/docs/lis-skins/. Публичный API с ключом
нужен для покупки; SkinEdge использует только публичные выгрузки цен без ключа:
``https://lis-skins.com/market_export_json/{csgo,dota2,rust}.json`` («короткий
список»: одна строка на название, без номеров лотов).

Проверено живыми запросами 27.10.2026 (в документации этого нет):

* все три файла отвечают без ключа, заголовков о лимитах нет; CS2 — около 5 МБ
  и 27 678 названий;
* файл — список ``[{"name", "price", "unlocked_price", "url", "count"}]``,
  цены — числа JSON с двумя знаками; ``url`` — полная ссылка на страницу предмета;
* валюта в файле не указана. Цены в долларах: страница предмета без выбора
  валюты показывает ту же цену со знаком $ («80 offers from $6.44» при
  ``"price": 6.44, "count": 80``);
* по полным спискам Dota 2 и Rust (``api_*_full.json``, по лоту на строку):
  ``price`` — минимальная цена всех лотов, ``count`` — число лотов,
  ``unlocked_price`` — минимальная цена лотов, которые можно вывести сейчас.
  Полные списки были обновлены за 2 минуты до запроса.

Лот с ``unlock_at`` в будущем нельзя вывести в Steam до этой даты, поэтому цена
покупки — ``unlocked_price``. Продаж Lis-Skins API не отдаёт, поэтому Lis-Skins —
только площадка покупки, как CSFloat.
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
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import (
    RateLimiter,
    SourceError,
    positive_price,
    request_with_retries,
)

log = logging.getLogger(__name__)

HOST = "https://lis-skins.com"
GAMES = {CS2: "csgo", DOTA2: "dota2", RUST: "rust"}
CURRENCY = Currency.USD


def _url(app_id: int) -> str:
    game = GAMES.get(app_id)
    if game is None:
        raise SourceError(f"Lis-Skins: игра app_id={app_id} не поддерживается")
    return f"{HOST}/market_export_json/{game}.json"


class LisSkinsSource:
    """Минимальные цены Lis-Skins по названиям.

    Args:
        client: HTTP-клиент; по умолчанию создаётся свой.
        clock: Монотонные часы для лимита (для тестов).
        sleep: Функция ожидания (для тестов).
    """

    name = "lisskins"

    def __init__(
        self,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or httpx.Client(timeout=60.0)
        self._sleep = sleep
        # Лимиты не описаны; за проход нужен один файл на игру.
        self._limiter = RateLimiter(1, 2.0, clock=clock, sleep=sleep)

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
            raise SourceError(f"Lis-Skins {url}: ответ не JSON") from exc

    def fetch_quotes(self, app_id: int = CS2) -> list[Quote]:
        """Минимальная цена лота, который можно вывести сейчас, по каждому названию, в USD.

        Названия, у которых все лоты ещё заблокированы, пропускаются.

        Raises:
            SourceError: Если файл недоступен или ответ не в ожидаемом формате.
        """
        url = _url(app_id)
        rows = self._get(url)
        if not isinstance(rows, list):
            raise SourceError(f"Lis-Skins {url}: ожидался список предметов")
        fetched_at = datetime.now(UTC)
        quotes: list[Quote] = []
        skipped = locked = 0
        for row in rows:
            try:
                if row["unlocked_price"] is None:
                    locked += 1
                    continue
                quotes.append(self._quote(row, app_id, fetched_at))
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                log.debug("Lis-Skins: пропущена строка %r: %s", row, exc)
        if skipped:
            log.warning("Lis-Skins: пропущено %d строк прайс-листа с неверными данными", skipped)
        if locked:
            log.debug("Lis-Skins: %d названий только с заблокированными лотами", locked)
        if rows and not quotes:
            raise SourceError(f"Lis-Skins {url}: ни одной пригодной строки")
        return quotes

    def _quote(self, row: dict[str, Any], app_id: int, fetched_at: datetime) -> Quote:
        name = row["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("пустое название")
        price = positive_price(row["price"])
        unlocked = positive_price(row["unlocked_price"])
        # Минимум по разблокированным лотам не может быть ниже минимума по всем.
        if unlocked < price:
            raise ValueError(f"unlocked_price {unlocked} ниже price {price}")
        count = row["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"число лотов {count!r}")
        link = row.get("url")
        return Quote(
            source=self.name,
            app_id=app_id,
            market_hash_name=name,
            currency=CURRENCY,
            kind=QuoteKind.AGGREGATE,
            min_price=unlocked,
            # count включает и заблокированные лоты: для покупки важно только «есть лот».
            quantity=count,
            url=link if isinstance(link, str) and link.startswith(f"{HOST}/") else None,
            fetched_at=fetched_at,
        )

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        """Котировки ровно по запрошенным названиям.

        Для названия без доступных лотов возвращается котировка с ``min_price=None``,
        чтобы в базе не осталась прежняя цена исчезнувшего лота.
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
