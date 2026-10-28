import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from test_runner import FakeDMarket, FakeSkinport
from test_scanner import FX, NAME

from skinedge.fees.models import Operation
from skinedge.fees.schedule import FeeSchedule
from skinedge.games import CS2, DOTA2, RUST
from skinedge.money import Currency, Money
from skinedge.paper import PaperTrade
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote, QuoteKind, Sale, SalesWindow
from skinedge.sources.base import SourceError
from skinedge.sources.marketcsgo import INDEX_SECONDS, MarketCSGOSource
from skinedge.storage import Storage

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
TS = int(NOW.timestamp())

# Формат ответов — как в живых запросах 27–28.10.2026.
PRICES = {
    "success": True,
    "time": 1793088722,
    "currency": "RUB",
    "items": [
        {"market_hash_name": NAME, "volume": "372", "price": "1961.82"},
        {"market_hash_name": "Prisma Case", "volume": "1179", "price": "83.25"},
        {"market_hash_name": "Zero", "volume": "3", "price": "0.00"},
        {"market_hash_name": "No volume", "price": "90.00"},
    ],
}
INDEX = {"history": {NAME: 7929, "Prisma Case": 39}}
HISTORY = {
    "time": 1793087749,
    "data": {
        "id": 7929,
        "sales7d": {"RUB": 3, "USD": 3, "EUR": 3},
        "history": [
            [TS - 3600, 2144.99, 25.895, 23.62],
            [TS - 7200, 2296.43, 27.723, 25.288],
            [TS - 7200, "bad", 27.723, 25.288],
        ],
    },
}


def source(
    routes: dict[str, Any], seen: list[str] | None = None, now: list[float] | None = None
) -> MarketCSGOSource:
    clock = now if now is not None else [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        body = routes.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=json.dumps(body))

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MarketCSGOSource(client, clock=lambda: clock[0], sleep=sleep)


CS = "https://market.csgo.com/api/v2"


def test_prices_parsed_in_rubles() -> None:
    quotes = source({f"{CS}/prices/RUB.json": PRICES}).fetch_quotes(CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("1961.82"), 372),
        ("Prisma Case", Decimal("83.25"), 1179),
    ]
    first = quotes[0]
    assert first.source == "marketcsgo" and first.currency is Currency.RUB
    assert first.kind is QuoteKind.AGGREGATE and first.app_id == CS2


def test_each_game_has_its_own_site() -> None:
    seen: list[str] = []
    routes = {
        "https://market.dota2.net/api/v2/prices/RUB.json": PRICES,
        "https://rust.tm/api/v2/prices/RUB.json": PRICES,
    }
    src = source(routes, seen)
    assert src.fetch_quotes(DOTA2)[0].app_id == DOTA2
    assert src.fetch_quotes(RUST)[0].app_id == RUST
    assert seen == list(routes)
    with pytest.raises(SourceError, match="440"):
        src.fetch_quotes(440)


@pytest.mark.parametrize(
    "body",
    [
        {**PRICES, "currency": "EUR"},
        {**PRICES, "success": False},
        {"success": True, "currency": "RUB"},
        {**PRICES, "items": [{"market_hash_name": "Zero", "volume": "1", "price": "0"}]},
    ],
)
def test_bad_price_list_is_an_error(body: dict[str, Any]) -> None:
    with pytest.raises(SourceError):
        source({f"{CS}/prices/RUB.json": body}).fetch_quotes(CS2)


def test_best_prices_mark_items_without_offers() -> None:
    src = source({f"{CS}/prices/RUB.json": PRICES})
    quotes = src.fetch_best_prices([NAME, "Gone", NAME], CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("1961.82"), 372),
        ("Gone", None, 0),
    ]


def test_sales_history_in_rubles_and_index_cached() -> None:
    seen: list[str] = []
    now = [0.0]
    routes = {f"{CS}/full-history/all.json": INDEX, f"{CS}/full-history/7929.json": HISTORY}
    src = source(routes, seen, now)

    sales = src.fetch_last_sales(NAME, CS2)
    assert [(s.price, s.sold_at) for s in sales] == [
        (Decimal("2144.99"), NOW - timedelta(hours=1)),
        (Decimal("2296.43"), NOW - timedelta(hours=2)),
    ]
    assert all(s.currency is Currency.RUB and s.source == "marketcsgo" for s in sales)
    # Предмета нет в истории: продаж нет, отдельный файл не запрашивается.
    assert src.fetch_last_sales("Unknown", CS2) == []
    assert seen == [f"{CS}/full-history/all.json", f"{CS}/full-history/7929.json"]

    now[0] += INDEX_SECONDS
    src.fetch_last_sales("Unknown", CS2)
    assert seen.count(f"{CS}/full-history/all.json") == 2


def test_history_without_data_is_an_error() -> None:
    routes = {f"{CS}/full-history/all.json": INDEX, f"{CS}/full-history/7929.json": {"time": 1}}
    with pytest.raises(SourceError, match=r"data\.history"):
        source(routes).fetch_last_sales(NAME, CS2)


def test_requests_are_paced() -> None:
    now = [0.0]
    routes = {f"{CS}/full-history/all.json": INDEX, f"{CS}/full-history/7929.json": HISTORY}
    src = source({**routes, f"{CS}/prices/RUB.json": PRICES}, now=now)
    src.fetch_quotes(CS2)
    src.fetch_last_sales(NAME, CS2)  # ещё два запроса: третий ждёт окна
    assert now[0] >= 1.0


# ---------- комиссии ----------


def test_fee_rules() -> None:
    schedule = FeeSchedule.load()
    market = schedule.market("marketcsgo")
    assert market.currency is Currency.RUB and market.settlement_days == 8
    base = Money.of(Decimal("9000"), Currency.RUB)
    sell = schedule.select("marketcsgo", Operation.SELL, base)
    assert [r.rate for r in sell] == [Decimal("0.05")]
    assert schedule.methods("marketcsgo", Operation.WITHDRAW) == ["card"]


# ---------- проход ----------


def tm_quote(name: str, price: str | None, qty: int = 10) -> Quote:
    return Quote(
        source="marketcsgo",
        app_id=CS2,
        market_hash_name=name,
        currency=Currency.RUB,
        kind=QuoteKind.AGGREGATE,
        min_price=None if price is None else Decimal(price),
        quantity=qty,
        fetched_at=NOW,
    )


def tm_sale(name: str, price: str, days: int) -> Sale:
    return Sale(
        source="marketcsgo",
        app_id=CS2,
        market_hash_name=name,
        currency=Currency.RUB,
        price=Decimal(price),
        sold_at=NOW - timedelta(days=days, hours=1),
        fetched_at=NOW,
    )


class FakeMarketCSGO:
    """NAME дешевле, чем на Skinport; X дороже и продаётся каждый день."""

    def __init__(self, fail_sales: bool = False, fail_prices: bool = False) -> None:
        self.fail_sales = fail_sales
        self.fail_prices = fail_prices
        self.requested: list[str] = []
        self.sales_for: list[str] = []

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        if self.fail_prices:
            raise SourceError("prices/RUB.json: HTTP 503")
        self.requested = list(titles)
        prices = {NAME: "810.00", "X": "2700.00"}
        return [tm_quote(t, prices.get(t), 10 if t in prices else 0) for t in titles]

    def fetch_last_sales(self, title: str, app_id: int = CS2) -> list[Sale]:
        self.sales_for.append(title)
        if self.fail_sales:
            raise SourceError("full-history: HTTP 503")
        return [tm_sale(title, "2430.00", d) for d in range(6)] if title == "X" else []


def _pipeline(storage: Storage, tm: FakeMarketCSGO, out: list[str]) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(NOW),  # type: ignore[arg-type]
        dmarket=FakeDMarket(NOW),  # type: ignore[arg-type]
        marketcsgo=tm,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: NOW,
    )


def _routes(found: list[Any]) -> set[tuple[str, str, str]]:
    return {(o.name, o.result.buy_market, o.result.sell_market) for o in found}


def test_market_csgo_routes() -> None:
    s = Storage(":memory:")
    tm = FakeMarketCSGO()
    out: list[str] = []
    result = _pipeline(s, tm, out).scan(ScanOptions(limit=2))

    assert tm.requested == [NAME, "X"]
    assert "Market.CSGO: запрошено 2, с лотами 2" in out
    # Продажи проверены только у кандидата на продажу в Market.CSGO.
    assert tm.sales_for == ["X"]
    assert "Проверяю реальные продажи Market.CSGO: 1 предметов" in out
    routes = _routes(result.found)
    assert (NAME, "marketcsgo", "skinport") in routes
    assert ("X", "skinport", "marketcsgo") in routes
    x = next(o for o in result.found if o.name == "X")
    # Цена продажи — медиана реальных сделок, ниже минимального лота.
    assert x.result.sell_price == Money.of(Decimal("2430.00"), Currency.RUB)
    assert x.sell.basis == "медиана продаж 7д, 6 эпизодов"
    assert x.result.lock_days >= 8

    # Через 10 минут история не перечитывается: файлы обновляются раз в час.
    _pipeline(s, tm, out).scan(ScanOptions(limit=2))
    assert tm.sales_for == ["X"]
    assert s.recent_sales("marketcsgo", SalesWindow.D7, NOW)["X"].volume == 6


def test_market_csgo_without_sales_is_not_a_sell() -> None:
    tm = FakeMarketCSGO(fail_sales=True)
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), tm, out).scan(ScanOptions(limit=2))
    assert tm.sales_for == ["X"]
    assert any("проверка прервана" in line for line in out)
    assert not any(o.result.sell_market == "marketcsgo" for o in result.found)
    assert (NAME, "marketcsgo", "skinport") in _routes(result.found)


def test_market_csgo_failure_skips_only_it() -> None:
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), FakeMarketCSGO(fail_prices=True), out).scan(
        ScanOptions(limit=2)
    )
    assert "Market.CSGO пропущен: prices/RUB.json: HTTP 503" in out
    assert result.found and all(
        "marketcsgo" not in (o.result.buy_market, o.result.sell_market) for o in result.found
    )


def test_open_trade_on_market_csgo_is_checked() -> None:
    s = Storage(":memory:")
    s.save_paper(
        PaperTrade(
            created_at=NOW - timedelta(days=1),
            app_id=CS2,
            market_hash_name="Old",
            buy_market="skinport",
            sell_market="marketcsgo",
            buy_price=Decimal("3.00"),
            buy_currency=Currency.EUR,
            sell_price=Decimal("4.00"),
            sell_currency=Currency.EUR,
            cost_eur=Decimal("3.00"),
            proceeds_eur=Decimal("3.40"),
            profit_eur=Decimal("0.40"),
            roi=Decimal("0.13"),
            usd_per_eur=Decimal("1.10"),
            complete=True,
            lock_days=15,
        )
    )
    tm = FakeMarketCSGO()
    _pipeline(s, tm, []).scan(ScanOptions(limit=2))
    assert "Old" in tm.requested
    assert tm.sales_for == ["X", "Old"]
