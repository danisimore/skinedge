"""Адаптер DMarket Trading API.

Документация: https://docs.dmarket.com/v1/swagger.html.
Все используемые endpoint'ы требуют подписи запроса ключами пользователя:

* ``X-Api-Key`` — публичный ключ (hex, нижний регистр);
* ``X-Sign-Date`` — Unix-время в секундах, не старше 2 минут;
* ``X-Request-Sign`` — ``"dmar ed25519 " + hex(подпись)`` строки
  ``METHOD + путь_с_query + тело + X-Sign-Date``.

SkinEdge вызывает только методы чтения: лучшие цены и заявки, историю продаж
и комиссии. Методов покупки и продажи в адаптере нет.

Неподтверждённое (проверяется при первом живом запуске):

* единицы ``Amount`` в ``aggregated-prices`` (считаем долларами в виде строки "12.34");
* единицы ``minAmount`` в ``customized-fees`` (пока не учитываем, только логируем);
* численные лимиты запросов не опубликованы, поэтому берём 1 запрос в секунду.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from nacl.bindings import crypto_sign, crypto_sign_seed_keypair

from skinedge.fees.models import FeeKind, FeeRule, Operation, Verification
from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind, Sale
from skinedge.sources.base import RateLimiter, SourceError, request_with_retries

log = logging.getLogger(__name__)

BASE_URL = "https://api.dmarket.com"
GAME_IDS = {730: "a8db", 570: "9a92", 252490: "rust", 440: "tf2"}
DOCS_URL = "https://docs.dmarket.com/v1/swagger.html"
SIGN_PREFIX = "dmar ed25519 "
TITLES_PER_REQUEST = 100


class DMarketSigner:
    """Подписывает запросы DMarket ключом Ed25519.

    Args:
        public_key: Публичный ключ (hex).
        secret_key: Секретный ключ (hex): 64 байта (seed + публичный ключ)
            или 32 байта seed.
        clock: Источник Unix-времени (для тестов).
    """

    def __init__(
        self, public_key: str, secret_key: str, clock: Callable[[], float] = time.time
    ) -> None:
        try:
            secret = bytes.fromhex(secret_key.strip())
            public = bytes.fromhex(public_key.strip())
        except ValueError as exc:
            raise ValueError("Ключи DMarket должны быть в hex") from exc
        if len(secret) == 32:
            derived_public, secret = crypto_sign_seed_keypair(secret)
        elif len(secret) == 64:
            derived_public = secret[32:]
        else:
            raise ValueError("Секретный ключ DMarket должен быть 32 или 64 байта")
        if derived_public != public:
            raise ValueError("Публичный ключ DMarket не соответствует секретному")
        self.public_key = public.hex()
        self._secret = secret
        self._clock = clock

    def headers(self, method: str, path_with_query: str, body: str = "") -> dict[str, str]:
        """Заголовки авторизации для одного запроса.

        Args:
            method: HTTP-метод в верхнем регистре.
            path_with_query: Путь с query-строкой ровно в том виде, в каком он уходит.
            body: Тело запроса ровно в том виде, в каком оно уходит.

        Returns:
            Заголовки ``X-Api-Key``, ``X-Sign-Date`` и ``X-Request-Sign``.
        """
        timestamp = str(int(self._clock()))
        message = f"{method.upper()}{path_with_query}{body}{timestamp}".encode()
        signature = crypto_sign(message, self._secret)[:64].hex()
        return {
            "X-Api-Key": self.public_key,
            "X-Sign-Date": timestamp,
            "X-Request-Sign": SIGN_PREFIX + signature,
        }


def _amount(value: Any) -> Decimal | None:
    """Разбирает сумму DMarket. Сейчас ожидается строка в долларах, например "12.34"."""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"не число: {value!r}") from exc
    if amount < 0:
        raise ValueError(f"отрицательная сумма: {value!r}")
    return amount


def _price_obj(obj: Any) -> Decimal | None:
    if not obj:
        return None
    currency = obj.get("Currency") or obj.get("currency")
    if currency and currency != "USD":
        raise ValueError(f"валюта {currency}")
    return _amount(obj.get("Amount", obj.get("amount")))


class DMarketSource:
    """Источник цен DMarket.

    Args:
        signer: Подписчик запросов с ключами пользователя.
        client: HTTP-клиент; по умолчанию создаётся свой.
        sleep: Функция ожидания для лимита и повторов (для тестов).
    """

    name = "dmarket"

    def __init__(
        self,
        signer: DMarketSigner,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.signer = signer
        self.client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._limiter = RateLimiter(1, 1.0, clock=clock, sleep=sleep)

    def _request(
        self, method: str, path: str, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        query = urlencode(params or {}, quote_via=quote)
        path_with_query = f"{path}?{query}" if query else path
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        response = request_with_retries(
            self.client,
            method,
            f"{BASE_URL}{path_with_query}",
            limiter=self._limiter,
            sleep=self._sleep,
            content=body_text.encode() if body is not None else None,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SkinEdge/0.1 (+price monitor)",
                **self.signer.headers(method, path_with_query, body_text),
            },
        )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"DMarket {path}: ответ не JSON") from exc

    @staticmethod
    def _game(app_id: int) -> str:
        try:
            return GAME_IDS[app_id]
        except KeyError as exc:
            raise SourceError(f"DMarket: игра app_id={app_id} не поддерживается") from exc

    def fetch_best_prices(self, titles: list[str], app_id: int = 730) -> list[Quote]:
        """Лучшая цена предложения (ask) и лучшая заявка (bid) по названиям.

        Args:
            titles: Точные названия предметов (market_hash_name).
            app_id: Steam app_id игры.

        Returns:
            Сводные котировки; названия без предложений и заявок приходят с ``None``.
        """
        game = self._game(app_id)
        quotes: list[Quote] = []
        for start in range(0, len(titles), TITLES_PER_REQUEST):
            batch = titles[start : start + TITLES_PER_REQUEST]
            cursor = ""
            while True:
                body: dict[str, Any] = {
                    "filter": {"game": game, "titles": batch},
                    "limit": str(len(batch)),
                }
                if cursor:
                    body["cursor"] = cursor
                data = self._request("POST", "/marketplace-api/v1/aggregated-prices", body=body)
                if not isinstance(data, dict):
                    raise SourceError("DMarket aggregated-prices: ожидался объект")
                rows = data.get("aggregatedPrices") or []
                if rows and not quotes:
                    log.debug("DMarket aggregated-prices, первая строка: %s", rows[0])
                quotes.extend(self._parse_prices(rows, app_id))
                cursor = data.get("nextCursor") or ""
                if not cursor:
                    break
        return quotes

    def _parse_prices(self, rows: list[Any], app_id: int) -> list[Quote]:
        fetched_at = datetime.now(UTC)
        out: list[Quote] = []
        for row in rows:
            try:
                title = row["title"]
                out.append(
                    Quote(
                        source=self.name,
                        app_id=app_id,
                        market_hash_name=title,
                        currency=Currency.USD,
                        kind=QuoteKind.AGGREGATE,
                        min_price=_price_obj(row.get("offerBestPrice")),
                        quantity=int(row.get("offerCount") or 0),
                        best_bid=_price_obj(row.get("orderBestPrice")),
                        bid_quantity=int(row.get("orderCount") or 0),
                        url=f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(title)}",
                        fetched_at=fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("DMarket: пропущена строка %r: %s", row, exc)
        return out

    def fetch_last_sales(self, title: str, app_id: int = 730, limit: int = 20) -> list[Sale]:
        """Последние продажи предмета (до 20 за запрос, за 12 месяцев).

        Args:
            title: Точное название предмета.
            app_id: Steam app_id игры.
            limit: Сколько продаж запросить (1–20).

        Returns:
            Продажи, новые первыми.
        """
        params = {
            "gameId": self._game(app_id),
            "title": title,
            "limit": str(max(1, min(limit, 20))),
        }
        data = self._request("GET", "/trade-aggregator/v1/last-sales", params=params)
        fetched_at = datetime.now(UTC)
        sales: list[Sale] = []
        for row in (data or {}).get("sales") or []:
            try:
                attrs = row.get("offerAttributes") or {}
                price = _amount(row["price"])
                if price is None:
                    raise ValueError("нет цены")
                float_value = attrs.get("floatValue")
                sales.append(
                    Sale(
                        source=self.name,
                        app_id=app_id,
                        market_hash_name=title,
                        currency=Currency.USD,
                        price=price,
                        sold_at=datetime.fromtimestamp(int(row["date"]), tz=UTC),
                        tx_type=row.get("txOperationType"),
                        float_value=Decimal(str(float_value)) if float_value else None,
                        phase=attrs.get("phaseTitle") or None,
                        fetched_at=fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                log.warning("DMarket last-sales: пропущена строка %r: %s", row, exc)
        return sorted(sales, key=lambda s: s.sold_at, reverse=True)

    def fetch_sell_fees(self, app_id: int = 730, offer_type: str = "dmarket") -> list[FeeRule]:
        """Общая комиссия продавца из API.

        Сниженные ставки по отдельным предметам пока не применяются (см. комментарий ниже).

        Args:
            app_id: Steam app_id игры.
            offer_type: ``dmarket`` или ``p2p``.

        Returns:
            Правила комиссий со статусом ``official``.
        """
        game = self._game(app_id)
        rules: list[FeeRule] = []
        reduced: list[Any] = []
        offset = 0
        default: Any = None
        while True:
            data = self._request(
                "GET",
                "/exchange/v1/customized-fees",
                params={
                    "gameId": game,
                    "offerType": offer_type,
                    "limit": "100",
                    "offset": str(offset),
                },
            )
            default = default or data.get("defaultFee")
            page = data.get("reducedFees") or []
            reduced.extend(page)
            if len(page) < 100:
                break
            offset += 100
        if not default or default.get("fraction") is None:
            raise SourceError("DMarket customized-fees: нет defaultFee")
        log.info("DMarket defaultFee (%s): %s", offer_type, default)
        today = datetime.now(UTC).date()
        rules.append(
            FeeRule(
                id=f"dmarket.sell.{offer_type}.default",
                marketplace=self.name,
                operation=Operation.SELL,
                kind=FeeKind.PERCENT,
                rate=Decimal(str(default["fraction"])),
                currency=Currency.USD,
                verification=Verification.OFFICIAL,
                source=DOCS_URL,
                verified_at=today,
                note=f"Из API customized-fees ({offer_type}); minAmount={default.get('minAmount')}"
                " не учтён: единицы не подтверждены",
            )
        )
        if reduced:
            # Сниженные ставки действуют в диапазоне цен minPrice–maxPrice, единицы которого
            # не подтверждены. Пока их не применяем: общая ставка выше, расчёт консервативнее.
            log.info("DMarket: %d предметов со сниженной комиссией не учтены", len(reduced))
        return rules
