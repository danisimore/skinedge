import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_marketcsgo import NOW as SCAN_NOW
from test_marketcsgo import FakeMarketCSGO
from test_runner import FakeDMarket, FakeSkinport
from test_scanner import FX, NAME

from skinedge import cli
from skinedge.config import Settings
from skinedge.fees.models import Operation
from skinedge.fees.schedule import FeeSchedule
from skinedge.games import CS2, DOTA2, RUST
from skinedge.money import Currency, Money
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import SourceError
from skinedge.sources.csdeals import MAX_PAGES, CSDealsSource
from skinedge.storage import Storage

NOW = datetime(2026, 10, 28, 21, 33, tzinfo=UTC)
GENERATED = "2026-10-28T21:32:59.319Z"
KEY = "csd_test"
CASE = "Prisma Case"


def price_row(name: str, cents: Any, stock: Any = 1, **extra: Any) -> dict[str, Any]:
    # Формат — как в живом ответе 28.10.2026.
    return {
        "app_id": 730,
        "market_hash_name": name,
        "market_price": 4000,
        "recommended_price": 3900,
        "lowest_listing_price": cents,
        "updated_at": "2026-10-28T21:20:00.000Z",
        "stock": stock,
        "listing_count": stock,
        **extra,
    }


def lot(
    lot_id: int, name: str, cents: int, age: timedelta, lock: timedelta | None = None
) -> dict[str, Any]:
    created = NOW - age
    return {
        "id": lot_id,
        "app_id": 730,
        "market_hash_name": name,
        "price": cents,
        "amount": 1,
        "commodity": False,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "steam_asset_id": "54347504109",
        "trade_locked_until": None if lock is None else (NOW + lock).isoformat(),
    }


PRICES = {
    "prices": [
        price_row(NAME, 12000, stock=1),
        price_row(CASE, 61, stock=13),
        price_row("Nothing listed", None, stock=0),
    ],
    "generated_at": GENERATED,
}
# Самый дешёвый кейс заблокирован ещё на 3 дня, у AK-47 заблокирован лот дороже.
LOTS = [
    lot(9, CASE, 61, timedelta(days=4), lock=timedelta(days=3)),
    lot(8, NAME, 13000, timedelta(days=5), lock=timedelta(days=2)),
    lot(7, CASE, 70, timedelta(days=12)),
]


class Api:
    """CS.Deals с заданной сводкой и страницами лотов (по ``cursor``)."""

    def __init__(self, prices: Any = PRICES, pages: list[list[dict[str, Any]]] | None = None):
        self.prices = prices
        self.pages = [LOTS] if pages is None else pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/public/v1")
        if path == "/prices/all":
            return httpx.Response(200, text=json.dumps(self.prices))
        if path == "/listings":
            index = int(request.url.params.get("cursor", "0"))
            nxt = index + 1 if index + 1 < len(self.pages) else None
            return httpx.Response(200, json={"listings": self.pages[index], "next_cursor": nxt})
        return httpx.Response(404, json={"error": "NOT_FOUND"})


def source(api: Api) -> CSDealsSource:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(api))
    return CSDealsSource(KEY, client, clock=lambda: clock[0], sleep=sleep, now=lambda: NOW)


def test_prices_in_dollars_at_summary_time() -> None:
    api = Api()
    quotes = source(api).fetch_best_prices([NAME], CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("120.00"), 1)
    ]
    quote = quotes[0]
    assert quote.source == "csdeals" and quote.currency is Currency.USD
    assert quote.kind is QuoteKind.AGGREGATE and quote.app_id == CS2 and quote.url is None
    assert quote.fetched_at == datetime(2026, 10, 28, 21, 32, 59, 319000, tzinfo=UTC)


def test_only_reads_market_data_with_the_key_in_a_header() -> None:
    api = Api()
    source(api).fetch_best_prices([NAME], CS2)
    assert [(r.method, r.url.path, dict(r.url.params)) for r in api.requests] == [
        ("GET", "/public/v1/prices/all", {"app_id": "730"}),
        ("GET", "/public/v1/listings", {"limit": "1000", "app_id": "730"}),
    ]
    for request in api.requests:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in str(request.url)


def test_other_methods_are_refused() -> None:
    api = Api()
    with pytest.raises(SourceError, match="не разрешён"):
        source(api)._get("/purchase", {})
    assert api.requests == []


def test_locked_cheapest_lot_hides_the_price() -> None:
    quotes = {q.market_hash_name: q for q in source(Api()).fetch_best_prices([CASE, NAME], CS2)}
    # У кейса самый дешёвый лот заблокирован: цену доступного лота сводка не даёт.
    assert (quotes[CASE].min_price, quotes[CASE].quantity) == (None, 0)
    # У AK-47 заблокирован только лот дороже: цена сводки — лот, который можно вывести.
    assert quotes[NAME].min_price == Decimal("120.00")


def test_locked_lots_are_not_counted() -> None:
    prices = PRICES | {"prices": [price_row(CASE, 61, stock=13)]}
    pages = [[lot(9, CASE, 75, timedelta(days=1), lock=timedelta(days=6))]]
    quote = source(Api(prices, pages)).fetch_best_prices([CASE], CS2)[0]
    assert (quote.min_price, quote.quantity) == (Decimal("0.61"), 12)


def test_expired_lock_is_not_a_lock() -> None:
    pages = [[lot(9, CASE, 61, timedelta(days=8), lock=-timedelta(hours=1))]]
    quote = source(Api(pages=pages)).fetch_best_prices([CASE], CS2)[0]
    assert quote.min_price == Decimal("0.61")


def test_pages_are_read_until_lots_older_than_the_window() -> None:
    pages = [
        [lot(30, NAME, 15000, timedelta(days=1))],
        [lot(20, CASE, 61, timedelta(days=8, hours=23), lock=timedelta(hours=1))],
        [lot(10, NAME, 9000, timedelta(days=9, hours=1))],
        [lot(5, NAME, 8000, timedelta(days=30), lock=timedelta(days=1))],
    ]
    api = Api(pages=pages)
    quotes = {q.market_hash_name: q for q in source(api).fetch_best_prices([CASE, NAME], CS2)}
    cursors = [r.url.params.get("cursor") for r in api.requests if r.url.path.endswith("listings")]
    # Третья страница уже старше 9 дней: четвёртую не читаем.
    assert cursors == [None, "1", "2"]
    assert quotes[CASE].min_price is None and quotes[NAME].min_price == Decimal("120.00")


def test_window_not_reached_is_an_error() -> None:
    pages = [[lot(100 - i, NAME, 15000, timedelta(hours=i))] for i in range(MAX_PAGES + 1)]
    with pytest.raises(SourceError, match="не дошёл"):
        source(Api(pages=pages)).fetch_best_prices([NAME], CS2)


@pytest.mark.parametrize(
    "bad",
    [
        {"price": "61"},
        {"price": 0},
        {"market_hash_name": ""},
        {"created_at": None},
        {"trade_locked_until": "soon"},
        {"trade_locked_until": "2026-11-04T20:00:00"},
    ],
)
def test_bad_lot_is_an_error(bad: dict[str, Any]) -> None:
    # Неверный лот нельзя пропустить: заблокированный сошёл бы за доступный.
    pages = [[lot(9, CASE, 61, timedelta(days=4), lock=timedelta(days=3)) | bad]]
    with pytest.raises(SourceError):
        source(Api(pages=pages)).fetch_best_prices([CASE], CS2)


def test_bad_summary_rows_are_skipped() -> None:
    prices = PRICES | {
        "prices": [
            price_row("Float", 1.5),
            price_row("String", "150"),
            price_row("Bool", True),
            price_row("No stock", 150, stock=0),
            price_row(CASE, 61, stock=13),
        ]
    }
    quotes = source(Api(prices, [[]])).fetch_best_prices(
        ["Float", "String", "Bool", "No stock", CASE], CS2
    )
    assert [q.market_hash_name for q in quotes if q.min_price is not None] == [CASE]


@pytest.mark.parametrize(
    "prices",
    [
        {"prices": [price_row("Float", 1.5)], "generated_at": GENERATED},
        {"prices": [price_row(CASE, 61)], "generated_at": "2026-10-28T21:32:59"},
        {"prices": [price_row(CASE, 61)]},
        {"items": [price_row(CASE, 61)], "generated_at": GENERATED},
        [price_row(CASE, 61)],
    ],
)
def test_bad_summary_is_an_error(prices: Any) -> None:
    with pytest.raises(SourceError):
        source(Api(prices)).fetch_best_prices([CASE], CS2)


def test_best_prices_mark_items_without_offers() -> None:
    quotes = source(Api()).fetch_best_prices([NAME, "Nothing listed", "Gone", NAME], CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("120.00"), 1),
        ("Nothing listed", None, 0),
        ("Gone", None, 0),
    ]


def test_three_games_only() -> None:
    api = Api()
    for app_id in (DOTA2, RUST):
        source(api).fetch_best_prices([NAME], app_id)
    assert {r.url.params["app_id"] for r in api.requests} == {"570", "252490"}
    with pytest.raises(SourceError, match="440"):
        source(api).fetch_best_prices([NAME], 440)


def test_key_is_required() -> None:
    with pytest.raises(ValueError):
        CSDealsSource("")


def test_key_from_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)
    said: list[str] = []
    assert cli._csdeals_source(Settings(), said.append) is None
    assert said == ["Нет ключа CS.Deals (SKINEDGE_CSDEALS_KEY в .env): CS.Deals не проверяется"]
    monkeypatch.setenv("SKINEDGE_CSDEALS_KEY", KEY)
    settings = Settings()
    assert settings.csdeals_api_key is not None and KEY not in repr(settings)
    assert isinstance(cli._csdeals_source(settings, said.append), CSDealsSource)


def test_fee_rules() -> None:
    schedule = FeeSchedule.load()
    market = schedule.market("csdeals")
    assert market.currency is Currency.USD and market.transfer_lock_days == 7
    base = Money.of(Decimal("100"), Currency.USD)
    for operation in (Operation.BUY, Operation.DEPOSIT):
        assert [r.rate for r in schedule.select("csdeals", operation, base)] == [Decimal("0")]


# ---------- проход ----------


class FakeCSDeals:
    """X дешевле, чем продаётся на Market.CSGO."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requested: list[tuple[str, int]] = []

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        if self.fail:
            raise SourceError("/prices/all: HTTP 503")
        self.requested += [(t, app_id) for t in titles]
        return [
            Quote(
                source="csdeals",
                app_id=app_id,
                market_hash_name=t,
                currency=Currency.USD,
                kind=QuoteKind.AGGREGATE,
                min_price=Decimal("15.00") if t == "X" else None,
                quantity=2 if t == "X" else 0,
                fetched_at=SCAN_NOW,
            )
            for t in titles
        ]


def _pipeline(storage: Storage, cs: FakeCSDeals, out: list[str]) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(SCAN_NOW),  # type: ignore[arg-type]
        dmarket=FakeDMarket(SCAN_NOW),  # type: ignore[arg-type]
        marketcsgo=FakeMarketCSGO(),  # type: ignore[arg-type]
        csdeals=cs,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: SCAN_NOW,
    )


def test_cs_deals_is_a_buy_market() -> None:
    cs = FakeCSDeals()
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), cs, out).scan(ScanOptions(limit=2))

    assert "CS.Deals: запрошено 2, с лотами 1" in out
    found = [o for o in result.found if o.result.buy_market == "csdeals"]
    assert [(o.name, o.result.sell_market) for o in found] == [("X", "marketcsgo")]
    opp = found[0]
    assert opp.result.buy_price == Money.of(Decimal("15.00"), Currency.USD)
    # Страниц предметов у CS.Deals нет: ссылка на рынок из правил.
    assert opp.buy_url == "https://cs.deals/market"
    assert not any(o.result.sell_market == "csdeals" for o in result.found)


def test_cs_deals_in_every_game() -> None:
    cs = FakeCSDeals()
    _pipeline(Storage(":memory:"), cs, []).scan(ScanOptions(games=(CS2, DOTA2), limit=2))
    assert {app_id for _, app_id in cs.requested} == {CS2, DOTA2}


def test_cs_deals_failure_skips_only_it() -> None:
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), FakeCSDeals(fail=True), out).scan(
        ScanOptions(limit=2, skinport=True)
    )
    assert "CS.Deals пропущен: /prices/all: HTTP 503" in out
    assert result.found and all(o.result.buy_market != "csdeals" for o in result.found)
