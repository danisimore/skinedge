import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_marketcsgo import NOW as SCAN_NOW
from test_marketcsgo import FakeMarketCSGO
from test_runner import FakeDMarket, FakeSkinport
from test_scanner import FX, NAME

from skinedge.fees.models import Operation
from skinedge.fees.schedule import FeeSchedule
from skinedge.games import CS2, DOTA2, RUST
from skinedge.money import Currency, Money
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import SourceError
from skinedge.sources.whitemarket import WhiteMarketSource
from skinedge.storage import Storage

WM = "https://export.white.market/v1/prices"
AS_OF = "2026-10-28T21:03:02+00:00"
META = {"file": "730.json", "data_as_of": AS_OF, "generated_at": "2026-10-28T21:04:40+00:00"}
KARAMBIT = "★ Karambit | Doppler (Factory New)"


def row(name: str, price: Any, count: Any = 5, **extra: Any) -> dict[str, Any]:
    link = f"https://white.market/item?appId=730&amp;nameHash={name.replace(' ', '%20')}"
    return {
        "market_hash_name": name,
        "price": price,
        "market_product_link": link,
        "market_product_count": count,
        "cheapest_float": None,
        "paint_index": "0",
        **extra,
    }


# Формат — как в живом ответе 28.10.2026: цена строкой с тремя знаками, «&amp;» в ссылке,
# фазы Doppler отдельными строками.
ROWS = [
    row(NAME, "26.160", count=3177, paint_index="282"),
    row(KARAMBIT, "7291.050", count=9, paint_index="415")
    | {"market_product_link": "https://white.market/item?appId=730&amp;csgoPhase=RUBY"},
    row(KARAMBIT, "5060.280", count=4, paint_index="418")
    | {"market_product_link": "https://white.market/item?appId=730&amp;csgoPhase=PHASE1"},
]


def source(routes: dict[str, Any], seen: list[str] | None = None) -> WhiteMarketSource:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        body = routes.get(url)
        if body is None:
            return httpx.Response(404, text="Not Found")
        text = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(200, text=text)

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WhiteMarketSource(client, clock=lambda: clock[0], sleep=sleep)


def export(rows: Any, meta: Any = META) -> dict[str, Any]:
    return {f"{WM}/730.json.meta.json": meta, f"{WM}/730.json": rows}


def test_prices_parsed_in_dollars_at_export_time() -> None:
    seen: list[str] = []
    quotes = source(export(ROWS), seen).fetch_quotes(CS2)
    # Сначала описание, потом сам файл.
    assert seen == [f"{WM}/730.json.meta.json", f"{WM}/730.json"]
    first = quotes[0]
    assert (first.market_hash_name, first.min_price, first.quantity) == (
        NAME,
        Decimal("26.160"),
        3177,
    )
    assert first.source == "whitemarket" and first.currency is Currency.USD
    assert first.kind is QuoteKind.AGGREGATE and first.app_id == CS2
    assert first.fetched_at == datetime(2026, 10, 28, 21, 3, 2, tzinfo=UTC)
    assert (
        first.url
        == "https://white.market/item?appId=730&nameHash=AK-47%20|%20Redline%20(Field-Tested)"
    )


def test_variants_of_one_name_give_the_cheapest() -> None:
    quotes = {q.market_hash_name: q for q in source(export(ROWS)).fetch_quotes(CS2)}
    knife = quotes[KARAMBIT]
    assert knife.min_price == Decimal("5060.280") and knife.quantity == 13
    assert knife.url == "https://white.market/item?appId=730&csgoPhase=PHASE1"


def test_bad_rows_are_skipped() -> None:
    rows = [
        row("Zero", "0"),
        row("No lots", "1.000", count=0),
        row("Bool count", "1.000", count=True),
        row("", "1.000"),
        row("Float price", 1.5),
        row("Foreign link", "2.000") | {"market_product_link": "https://example.com/x"},
        row("No link", "2.500") | {"market_product_link": None},
    ]
    quotes = {q.market_hash_name: q for q in source(export(rows)).fetch_quotes()}
    # Число JSON читается как Decimal, а не как float.
    assert set(quotes) == {"Float price", "Foreign link", "No link"}
    assert quotes["Float price"].min_price == Decimal("1.5")
    assert quotes["Foreign link"].url is None and quotes["No link"].url is None


@pytest.mark.parametrize(
    "routes",
    [
        export({"items": ROWS}),
        export([row("Zero", "0")]),
        export("<html>Just a moment...</html>"),
        export(ROWS, meta=META | {"file": "730.10min.json"}),
        export(ROWS, meta={"file": "730.json"}),
        export(ROWS, meta=META | {"data_as_of": "2026-10-28T21:03:02"}),
        {f"{WM}/730.json": ROWS},
    ],
)
def test_bad_export_is_an_error(routes: dict[str, Any]) -> None:
    with pytest.raises(SourceError):
        source(routes).fetch_quotes(CS2)


def test_only_cs2() -> None:
    seen: list[str] = []
    src = source(export(ROWS), seen)
    for app_id in (DOTA2, RUST):
        with pytest.raises(SourceError, match=str(app_id)):
            src.fetch_quotes(app_id)
    assert seen == []


def test_best_prices_mark_items_without_offers() -> None:
    quotes = source(export(ROWS)).fetch_best_prices([NAME, "Gone", NAME], CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("26.160"), 3177),
        ("Gone", None, 0),
    ]
    assert quotes[1].fetched_at == quotes[0].fetched_at


def test_fee_rules() -> None:
    schedule = FeeSchedule.load()
    market = schedule.market("whitemarket")
    assert market.currency is Currency.USD and market.transfer_lock_days == 7
    base = Money.of(Decimal("100"), Currency.USD)
    for operation in (Operation.BUY, Operation.DEPOSIT):
        assert [r.rate for r in schedule.select("whitemarket", operation, base)] == [Decimal("0")]


# ---------- проход ----------


class FakeWhiteMarket:
    """X дешевле, чем продаётся на Market.CSGO."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requested: list[tuple[str, int]] = []

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        if self.fail:
            raise SourceError("730.json: HTTP 503")
        self.requested += [(t, app_id) for t in titles]
        return [
            Quote(
                source="whitemarket",
                app_id=app_id,
                market_hash_name=t,
                currency=Currency.USD,
                kind=QuoteKind.AGGREGATE,
                min_price=Decimal("15.00") if t == "X" else None,
                quantity=7 if t == "X" else 0,
                url="https://white.market/item?appId=730&nameHash=X" if t == "X" else None,
                fetched_at=SCAN_NOW,
            )
            for t in titles
        ]


def _pipeline(storage: Storage, wm: FakeWhiteMarket, out: list[str]) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(SCAN_NOW),  # type: ignore[arg-type]
        dmarket=FakeDMarket(SCAN_NOW),  # type: ignore[arg-type]
        marketcsgo=FakeMarketCSGO(),  # type: ignore[arg-type]
        whitemarket=wm,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: SCAN_NOW,
    )


def test_white_market_is_a_buy_market() -> None:
    wm = FakeWhiteMarket()
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), wm, out).scan(ScanOptions(limit=2))

    assert "White.market: запрошено 2, с лотами 1" in out
    found = [o for o in result.found if o.result.buy_market == "whitemarket"]
    assert [(o.name, o.result.sell_market) for o in found] == [("X", "marketcsgo")]
    opp = found[0]
    assert opp.result.buy_price == Money.of(Decimal("15.00"), Currency.USD)
    assert opp.buy_url == "https://white.market/item?appId=730&nameHash=X"
    assert not any(o.result.sell_market == "whitemarket" for o in result.found)


def test_white_market_only_in_cs2() -> None:
    wm = FakeWhiteMarket()
    _pipeline(Storage(":memory:"), wm, []).scan(ScanOptions(games=(CS2, DOTA2), limit=2))
    assert {app_id for _, app_id in wm.requested} == {CS2}


def test_white_market_failure_skips_only_it() -> None:
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), FakeWhiteMarket(fail=True), out).scan(
        ScanOptions(limit=2, skinport=True)
    )
    assert "White.market пропущен: 730.json: HTTP 503" in out
    assert result.found and all(o.result.buy_market != "whitemarket" for o in result.found)
