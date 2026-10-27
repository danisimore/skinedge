from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_runner import FakeDMarket, FakeSkinport
from test_scanner import FX, NAME, q

from skinedge.fees.schedule import FeeSchedule
from skinedge.money import Currency
from skinedge.pipeline import CSFLOAT_OFFSET, Pipeline, ScanOptions
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import SourceError
from skinedge.sources.csfloat import CSFloatSource
from skinedge.storage import Storage

KEY = "csfloat-secret-key"
NOW = datetime(2026, 10, 27, 0, 15, tzinfo=UTC)


def listing(name: str = NAME, price: int = 2380, kind: str = "buy_now") -> dict[str, Any]:
    # Сокращённый лот из живого ответа 27.10.2026.
    return {
        "id": "1032338883863513572",
        "created_at": "2026-10-19T17:06:09.842771Z",
        "type": kind,
        "price": price,
        "state": "listed",
        "item": {"market_hash_name": name, "float_value": 0.3498, "paint_seed": 540},
    }


def source(
    handler: Any, wall: float = 1000.0, reserve: int = 20
) -> tuple[CSFloatSource, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    now = [0.0]  # монотонные часы идут вперёд только на время паузы

    def sleep(seconds: float) -> None:
        now[0] += seconds

    src = CSFloatSource(
        KEY, client, sleep=sleep, clock=lambda: now[0], wall=lambda: wall, reserve=reserve
    )
    return src, seen


def ok(rows: list[dict[str, Any]], remaining: int = 150) -> httpx.Response:
    headers = {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": "2000"}
    return httpx.Response(200, json={"cursor": "c", "data": rows}, headers=headers)


def test_lowest_listing() -> None:
    src, seen = source(lambda r: ok([listing()]))
    (quote,) = src.fetch_lowest([NAME])
    assert quote.min_price == Decimal("23.80") and quote.currency is Currency.USD
    assert quote.kind is QuoteKind.LISTING and quote.listing_id == "1032338883863513572"
    assert quote.quantity == 1 and src.remaining == 150 and src.reset_at == 2000.0
    params = seen[0].url.params
    assert params["market_hash_name"] == NAME and params["sort_by"] == "lowest_price"
    assert params["type"] == "buy_now" and params["limit"] == "1"
    assert seen[0].headers["Authorization"] == KEY


def test_no_listings_and_foreign_rows() -> None:
    src, _ = source(lambda r: ok([listing(name="Other"), listing(kind="auction")]))
    (quote,) = src.fetch_lowest([NAME])
    assert quote.min_price is None and quote.quantity == 0


def test_bad_key_is_an_error_without_the_key() -> None:
    src, _ = source(lambda r: httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(SourceError) as err:
        src.fetch_lowest([NAME])
    assert "403" in str(err.value) and KEY not in str(err.value)


def test_rate_limit_stops_the_batch() -> None:
    responses = iter([ok([listing()]), httpx.Response(429)])
    src, seen = source(lambda r: next(responses))
    assert len(src.fetch_lowest([NAME, "B", "C"])) == 1
    assert len(seen) == 2


def test_reserve_is_kept_until_reset() -> None:
    src, seen = source(lambda r: ok([listing()], remaining=20), wall=1000.0)
    assert len(src.fetch_lowest([NAME, "B"])) == 1  # после первого ответа остался запас
    assert len(seen) == 1
    later, _ = source(lambda r: ok([listing()], remaining=20), wall=2500.0)
    assert len(later.fetch_lowest([NAME, NAME])) == 2  # окно лимита сброшено


def test_network_error_stops_quietly() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    src, _ = source(fail)
    assert src.fetch_lowest([NAME]) == []


def test_only_cs2() -> None:
    src, _ = source(lambda r: ok([]))
    with pytest.raises(SourceError):
        src.fetch_lowest([NAME], app_id=570)


# ---------- проход ----------


class FakeCSFloat:
    def __init__(self, error: Exception | None = None) -> None:
        self.asked: list[list[str]] = []
        self.remaining: int | None = 120
        self.error = error

    def fetch_lowest(self, names: list[str], app_id: int = 730) -> list[Quote]:
        if self.error:
            raise self.error
        self.asked.append(list(names))
        quote = q("dmarket", "9.00", at=NOW).model_copy(
            update={"source": "csfloat", "kind": QuoteKind.LISTING, "quantity": 1}
        )
        return [quote.model_copy(update={"market_hash_name": n}) for n in names]


def _pipeline(cf: FakeCSFloat, storage: Storage, out: list[str]) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(NOW),  # type: ignore[arg-type]
        dmarket=FakeDMarket(NOW),  # type: ignore[arg-type]
        csfloat=cf,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: NOW,
    )


def test_pipeline_buys_on_csfloat() -> None:
    s = Storage(":memory:")
    cf = FakeCSFloat()
    out: list[str] = []
    result = _pipeline(cf, s, out).run(ScanOptions(limit=3, csfloat=5, paper=False))

    # Только предметы со спросом на Skinport: у X нет продаж.
    assert cf.asked == [[NAME, "Old"]]
    routes = {(o.result.buy_market, o.result.sell_market) for o in result.found}
    assert ("csfloat", "skinport") in routes and ("dmarket", "skinport") in routes
    opp = next(o for o in result.found if o.result.buy_market == "csfloat")
    assert opp.result.complete and opp.buy_url is not None and "csfloat.com" in opp.buy_url
    assert s.get_meta(CSFLOAT_OFFSET) == "2"
    assert any("CSFloat: проверено 2 из 2" in line and "осталось 120" in line for line in out)


def test_pipeline_survives_csfloat_error() -> None:
    out: list[str] = []
    cf = FakeCSFloat(error=SourceError("CSFloat: ключ API не принят (HTTP 401)"))
    result = _pipeline(cf, Storage(":memory:"), out).run(ScanOptions(limit=2, paper=False))
    assert [o.result.buy_market for o in result.found] == ["dmarket"]
    assert any("CSFloat пропущен" in line for line in out)
