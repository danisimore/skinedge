"""Тесты адаптера Skinport.

Фикстуры в tests/fixtures собраны вручную по схеме из документации
(https://docs.skinport.com/items, https://docs.skinport.com/sales/history),
а не записаны с живого API: из среды разработки API недоступен.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import brotli
import httpx
import pytest

from skinedge.money import Currency
from skinedge.quotes import QuoteKind, SalesWindow
from skinedge.sources.base import RateLimiter, SourceError
from skinedge.sources.skinport import SkinportSource

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def br_response(
    body: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        content=brotli.compress(body),
        headers={"Content-Encoding": "br", "Content-Type": "application/json", **(headers or {})},
    )


def make_source(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock | None = None
) -> tuple[SkinportSource, list[httpx.Request], FakeClock]:
    clock = clock or FakeClock()
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return SkinportSource(client, clock=clock, sleep=clock.sleep), seen, clock


def fixture_handler(request: httpx.Request) -> httpx.Response:
    name = "skinport_items.json" if request.url.path.endswith("/items") else "skinport_sales.json"
    return br_response((FIXTURES / name).read_bytes())


def test_items_parsed_with_exact_decimals() -> None:
    src, _, _ = make_source(fixture_handler)
    quotes = {q.market_hash_name: q for q in src.fetch_quotes(730)}

    ak = quotes["AK-47 | Redline (Field-Tested)"]
    assert ak.min_price == Decimal("28.14")
    assert ak.median_price == Decimal("30.9")
    assert ak.quantity == 412
    assert ak.currency is Currency.EUR
    assert ak.kind is QuoteKind.AGGREGATE
    assert ak.source_updated_at == datetime.fromtimestamp(1793030000, tz=UTC)
    assert ak.url == "https://skinport.com/item/csgo/ak-47-redline-field-tested"

    empty = quotes["Sticker | Rare Example"]
    assert empty.min_price is None and empty.quantity == 0

    assert "Broken Row" not in quotes  # некорректная строка пропущена


def test_request_headers_and_params() -> None:
    src, seen, _ = make_source(fixture_handler)
    src.fetch_quotes(730)
    req = seen[0]
    assert req.url.host == "api.skinport.com"
    assert req.url.path == "/v1/items"
    assert req.headers["Accept-Encoding"] == "br"
    assert dict(req.url.params) == {"app_id": "730", "currency": "EUR", "tradable": "1"}


def test_cache_for_five_minutes() -> None:
    src, seen, clock = make_source(fixture_handler)
    src.fetch_quotes(730)
    clock.now += 299
    src.fetch_quotes(730)
    assert len(seen) == 1
    clock.now += 2
    src.fetch_quotes(730)
    assert len(seen) == 2


def test_sales_history_windows_and_versions() -> None:
    src, seen, _ = make_source(fixture_handler)
    stats = src.fetch_sales(730, names=["AK-47 | Redline (Field-Tested)"])
    assert seen[0].url.params["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert len(stats) == 8
    ak7 = next(
        s for s in stats if s.market_hash_name.startswith("AK") and s.window is SalesWindow.D7
    )
    assert ak7.median == Decimal("30.2") and ak7.volume == 402
    k24 = next(s for s in stats if s.version == "Phase 2" and s.window is SalesWindow.H24)
    assert k24.median is None and k24.volume == 0


def test_retry_after_429() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "42"})
        return fixture_handler(request)

    src, _, clock = make_source(handler)
    assert src.fetch_quotes(730)
    assert 42 in clock.sleeps


def test_server_error_exhausts_retries() -> None:
    src, seen, _ = make_source(lambda r: httpx.Response(503))
    with pytest.raises(SourceError, match="попытки исчерпаны"):
        src.fetch_quotes(730)
    assert len(seen) == 3


def test_client_error_is_not_retried() -> None:
    src, seen, _ = make_source(lambda r: httpx.Response(400, json={"errors": ["bad"]}))
    with pytest.raises(SourceError, match="HTTP 400"):
        src.fetch_quotes(730)
    assert len(seen) == 1


def test_network_error_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return fixture_handler(request)

    src, _, _ = make_source(handler)
    assert src.fetch_quotes(730)


def test_non_list_response_rejected() -> None:
    src, _, _ = make_source(lambda r: br_response(b'{"error": "x"}'))
    with pytest.raises(SourceError, match="ожидался список"):
        src.fetch_quotes(730)


def test_all_rows_invalid_rejected() -> None:
    src, _, _ = make_source(lambda r: br_response(b'[{"foo": 1}]'))
    with pytest.raises(SourceError, match="ни одной"):
        src.fetch_quotes(730)


def test_wrong_currency_rows_skipped() -> None:
    body = b'[{"market_hash_name": "X", "currency": "USD", "min_price": 1.0, "quantity": 1}]'
    src, _, _ = make_source(lambda r: br_response(body))
    with pytest.raises(SourceError):
        src.fetch_quotes(730)


def test_rate_limiter_waits_for_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8, 300.0, clock=clock, sleep=clock.sleep)
    for _ in range(8):
        limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()
    assert clock.sleeps == [300.0]


def test_quote_staleness() -> None:
    src, _, _ = make_source(fixture_handler)
    ak = next(q for q in src.fetch_quotes(730) if q.market_hash_name.startswith("AK"))
    updated = datetime.fromtimestamp(1793030000, tz=UTC)
    assert not ak.is_stale(updated + timedelta(minutes=10), timedelta(minutes=15))
    assert ak.is_stale(updated + timedelta(minutes=16), timedelta(minutes=15))


def test_cli_fetch_saves_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from skinedge import cli
    from skinedge.storage import Storage

    src, _, _ = make_source(fixture_handler)
    monkeypatch.setattr(cli, "SkinportSource", lambda: src)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(tmp_path / "h.db"))
    result = CliRunner().invoke(cli.app, ["fetch", "skinport"])
    assert result.exit_code == 0, result.output
    assert "3 предметов, из них с лотами 2" in result.output
    assert "Статистика продаж: 8 записей" in result.output
    s = Storage(tmp_path / "h.db")
    assert s.latest_quotes("AK-47 | Redline (Field-Tested)")[0].min_price == Decimal("28.14")
