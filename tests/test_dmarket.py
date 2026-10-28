"""Тесты адаптера DMarket.

Ответы собраны по схемам из https://docs.dmarket.com/v1/swagger.html и сверены
с живым запуском 26.10.2026: Amount в aggregated-prices — центы строкой, счётчики —
строки, price в last-sales — доллары.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from nacl.signing import SigningKey, VerifyKey

from skinedge.fees.models import Operation, Verification
from skinedge.sources.base import SourceError
from skinedge.sources.dmarket import DMarketSigner, DMarketSource

SEED = bytes(range(32))
KEY = SigningKey(SEED)
PUBLIC = KEY.verify_key.encode().hex()
SECRET64 = (SEED + KEY.verify_key.encode()).hex()


def signer() -> DMarketSigner:
    return DMarketSigner(PUBLIC, SECRET64, clock=lambda: 1_793_000_000.7)


def make_source(handler: Any) -> tuple[DMarketSource, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return DMarketSource(signer(), client, sleep=sleep, clock=lambda: now[0]), seen


def verify(request: httpx.Request) -> None:
    """Проверяет подпись так, как это делает сервер DMarket."""
    ts = request.headers["X-Sign-Date"]
    sign = request.headers["X-Request-Sign"]
    assert sign.startswith("dmar ed25519 ")
    path = request.url.raw_path.decode()
    message = f"{request.method}{path}{request.content.decode()}{ts}".encode()
    VerifyKey(bytes.fromhex(request.headers["X-Api-Key"])).verify(
        message, bytes.fromhex(sign.removeprefix("dmar ed25519 "))
    )


def test_signer_headers_verify() -> None:
    h = signer().headers("GET", "/x?a=1", "")
    assert h["X-Api-Key"] == PUBLIC
    assert h["X-Sign-Date"] == "1793000000"
    sig = bytes.fromhex(h["X-Request-Sign"].removeprefix("dmar ed25519 "))
    VerifyKey(bytes.fromhex(PUBLIC)).verify(b"GET/x?a=11793000000", sig)


def test_signer_accepts_seed() -> None:
    assert DMarketSigner(PUBLIC, SEED.hex()).public_key == PUBLIC


def test_signer_rejects_mismatched_keys() -> None:
    other = SigningKey(bytes(32)).verify_key.encode().hex()
    with pytest.raises(ValueError, match="не соответствует"):
        DMarketSigner(other, SECRET64)


def test_signer_rejects_non_hex() -> None:
    with pytest.raises(ValueError, match="hex"):
        DMarketSigner("zz", "yy")


PRICES = {
    "aggregatedPrices": [
        {
            "title": "AK-47 | Redline (Field-Tested)",
            "orderBestPrice": {"Currency": "USD", "Amount": "2210"},
            "orderCount": "154",
            "offerBestPrice": {"Currency": "USD", "Amount": "2437"},
            "offerCount": "311",
        },
        {
            "title": "Sticker | Nobody",
            "orderBestPrice": None,
            "orderCount": 0,
            "offerBestPrice": None,
            "offerCount": 0,
        },
        {"title": "Bad", "offerBestPrice": {"Currency": "EUR", "Amount": "1"}},
    ],
    "nextCursor": "",
}


def test_best_prices_parsed_and_signed() -> None:
    src, seen = make_source(lambda r: httpx.Response(200, json=PRICES))
    quotes = {
        q.market_hash_name: q
        for q in src.fetch_best_prices(["AK-47 | Redline (Field-Tested)", "Sticker | Nobody"])
    }
    ak = quotes["AK-47 | Redline (Field-Tested)"]
    assert ak.min_price == Decimal("24.37") and ak.quantity == 311
    assert ak.best_bid == Decimal("22.10") and ak.bid_quantity == 154
    assert quotes["Sticker | Nobody"].min_price is None
    assert "Bad" not in quotes  # не та валюта

    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/marketplace-api/v1/aggregated-prices"
    body = json.loads(req.content)
    assert body["filter"] == {
        "game": "a8db",
        "titles": ["AK-47 | Redline (Field-Tested)", "Sticker | Nobody"],
    }
    verify(req)


def test_best_prices_batches_and_cursor() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["filter"]["titles"][0] == "t0" and "cursor" not in body:
            return httpx.Response(200, json={"aggregatedPrices": [], "nextCursor": "c1"})
        return httpx.Response(200, json={"aggregatedPrices": [], "nextCursor": ""})

    src, _ = make_source(handler)
    src.fetch_best_prices([f"t{i}" for i in range(150)])
    assert len(calls) == 3  # 100 названий + продолжение по курсору + ещё 50
    assert calls[1]["cursor"] == "c1"
    assert len(calls[2]["filter"]["titles"]) == 50


SALES = {
    "sales": [
        {
            "price": "23.50",
            "date": "1793000000",
            "txOperationType": "Offer",
            "offerAttributes": {"floatValue": 0.2512, "phaseTitle": ""},
        },
        {"price": "24.10", "date": "1793003600", "txOperationType": "Target"},
        {"price": None, "date": "1"},
    ]
}


def test_last_sales_parsed_and_query_signed() -> None:
    src, seen = make_source(lambda r: httpx.Response(200, json=SALES))
    sales = src.fetch_last_sales("AK-47 | Redline (Field-Tested)")
    assert [s.price for s in sales] == [Decimal("24.10"), Decimal("23.50")]
    assert sales[1].float_value == Decimal("0.2512")
    assert sales[0].sold_at == datetime.fromtimestamp(1793003600, tz=UTC)
    req = seen[0]
    assert req.url.params["title"] == "AK-47 | Redline (Field-Tested)"
    assert req.url.params["limit"] == "20"
    verify(req)


def test_sell_fees_default_official() -> None:
    data = {
        "defaultFee": {"fraction": "0.1", "minAmount": 1},
        "reducedFees": [{"title": "X", "fraction": "0.02", "minPrice": 0, "maxPrice": 1}],
    }
    src, seen = make_source(lambda r: httpx.Response(200, json=data))
    rules = src.fetch_sell_fees()
    assert len(seen) == 1  # только первая страница
    assert len(rules) == 1  # сниженные ставки пока не применяются
    assert rules[0].rate == Decimal("0.1")
    assert rules[0].min_fee == Decimal("0.01")
    assert rules[0].operation is Operation.SELL
    assert rules[0].verification is Verification.OFFICIAL


def test_sell_fees_missing_default() -> None:
    src, _ = make_source(lambda r: httpx.Response(200, json={"reducedFees": []}))
    with pytest.raises(SourceError):
        src.fetch_sell_fees()


def test_cents_format_is_enforced() -> None:
    bad = {
        "aggregatedPrices": [
            {"title": "X", "offerBestPrice": {"Currency": "USD", "Amount": "24.37"}}
        ]
    }
    src, _ = make_source(lambda r: httpx.Response(200, json=bad))
    assert src.fetch_best_prices(["X"]) == []  # формат изменился: строка пропущена, а не принята


def test_unauthorized_not_retried() -> None:
    src, seen = make_source(lambda r: httpx.Response(401, json={"error": "bad sign"}))
    with pytest.raises(SourceError, match="HTTP 401"):
        src.fetch_best_prices(["x"])
    assert len(seen) == 1


def test_unknown_game() -> None:
    src, _ = make_source(lambda r: httpx.Response(200, json={}))
    with pytest.raises(SourceError, match="не поддерживается"):
        src.fetch_best_prices(["x"], app_id=1)


# Форма ответа GET /marketplace-api/v2/offers сверена с живым запуском 28.10.2026:
# название и картинка лежат в attributes, верхний title пустой.
IMAGE = "https://steamcommunity-a.akamaihd.net/economy/image/abc"


def offer(title: str, image: str = IMAGE) -> dict[str, Any]:
    return {
        "offerId": "o1",
        "priceCents": "3600",
        "attributes": {"title": title, "imageUri": image, "gameId": "a8db"},
    }


def test_image_by_exact_title() -> None:
    name = "Fracture Case"
    data = {"items": [offer("Fracture Case Key", IMAGE + "key"), offer(name)], "cursor": ""}
    src, seen = make_source(lambda r: httpx.Response(200, json=data))
    assert src.fetch_image(name) == IMAGE
    req = seen[0]
    assert req.url.path == "/marketplace-api/v2/offers"
    assert req.url.params["title"] == name and req.url.params["withImages"] == "true"
    assert req.url.params["gameId"] == "a8db"
    verify(req)

    only_prefix = {"items": [offer("Fracture Case Key")]}
    src, _ = make_source(lambda r: httpx.Response(200, json=only_prefix))
    assert src.fetch_image(name) is None
    src, _ = make_source(lambda r: httpx.Response(200, json={"code": "x"}))
    with pytest.raises(SourceError):
        src.fetch_image(name)
