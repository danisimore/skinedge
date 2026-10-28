"""Приложение: сканер кнопкой из интерфейса и карточки найденных сделок."""

import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runner import START, Clock, FakeDMarket, FakeSkinport, StubPipeline
from test_scanner import FX, NAME, q, scanner, stats
from test_ui import SCHEDULE, SPREAD, FakeEcb, call
from typer.testing import CliRunner

import skinedge.cli as cli
from skinedge import feed
from skinedge.cli import app
from skinedge.config import SetupError
from skinedge.pipeline import ScanOptions, ScanResult
from skinedge.runner import HEARTBEAT, PID, Runner, RunOptions
from skinedge.scanner import Opportunity
from skinedge.sources.base import SourceError
from skinedge.storage import Storage
from skinedge.ui.control import ScannerControl
from skinedge.ui.server import App, Rates, serve

IMG = "https://steamcommunity-a.akamaihd.net/economy/image/"


def until(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "не дождался"
        time.sleep(0.02)


def found(at: datetime = START) -> list[Opportunity]:
    return scanner().scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00", at=at)},
        {NAME: q("skinport", "14.01", at=at)},
        {NAME: stats("13.00", 10, at=at)},
        at,
    )


class Images:
    def __init__(self, url: str | None = IMG + "ak", fail: bool = False) -> None:
        self.url, self.fail, self.calls = url, fail, 0

    def __call__(self, name: str, app_id: int) -> str | None:
        self.calls += 1
        if self.fail:
            raise SourceError("DMarket: HTTP 503")
        return self.url


# ---------- карточки ----------


def test_deals_keep_first_seen_and_other_games() -> None:
    s = Storage(":memory:")
    images = Images()
    feed.update_deals(s, START, (730,), found(), images, START)
    (deal,) = feed.load(s, START)["deals"]
    assert (deal["name"], deal["buy_market"], deal["sell_market"]) == (NAME, "dmarket", "skinport")
    assert deal["buy_currency"] == "USD" and deal["sell_currency"] == "EUR"
    assert deal["currency"] == "EUR" and Decimal(deal["profit"]) > 0
    assert deal["image"] == IMG + "ak" and deal["first_seen"] == deal["found_at"]

    later = START + timedelta(minutes=15)
    feed.update_deals(s, later, (730,), found(later), images, later)
    (deal,) = feed.load(s, later)["deals"]
    assert deal["first_seen"] == START.isoformat() and deal["found_at"] == later.isoformat()
    assert images.calls == 1  # картинка запомнена

    feed.update_deals(s, later, (570,), [], images, later)  # другая игра: CS2 остаётся
    assert len(feed.load(s, later)["deals"]) == 1
    feed.update_deals(s, later, (730,), [], images, later)  # CS2 без находок
    assert feed.load(s, later)["deals"] == []


def test_old_deals_hidden() -> None:
    s = Storage(":memory:")
    feed.update_deals(s, START, (730,), found(), None, START)
    assert len(feed.load(s, START + feed.MAX_AGE)["deals"]) == 1
    assert feed.load(s, START + feed.MAX_AGE + timedelta(minutes=1))["deals"] == []


def test_images_only_from_allowed_hosts_and_retried_later() -> None:
    s = Storage(":memory:")
    foreign = Images("https://evil.example/x.png")
    feed.update_deals(s, START, (730,), found(), foreign, START)
    assert feed.load(s, START)["deals"][0]["image"] is None
    feed.update_deals(s, START, (730,), found(), foreign, START + timedelta(days=1))
    assert foreign.calls == 1  # не нашлась — повтор через неделю
    good = Images()
    feed.update_deals(s, START, (730,), found(), good, START + feed.IMAGE_RETRY)
    assert good.calls == 1 and feed.load(s, START)["deals"][0]["image"] == IMG + "ak"
    assert feed.image_url("http://steamcommunity-a.akamaihd.net/x") is None


def test_image_failure_does_not_lose_deals() -> None:
    s = Storage(":memory:")
    broken = Images(fail=True)
    feed.update_deals(s, START, (730,), found(), broken, START)
    (deal,) = feed.load(s, START)["deals"]
    assert deal["image"] is None and s.image(730, NAME) is None  # попробует в следующий раз


# ---------- сканер в потоке ----------


class Finding(StubPipeline):
    def run(self, opts: ScanOptions) -> ScanResult:
        super().run(opts)
        return ScanResult(at=START, found=found(), pairs=1)


def test_runner_publishes_pass_and_stops_on_event() -> None:
    clock = Clock(START)
    stop = threading.Event()
    pipeline = Finding(Storage(":memory:"), None, [SourceError("Skinport: HTTP 503")])
    pipeline.dmarket = FakeDMarket(START)  # type: ignore[assignment]

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if runner.passes == 2:
            stop.set()  # «Остановить» во время ожидания

    runner = Runner(
        pipeline,
        ScanOptions(),
        RunOptions(every=timedelta(minutes=5), report_hour=None),
        lambda: FX,
        SPREAD,
        clock,
        sleep,
        tz=UTC,
        stop=stop,
    )
    assert runner.run() == 2
    cards = feed.load(runner.storage, START)
    assert cards["last"]["n"] == 2 and cards["last"]["ok"] and cards["last"]["found"] == 1
    assert cards["deals"][0]["image"].startswith(IMG)
    assert runner.storage.get_meta(HEARTBEAT) is None and runner.next_at is None


def test_failed_pass_is_reported() -> None:
    clock = Clock(START)
    pipeline = StubPipeline(Storage(":memory:"), None, [SourceError("Skinport: HTTP 503")])
    runner = Runner(
        pipeline,
        ScanOptions(),
        RunOptions(every=timedelta(minutes=5), report_hour=None),
        lambda: FX,
        SPREAD,
        clock,
        clock.sleep,
        tz=UTC,
    )
    runner.tick(1)
    last = feed.load(runner.storage, START)["last"]
    assert not last["ok"] and last["error"] == "Skinport: HTTP 503"


def runner_factory(
    db: Path, cls: type[Runner] = Runner
) -> Callable[[threading.Event, bool], Runner]:
    def make(stop: threading.Event, force: bool) -> Runner:
        pipeline = Finding(Storage(db, threads=True), None, [])
        opts = RunOptions(every=timedelta(minutes=5), report_hour=None, force=force)
        return cls(pipeline, ScanOptions(), opts, lambda: FX, SPREAD, sleep=stop.wait, stop=stop)

    return make


def test_control_start_stop(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    control = ScannerControl(runner_factory(db))
    ui = Storage(db, threads=True)
    control.start()
    until(lambda: control.status()["next_at"] is not None)  # первый проход прошёл
    assert control.status()["state"] == "running"
    assert ui.get_meta(PID) == str(os.getpid()) and ui.get_meta(HEARTBEAT) is not None
    control.start()  # повторный запуск ничего не делает
    control.stop()
    until(lambda: not control.alive)
    assert control.status()["state"] == "stopped" and control.status()["error"] is None
    assert ui.get_meta(HEARTBEAT) is None


def test_control_refuses_second_scanner_and_reports_crash(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    ui = Storage(db, threads=True)
    ui.set_meta(HEARTBEAT, datetime.now(UTC).isoformat())
    ui.set_meta(PID, "1")
    control = ScannerControl(runner_factory(db))
    app_ = App(ui, SCHEDULE, Rates(FakeEcb(), ui, lambda: START), SPREAD, control=control)
    assert call(app_, "GET", "/api/scanner")[1]["state"] == "external"
    status, body = call(app_, "POST", "/api/scanner/start", {})
    assert status == 409 and body["can_force"] and "уже работает" in body["error"]
    status, body = call(app_, "POST", "/api/scanner/start", {"force": True})
    assert status == 200 and body["state"] == "running"
    control.shutdown(ui, timeout=5)
    assert not control.alive and ui.get_meta(HEARTBEAT) is None

    class Broken(Runner):
        def loop(self) -> int:
            raise RuntimeError("диск переполнен")

    crashing = ScannerControl(runner_factory(db, Broken))
    crashing.start()
    until(lambda: not crashing.alive)
    assert "диск переполнен" in crashing.status()["error"]
    assert ui.get_meta(HEARTBEAT) is None


def test_scanner_api_without_keys_and_shutdown() -> None:
    s = Storage(":memory:")

    def no_keys(stop: threading.Event, force: bool) -> Runner:
        raise SetupError("Нет ключей DMarket")

    app_ = App(
        s, SCHEDULE, Rates(FakeEcb(), s, lambda: START), SPREAD, control=ScannerControl(no_keys)
    )
    status, body = call(app_, "POST", "/api/scanner/start", {})
    assert status == 400 and body["error"] == "Нет ключей DMarket"
    assert call(app_, "GET", "/api/ping")[1] == {"app": "SkinEdge"}
    assert call(app_, "GET", "/api/state")[1]["can_quit"] is False
    assert call(app_, "POST", "/api/shutdown")[0] == 404
    quits: list[int] = []
    app_.quit = lambda: quits.append(1)
    assert call(app_, "POST", "/api/shutdown")[1] == {"ok": True} and quits == [1]

    plain = App(s, SCHEDULE, Rates(FakeEcb(), s, lambda: START), SPREAD)
    assert call(plain, "POST", "/api/scanner/start", {})[0] == 404
    state = call(plain, "GET", "/api/scanner")[1]
    assert state["state"] == "stopped" and state["controls"] is False and state["deals"] == []


def test_shutdown_button_stops_server() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    s = Storage(":memory:", threads=True)
    app_ = App(s, SCHEDULE, Rates(FakeEcb(), s, lambda: START), SPREAD)
    thread = threading.Thread(target=serve, args=(app_, port), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    until(lambda: cli._already_open(port))
    with httpx.Client(base_url=base, trust_env=False) as c:
        page = c.get("/")
        assert "Найденные сделки" in page.text
        assert IMG.removesuffix("/economy/image/") in page.headers["content-security-policy"]
        r = c.post("/api/shutdown", headers={"X-SkinEdge": "1", "Origin": base})
        assert r.json() == {"ok": True}
    thread.join(5)
    assert not thread.is_alive()


# ---------- skinedge app ----------


def test_cli_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(cli, "SkinportSource", lambda: FakeSkinport(now))
    monkeypatch.setattr(cli, "_dmarket", lambda settings: FakeDMarket(now))
    monkeypatch.setattr(cli, "MarketCSGOSource", lambda: None)
    monkeypatch.setattr(cli, "LisSkinsSource", lambda: None)
    monkeypatch.setattr(cli, "WhiteMarketSource", lambda: None)
    monkeypatch.setattr(cli, "_official_fx", lambda settings: FX)
    monkeypatch.setattr(cli, "_already_open", lambda port: False)
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("SKINEDGE_LOG_PATH", str(tmp_path / "logs" / "app.log"))
    seen: list[dict[str, Any]] = []

    def fake_serve(ui: App, port: int, ready: Callable[[str], None]) -> None:
        ready(f"http://127.0.0.1:{port}/")
        assert ui.control is not None
        assert call(ui, "POST", "/api/scanner/start", {})[0] == 200
        until(lambda: call(ui, "GET", "/api/scanner")[1]["last"] is not None)
        seen.append(call(ui, "GET", "/api/scanner")[1])

    monkeypatch.setattr(cli, "serve", fake_serve)
    result = CliRunner().invoke(app, ["app", "--port", "8799", "--skinport"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:8799/"]
    state = seen[0]
    assert state["state"] == "running" and state["last"]["ok"]
    assert [d["name"] for d in state["deals"]] == [NAME]
    assert state["deals"][0]["image"].startswith(IMG)
    s = Storage(tmp_path / "x.db")
    assert s.get_meta(HEARTBEAT) is None  # выключение остановило сканер и сняло отметку
    log = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "Сканер запущен из интерфейса" in log and "SkinEdge выключен" in log


def test_cli_app_opens_running_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_LOG_PATH", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr(cli, "_already_open", lambda port: True)
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "serve", lambda *a: pytest.fail("второй экземпляр"))
    result = CliRunner().invoke(app, ["app"])
    assert result.exit_code == 0 and opened == ["http://127.0.0.1:8765/"]
