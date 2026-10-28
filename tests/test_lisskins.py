import json
from datetime import UTC, datetime
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
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote, QuoteKind
from skinedge.sources.base import SourceError
from skinedge.sources.lisskins import LisSkinsSource
from skinedge.storage import Storage

NOW = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
LIS = "https://lis-skins.com/market_export_json"


def row(name: str, price: Any, unlocked: Any = None, count: Any = 5, **extra: Any) -> dict:
    slug = name.lower().replace(" ", "-")
    return {
        "name": name,
        "price": price,
        "unlocked_price": price if unlocked is None else unlocked,
        "url": f"https://lis-skins.com/market/csgo/{slug}/",
        "count": count,
        **extra,
    }


# Формат — как в живых ответах 27.10.2026 (числа JSON, полная ссылка).
ROWS = [
    {
        "name": "FAMAS | Doomkitty (Minimal Wear)",
        "price": 6.44,
        "unlocked_price": 6.44,
        "url": "https://lis-skins.com/market/csgo/famas-doomkitty-minimal-wear/",
        "count": 80,
    },
    row(NAME, 9, count=3),
]


def source(routes: dict[str, Any], seen: list[str] | None = None) -> LisSkinsSource:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        body = routes.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        text = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(200, text=text)

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LisSkinsSource(client, clock=lambda: clock[0], sleep=sleep)


def test_prices_parsed_in_dollars() -> None:
    quotes = source({f"{LIS}/csgo.json": ROWS}).fetch_quotes(CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        ("FAMAS | Doomkitty (Minimal Wear)", Decimal("6.44"), 80),
        (NAME, Decimal("9"), 3),
    ]
    first = quotes[0]
    assert first.source == "lisskins" and first.currency is Currency.USD
    assert first.kind is QuoteKind.AGGREGATE and first.app_id == CS2
    assert first.url == "https://lis-skins.com/market/csgo/famas-doomkitty-minimal-wear/"


def test_each_game_has_its_own_file() -> None:
    seen: list[str] = []
    routes = {f"{LIS}/dota2.json": ROWS, f"{LIS}/rust.json": ROWS}
    src = source(routes, seen)
    assert src.fetch_quotes(DOTA2)[0].app_id == DOTA2
    assert src.fetch_quotes(RUST)[0].app_id == RUST
    assert seen == list(routes)
    with pytest.raises(SourceError, match="440"):
        src.fetch_quotes(440)


def test_buy_price_is_the_cheapest_unlocked_lot() -> None:
    rows = [
        row("Locked cheaper", 4.00, unlocked=5.00),  # есть заблокированный лот дешевле
        row("Only locked", 3.00, unlocked=None) | {"unlocked_price": None},
        row("Inconsistent", 5.00, unlocked=4.00),  # минимум разблокированных ниже общего
        row("Zero", 0, count=1),
        row("No lots", 1.00, count=0),
        row("Bool count", 1.00, count=True),
        row("Foreign link", 2.00) | {"url": "https://example.com/x"},
        row("No link", 2.50) | {"url": None},
    ]
    quotes = {q.market_hash_name: q for q in source({f"{LIS}/csgo.json": rows}).fetch_quotes()}
    assert set(quotes) == {"Locked cheaper", "Foreign link", "No link"}
    assert quotes["Locked cheaper"].min_price == Decimal("5.00")
    assert quotes["Foreign link"].url is None and quotes["No link"].url is None


@pytest.mark.parametrize(
    "body",
    [
        {"items": ROWS},
        [row("Zero", 0)],
        "<html>Just a moment...</html>",
    ],
)
def test_bad_price_list_is_an_error(body: Any) -> None:
    with pytest.raises(SourceError):
        source({f"{LIS}/csgo.json": body}).fetch_quotes(CS2)


def test_best_prices_mark_items_without_offers() -> None:
    src = source({f"{LIS}/csgo.json": ROWS})
    quotes = src.fetch_best_prices([NAME, "Gone", NAME], CS2)
    assert [(q.market_hash_name, q.min_price, q.quantity) for q in quotes] == [
        (NAME, Decimal("9"), 3),
        ("Gone", None, 0),
    ]


def test_fee_rules() -> None:
    schedule = FeeSchedule.load()
    market = schedule.market("lisskins")
    assert market.currency is Currency.USD and market.transfer_lock_days == 7
    base = Money.of(Decimal("100"), Currency.USD)
    assert [r.rate for r in schedule.select("lisskins", Operation.BUY, base)] == [Decimal("0")]
    assert [r.rate for r in schedule.select("lisskins", Operation.DEPOSIT, base)] == [Decimal("0")]


# ---------- проход ----------


def lis_quote(name: str, price: str | None) -> Quote:
    return Quote(
        source="lisskins",
        app_id=CS2,
        market_hash_name=name,
        currency=Currency.USD,
        kind=QuoteKind.AGGREGATE,
        min_price=None if price is None else Decimal(price),
        quantity=0 if price is None else 4,
        url=None if price is None else f"https://lis-skins.com/market/csgo/{name}/",
        fetched_at=NOW,
    )


class FakeLisSkins:
    """NAME дешевле, чем на Skinport и DMarket."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requested: list[str] = []

    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        if self.fail:
            raise SourceError("csgo.json: HTTP 503")
        self.requested = list(titles)
        return [lis_quote(t, "9.00" if t == NAME else None) for t in titles]


def _pipeline(storage: Storage, lis: FakeLisSkins, out: list[str]) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(NOW),  # type: ignore[arg-type]
        dmarket=FakeDMarket(NOW),  # type: ignore[arg-type]
        lisskins=lis,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: NOW,
    )


def test_lis_skins_is_a_buy_market() -> None:
    lis = FakeLisSkins()
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), lis, out).scan(ScanOptions(limit=2, skinport=True))

    assert lis.requested == [NAME, "X"]
    assert "Lis-Skins: запрошено 2, с лотами 1" in out
    found = [o for o in result.found if o.result.buy_market == "lisskins"]
    assert [(o.name, o.result.sell_market) for o in found] == [(NAME, "skinport")]
    opp = found[0]
    assert opp.result.buy_price == Money.of(Decimal("9.00"), Currency.USD)
    assert opp.buy_url == f"https://lis-skins.com/market/csgo/{NAME}/"
    assert not any(o.result.sell_market == "lisskins" for o in result.found)


def test_lis_skins_failure_skips_only_it() -> None:
    out: list[str] = []
    result = _pipeline(Storage(":memory:"), FakeLisSkins(fail=True), out).scan(
        ScanOptions(limit=2, skinport=True)
    )
    assert "Lis-Skins пропущен: csgo.json: HTTP 503" in out
    assert result.found and all(o.result.buy_market != "lisskins" for o in result.found)
