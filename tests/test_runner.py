import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_scanner import CFG, FX, NAME, q, stats
from test_telegram import TOKEN, make, ok
from typer.testing import CliRunner

import skinedge.cli as cli
from skinedge import feed
from skinedge.cli import app
from skinedge.fees.schedule import FeeSchedule
from skinedge.fx import FxRates
from skinedge.money import Currency
from skinedge.notify.telegram import TelegramClient
from skinedge.paper import PaperTrade
from skinedge.pipeline import Pipeline, ScanOptions, ScanResult
from skinedge.quotes import Quote, Sale, SalesStats, SalesWindow
from skinedge.runner import HEARTBEAT, PRUNED_AT, REPORT_DATE, AlreadyRunning, Runner, RunOptions
from skinedge.sources.base import SourceError
from skinedge.storage import Storage

START = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


class FakeSkinport:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def fetch_quotes(self, app_id: int = 730) -> list[Quote]:
        return [
            q("skinport", "14.01", qty=50, at=self.now),
            q("skinport", "20.00", qty=30, at=self.now, name="X"),
            q("skinport", "0.50", qty=900, at=self.now, name="Cheap"),
            q("skinport", "5.00", qty=1, at=self.now, name="Rare"),
            q("skinport", "3.00", qty=2, at=self.now, name="Old"),
        ]

    def fetch_sales(self, app_id: int = 730) -> list[SalesStats]:
        rows = [stats("13.00", 10, at=self.now, window=w) for w in SalesWindow]
        rows.append(stats("13.00", 10, at=self.now).model_copy(update={"version": "Phase 1"}))
        rows.append(stats("4.00", 10, at=self.now, name="Rare"))
        rows.append(stats("3.00", 10, at=self.now, name="Old"))
        return rows


class FakeDMarket:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.requested: list[str] = []
        self.sales_for: list[str] = []

    def fetch_best_prices(self, titles: list[str], app_id: int = 730) -> list[Quote]:
        self.requested = list(titles)
        return [q("dmarket", "10.00", at=self.now, name=t) for t in titles if t == NAME]

    def fetch_sell_fees(self, app_id: int = 730, offer_type: str = "dmarket") -> list[Any]:
        raise SourceError("нет сети")

    def fetch_last_sales(self, title: str, app_id: int = 730, limit: int = 20) -> list[Sale]:
        self.sales_for.append(title)
        return []

    def fetch_image(self, title: str, app_id: int = 730) -> str | None:
        return f"https://steamcommunity-a.akamaihd.net/economy/image/{len(title)}"


def _old_trade(at: datetime) -> PaperTrade:
    return PaperTrade(
        created_at=at,
        app_id=730,
        market_hash_name="Old",
        buy_market="skinport",
        sell_market="dmarket",
        buy_price=Decimal("3.00"),
        buy_currency=Currency.EUR,
        sell_price=Decimal("4.00"),
        sell_currency=Currency.USD,
        cost_eur=Decimal("3.00"),
        proceeds_eur=Decimal("3.30"),
        profit_eur=Decimal("0.30"),
        roi=Decimal("0.10"),
        usd_per_eur=Decimal("1.10"),
        complete=False,
        lock_days=7,
    )


# ---------- проход ----------


def test_pipeline_saves_only_universe() -> None:
    s = Storage(":memory:")
    s.save_paper(_old_trade(START - timedelta(days=1)))
    dm = FakeDMarket(START)
    out: list[str] = []
    p = Pipeline(
        s,
        FeeSchedule.load(),
        FX,
        skinport=FakeSkinport(START),  # type: ignore[arg-type]
        dmarket=dm,  # type: ignore[arg-type]
        out=out.append,
        clock=lambda: START,
    )
    result = p.run(ScanOptions(limit=2, skinport=True))

    # Две самых ликвидных в диапазоне цен и предмет открытой виртуальной сделки.
    assert dm.requested == [NAME, "X", "Old"]
    assert set(s.recent_quotes("skinport", START)) == {NAME, "X", "Old"}
    rows = s.conn.execute("SELECT market_hash_name, period, version FROM sales_stats").fetchall()
    assert {(r[0], r[1]) for r in rows} == {
        (NAME, "24h"),
        (NAME, "7d"),
        (NAME, "30d"),
        ("Old", "7d"),
    }
    assert all(r[2] is None for r in rows)
    assert "Old" in dm.sales_for  # продажи открытой сделки на DMarket проверяются

    assert [o.name for o in result.found] == [NAME]
    assert result.found[0].result.profit.amount == Decimal("2.77")
    assert result.pairs == 1 and result.recorded == 1 and result.notified == 0
    assert any("Telegram не настроен" in line for line in out)


def test_pipeline_fetch_needs_sources() -> None:
    p = Pipeline(Storage(":memory:"), FeeSchedule.load(), FX)
    with pytest.raises(SourceError):
        p.scan(ScanOptions())


# ---------- база ----------


def test_prune_thins_and_drops() -> None:
    s = Storage(":memory:")
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    d3 = datetime(2026, 10, 23, 0, 10, tzinfo=UTC)
    times = [
        now - timedelta(days=50),
        d3,
        d3 + timedelta(minutes=10),
        d3 + timedelta(hours=5, minutes=40),  # тот же 6-часовой отрезок
        d3 + timedelta(hours=6),  # следующий отрезок
        now - timedelta(days=1),
        now - timedelta(days=1, minutes=-10),
    ]
    s.save_quotes(q("skinport", "1.00", at=t) for t in times)
    s.save_quotes([q("dmarket", "1.00", at=d3 + timedelta(minutes=5))])  # другой источник
    s.save_sales(stats("1.00", 5, at=t, window=w) for t in times[1:3] for w in SalesWindow)

    removed = s.prune(now)
    assert removed == {"quotes": 3, "sales_stats": 4}
    left = [r[0] for r in s.conn.execute("SELECT fetched_at FROM quotes WHERE source='skinport'")]
    assert left == [t.isoformat() for t in (times[1], times[4], times[5], times[6])]
    assert s.conn.execute("SELECT COUNT(*) FROM quotes WHERE source='dmarket'").fetchone()[0] == 1
    assert s.prune(now) == {"quotes": 0, "sales_stats": 0}


def test_meta() -> None:
    s = Storage(":memory:")
    assert s.get_meta("k") is None
    s.set_meta("k", "1")
    s.set_meta("k", "2")
    assert s.get_meta("k") == "2"
    s.delete_meta("k")
    assert s.get_meta("k") is None


# ---------- фоновый запуск ----------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += timedelta(seconds=seconds)


class StubPipeline(Pipeline):
    def __init__(
        self, storage: Storage, telegram: tuple[TelegramClient, str] | None, outcomes: list[Any]
    ) -> None:
        super().__init__(storage, FeeSchedule.load(), FX, telegram=telegram, out=lambda s: None)
        self.outcomes = outcomes
        self.calls = 0

    def run(self, opts: ScanOptions) -> ScanResult:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return ScanResult(at=START)


def _runner(
    outcomes: list[Any],
    clock: Clock,
    storage: Storage | None = None,
    fx: Any = lambda: FX,
    **opts: Any,
) -> tuple[Runner, StubPipeline, list[httpx.Request]]:
    bot, seen = make(ok)
    pipeline = StubPipeline(storage or Storage(":memory:"), (bot, "777"), outcomes)
    options = RunOptions(**{"every": timedelta(minutes=5), "report_hour": None, **opts})
    runner = Runner(
        pipeline, ScanOptions(), options, fx, Decimal("0.01"), clock, clock.sleep, tz=UTC
    )
    return runner, pipeline, seen


def _texts(seen: list[httpx.Request]) -> list[str]:
    return [json.loads(r.content)["text"] for r in seen]


def test_failures_alert_once_and_recover() -> None:
    clock = Clock(START)
    errors = [SourceError("DMarket: HTTP 503 <b>")] * 4
    runner, pipeline, seen = _runner([*errors, None], clock, iterations=5)
    assert runner.run() == 5
    assert pipeline.calls == 5
    assert clock.slept == 4 * 300  # проходы идут по расписанию и после сбоев
    alert, back = _texts(seen)
    assert "3 прохода подряд" in alert and "HTTP 503 &lt;b&gt;" in alert
    assert "снова работает после 4 сбоев" in back
    assert runner.storage.get_meta(HEARTBEAT) is None  # отметка снята при выходе


def test_unexpected_error_does_not_stop() -> None:
    clock = Clock(START)
    runner, _, seen = _runner([ZeroDivisionError("x"), None], clock, iterations=2)
    assert runner.run() == 2
    assert runner.failures == 0 and seen == []


def test_second_instance_refused() -> None:
    clock = Clock(START)
    s = Storage(":memory:")
    s.set_meta(HEARTBEAT, (START - timedelta(minutes=3)).isoformat())
    runner, pipeline, _ = _runner([], clock, storage=s, iterations=1)
    with pytest.raises(AlreadyRunning):
        runner.run()
    assert pipeline.calls == 0

    forced, pipeline, _ = _runner([], clock, storage=s, iterations=1, force=True)
    assert forced.run() == 1

    s.set_meta(HEARTBEAT, (START - timedelta(hours=1)).isoformat())  # аварийно закрытый
    stale, pipeline, _ = _runner([], clock, storage=s, iterations=1)
    assert stale.run() == 1


def test_fx_cache_and_fallback() -> None:
    clock = Clock(START)
    calls: list[int] = []

    def fx() -> FxRates:
        calls.append(1)
        if len(calls) > 1:
            raise SourceError("ЕЦБ: HTTP 500")
        return FX

    runner, pipeline, _ = _runner([], clock, fx=fx, iterations=3, every=timedelta(hours=40))
    runner.run()
    # 0 ч: курс получен; 40 ч: ЕЦБ недоступен, старый курс ещё годен; 80 ч: уже нет.
    assert len(calls) == 3 and pipeline.calls == 2 and runner.failures == 1

    def broken() -> FxRates:
        raise SourceError("ЕЦБ: HTTP 500")

    runner, pipeline, _ = _runner([], Clock(START), fx=broken, iterations=1)
    runner.run()
    assert pipeline.calls == 0 and runner.failures == 1


def test_daily_report_and_prune() -> None:
    from test_scanner import scanner

    from skinedge.paper import record

    clock = Clock(START)  # 08:00 UTC
    s = Storage(":memory:")
    found = scanner().scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00", at=START)},
        {NAME: q("skinport", "14.01", at=START)},
        {NAME: stats("13.00", 10, at=START)},
        START,
    )
    record(s, found, FX, START - timedelta(days=1, minutes=5), CFG)
    s.save_quotes([q("skinport", "14.01", at=START)])
    s.save_sales([stats("13.00", 10, at=START)])
    runner, _, seen = _runner(
        [], clock, storage=s, iterations=4, every=timedelta(hours=1), report_hour=10
    )
    runner.run()
    (report,) = _texts(seen)  # в 08:00 и 09:00 рано, в 10:00 отчёт, в 11:00 уже был
    assert "виртуальные сделки за 30 дн." in report
    assert "Сигналов: 1 (полных расчётов 1)" in report
    assert "24ч: проверено 1, в плюсе 1" in report
    assert "Проходов с прошлого отчёта: 3, с ошибкой: 0." in report
    assert s.get_meta(REPORT_DATE) == "2026-10-26"
    assert s.get_meta(PRUNED_AT) == START.isoformat()  # очистка раз в сутки


def test_rejects_short_interval() -> None:
    with pytest.raises(ValueError):
        _runner([], Clock(START), every=timedelta(minutes=1))


# ---------- CLI ----------


def test_cli_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    def bot(token: str) -> TelegramClient:
        return TelegramClient(token, httpx.Client(transport=httpx.MockTransport(handler)))

    now = datetime.now(UTC)
    monkeypatch.setattr(cli, "TelegramClient", bot)
    monkeypatch.setattr(cli, "SkinportSource", lambda: FakeSkinport(now))
    monkeypatch.setattr(cli, "_dmarket", lambda settings: FakeDMarket(now))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("SKINEDGE_LOG_PATH", str(tmp_path / "logs" / "run.log"))
    monkeypatch.setenv("TG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TG_CHAT_ID", "777")

    args = [
        "run",
        "--iterations",
        "1",
        "--usd-per-eur",
        "1.10",
        "--rub-per-eur",
        "90",
        "--skinport",
    ]
    args += ["--limit", "2", "--no-report"]
    args += ["--no-marketcsgo", "--no-lisskins", "--no-whitemarket", "--no-csdeals"]  # без сети
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Проход 1: сравнено 1, возможностей 1" in result.output
    assert "Выполнено проходов: 1" in result.output
    log = (tmp_path / "logs" / "run.log").read_text(encoding="utf-8")
    assert "Проход 1" in log and TOKEN not in log
    s = Storage(tmp_path / "x.db")
    cards = feed.load(s, datetime.now(UTC))
    assert cards["last"]["ok"] and cards["last"]["pairs"] == 1
    assert [d["name"] for d in cards["deals"]] == ["AK-47 | Redline (Field-Tested)"]
    assert cards["deals"][0]["image"].startswith("https://steamcommunity-a.akamaihd.net/")
    s.close()
    assert "AK-47 | Redline (Field-Tested)" in json.loads(seen[0].content)["text"]

    short = CliRunner().invoke(app, ["run", "--every", "3"])
    assert short.exit_code != 0
