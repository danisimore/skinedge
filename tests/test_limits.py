"""Скрытый лимит Skinport: пауза после 429, статистика раз в час, игры по очереди."""

from typing import Any

import httpx
import pytest
from test_runner import FX, START, Clock, FakeDMarket, FakeSkinport, StubPipeline, _runner
from test_skinport import fixture_handler, make_source

from skinedge.fees.schedule import FeeSchedule
from skinedge.games import CS2, DOTA2, RUST
from skinedge.pipeline import Pipeline, ScanOptions, ScanResult
from skinedge.runner import games_for_pass
from skinedge.sources.base import RateLimited, RateLimiter, SourceError, request_with_retries
from skinedge.storage import Storage

# Ответ Skinport из лога 27.10.2026.
LIMITED = (
    b'{"errors": [{"id": "rate_limit_exceeded",'
    b' "message": "Your connection is being rate limited."}]}'
)


def test_long_retry_after_is_not_waited() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, content=LIMITED, headers={"Retry-After": "3600"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    slept: list[float] = []
    with pytest.raises(RateLimited, match="через 3600 с") as info:
        request_with_retries(
            client, "GET", "https://x.test/a", limiter=RateLimiter(5, 1.0), sleep=slept.append
        )
    assert info.value.retry_after == 3600
    assert calls["n"] == 1 and slept == []
    # Тело ответа в одну строку.
    assert '"rate_limit_exceeded", "message": "Your connection' in str(info.value)


def test_skinport_pauses_after_429_until_retry_after() -> None:
    limited = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if limited["on"]:
            return httpx.Response(429, content=LIMITED, headers={"Retry-After": "3600"})
        return fixture_handler(request)

    src, seen, clock = make_source(handler)
    with pytest.raises(SourceError, match=r"на паузе до \d\d:\d\d \(60 мин\)"):
        src.fetch_quotes(CS2)
    assert len(seen) == 1 and clock.sleeps == []

    # Другие игры и статистика в паузе не запрашиваются.
    clock.now += 1800
    with pytest.raises(SourceError, match="на паузе после HTTP 429, ещё 30 мин"):
        src.fetch_quotes(DOTA2)
    with pytest.raises(SourceError, match="на паузе"):
        src.fetch_sales(RUST)
    assert len(seen) == 1

    limited["on"] = False
    clock.now += 1800
    assert src.fetch_quotes(CS2)
    assert len(seen) == 2


def test_skinport_429_without_retry_after_pauses_five_minutes() -> None:
    src, seen, clock = make_source(lambda r: httpx.Response(429, content=LIMITED))
    with pytest.raises(SourceError, match=r"\(5 мин\)"):
        src.fetch_quotes(CS2)
    assert len(seen) == 3  # обычные повторы с короткой паузой
    clock.now += 299
    with pytest.raises(SourceError, match="на паузе"):
        src.fetch_quotes(CS2)
    assert len(seen) == 3


def test_skinport_sales_cached_for_an_hour() -> None:
    src, seen, clock = make_source(fixture_handler)
    first = src.fetch_sales(CS2)
    clock.now += 3599
    again = src.fetch_sales(CS2)
    assert len(seen) == 1
    # Из кеша приходит время настоящей загрузки, а не текущее.
    assert {s.fetched_at for s in again} == {first[0].fetched_at}
    clock.now += 2
    src.fetch_sales(CS2)
    assert len(seen) == 2


# ---------- проход ----------


def test_cached_skinport_sales_are_saved_once() -> None:
    s = Storage(":memory:")
    p = Pipeline(
        s,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(START),  # type: ignore[arg-type]
        dmarket=FakeDMarket(START),  # type: ignore[arg-type]
        out=lambda line: None,
        clock=lambda: START,
    )
    p.scan(ScanOptions(limit=2))
    rows = s.conn.execute("SELECT COUNT(*) FROM sales_stats WHERE source='skinport'").fetchone()[0]
    assert rows > 0
    # Та же загрузка (тот же fetched_at) второй раз не записывается.
    p.scan(ScanOptions(limit=2))
    again = s.conn.execute("SELECT COUNT(*) FROM sales_stats WHERE source='skinport'").fetchone()
    assert again[0] == rows


# ---------- игры по очереди ----------


def test_games_for_pass() -> None:
    three = (CS2, DOTA2, RUST)
    assert [games_for_pass(three, n) for n in (1, 2, 3, 4)] == [
        (CS2, DOTA2),
        (CS2, RUST),
        (CS2, DOTA2),
        (CS2, RUST),
    ]
    assert games_for_pass((CS2, DOTA2), 2) == (CS2, DOTA2)
    assert games_for_pass((RUST,), 1) == (RUST,)


class GamesPipeline(StubPipeline):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.games: list[tuple[int, ...]] = []

    def run(self, opts: ScanOptions) -> ScanResult:
        self.games.append(opts.games)
        return super().run(opts)


def test_runner_rotates_games() -> None:
    clock = Clock(START)
    runner, stub, _ = _runner([], clock, iterations=3)
    pipeline = GamesPipeline(stub.storage, stub.telegram, [])
    runner.pipeline = pipeline
    runner.scan = ScanOptions(games=(CS2, DOTA2, RUST))
    assert runner.run() == 3
    assert pipeline.games == [(CS2, DOTA2), (CS2, RUST), (CS2, DOTA2)]
    assert runner.scan.games == (CS2, DOTA2, RUST)  # исходные настройки не меняются
