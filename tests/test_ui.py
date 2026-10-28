import json
import socket
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from test_runner import START, Clock, StubPipeline
from test_scanner import FX

from skinedge.fees.schedule import FeeSchedule
from skinedge.fx import FxRates
from skinedge.games import CS2
from skinedge.journal import NoRate, Trade, payout_at, tradable_from, valuate
from skinedge.money import Currency, Money
from skinedge.pipeline import ScanOptions, ScanResult
from skinedge.quotes import Quote, QuoteKind
from skinedge.runner import HEARTBEAT, Runner, RunOptions
from skinedge.sources.base import SourceError
from skinedge.storage import Storage
from skinedge.tuning import KEY, ScanSettings, load, reset, save
from skinedge.ui.server import App, Rates, make_handler

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
NAME = "AK-47 | Redline (Field-Tested)"
SCHEDULE = FeeSchedule.load()
SPREAD = Decimal("0.01")


def settings(**kw: Any) -> ScanSettings:
    base: dict[str, Any] = {
        "min_price": "2",
        "max_price": "50",
        "limit": 150,
        "min_sales_7d": 10,
        "min_profit": "1",
        "min_roi": "0.08",
    }
    return ScanSettings.model_validate(base | kw)


def trade(**kw: Any) -> Trade:
    base: dict[str, Any] = {
        "app_id": CS2,
        "market_hash_name": NAME,
        "bought_at": date(2026, 10, 28),
        "buy_market": "lisskins",
        "buy_price": Decimal("9.00"),
        "buy_currency": Currency.USD,
        "buy_usd_per_eur": Decimal("1.10"),
        "target_market": "skinport",
        "target_price": Decimal("13.00"),
        "target_currency": Currency.EUR,
        "target_at": NOW,
        "created_at": NOW,
    }
    return Trade.model_validate(base | kw)


# ---------- настройки сканера ----------


def test_settings_round_trip_and_apply() -> None:
    s = Storage(":memory:")
    assert load(s) is None
    save(s, settings())
    tuned = load(s)
    assert tuned == settings()
    opts = tuned.apply(ScanOptions(csfloat=7))
    assert (opts.limit, opts.min_price, opts.max_price) == (150, Decimal("2"), Decimal("50"))
    cfg = opts.config
    assert (cfg.min_sales_7d, cfg.min_profit, cfg.min_roi) == (10, Decimal("1"), Decimal("0.08"))
    assert opts.csfloat == 7  # остальные параметры не трогаются
    reset(s)
    assert load(s) is None


def test_bad_settings_are_rejected_and_broken_record_ignored() -> None:
    with pytest.raises(ValueError, match="больше минимальной"):
        settings(max_price="1")
    with pytest.raises(ValueError):
        settings(limit=0)
    s = Storage(":memory:")
    s.set_meta(KEY, '{"min_price": "abc"}')
    assert load(s) is None


def test_runner_uses_saved_settings(caplog: pytest.LogCaptureFixture) -> None:
    class Recording(StubPipeline):
        seen: list[ScanOptions]

        def run(self, opts: ScanOptions) -> ScanResult:
            self.seen.append(opts)
            return super().run(opts)

    clock = Clock(START)
    pipeline = Recording(Storage(":memory:"), None, [])
    pipeline.seen = []
    runner = Runner(
        pipeline,
        ScanOptions(limit=300),
        RunOptions(every=timedelta(minutes=5), report_hour=None),
        lambda: FX,
        SPREAD,
        clock,
        clock.sleep,
        tz=UTC,
    )
    with caplog.at_level("INFO"):
        runner.tick(1)
        save(pipeline.storage, settings())
        runner.tick(2)
        runner.tick(3)
        reset(pipeline.storage)
        runner.tick(4)
    assert [o.limit for o in pipeline.seen] == [300, 150, 150, 300]
    assert pipeline.seen[1].config.min_roi == Decimal("0.08")
    logged = [r.getMessage() for r in caplog.records if "Настройки из интерфейса" in r.getMessage()]
    assert len(logged) == 2 and "сброшены" in logged[1]


# ---------- журнал ----------


def test_profit_with_fees_and_rates() -> None:
    v = valuate(trade(), SCHEDULE, Decimal("1.20"), SPREAD)
    # 9.00 USD по курсу покупки 1.10 и спреду 1%: 8.2636 → 8.27 EUR.
    assert v.cost == Money.of("8.27", Currency.EUR)
    # Skinport: 13.00 − 8% = 11.96 EUR, вывод на счёт 0%.
    assert v.proceeds == Money.of("11.96", Currency.EUR)
    assert v.profit == Money.of("3.69", Currency.EUR)
    assert v.roi == Decimal("0.4462")
    assert v.complete and not v.wallet
    assert any("Комиссия продавца" in line for line in v.lines)


def test_sold_trade_uses_sale_and_its_rate() -> None:
    t = trade(
        sold_at=date(2026, 11, 5),
        sell_market="dmarket",
        sell_price=Decimal("14.00"),
        sell_currency=Currency.USD,
        sell_usd_per_eur=Decimal("1.00"),
    )
    v = valuate(t, SCHEDULE, Decimal("1.50"), SPREAD)
    assert v.sell_market == "dmarket"
    assert v.sell_price == Money.of("14.00", Currency.USD)
    assert v.proceeds.amount < Decimal("14.00")  # комиссия и вывод DMarket
    assert v.proceeds.amount > Decimal("10")  # курс продажи 1.00, а не текущий 1.50
    assert payout_at(t, SCHEDULE) == date(2026, 11, 12)
    assert tradable_from(t, SCHEDULE) == date(2026, 11, 4)


def test_usd_trade_without_rate_is_not_valued() -> None:
    with pytest.raises(NoRate):
        valuate(trade(buy_usd_per_eur=None), SCHEDULE, None, SPREAD)
    eur = trade(buy_market="skinport", buy_currency=Currency.EUR, buy_usd_per_eur=None)
    assert valuate(eur, SCHEDULE, None, SPREAD).profit.currency is Currency.EUR


def test_wallet_and_unknown_fees_are_flagged() -> None:
    steam = trade(target_market="steam", target_price=Decimal("15"))
    assert valuate(steam, SCHEDULE, Decimal("1.1"), SPREAD).wallet
    lis = trade(
        buy_market="skinport",
        buy_currency=Currency.EUR,
        target_market="lisskins",
        target_currency=Currency.USD,
    )
    assert not valuate(lis, SCHEDULE, Decimal("1.1"), SPREAD).complete


def test_trade_needs_whole_sale_and_sane_dates() -> None:
    with pytest.raises(ValueError, match="дата, площадка и цена"):
        trade(sold_at=date(2026, 11, 1))
    with pytest.raises(ValueError, match="раньше даты покупки"):
        trade(
            sold_at=date(2026, 10, 1),
            sell_market="skinport",
            sell_price=Decimal("1"),
            sell_currency=Currency.EUR,
        )


def test_storage_keeps_trades() -> None:
    s = Storage(":memory:")
    first = s.save_trade(trade())
    second = s.save_trade(trade(market_hash_name="Old", bought_at=date(2026, 10, 1)))
    stored = s.trade(first)
    assert stored is not None and stored.buy_price == Decimal("9.00")
    assert stored.target_at == NOW
    s.save_trade(stored.model_copy(update={"note": "float 0.15"}))
    assert s.trade(first).note == "float 0.15"  # type: ignore[union-attr]
    assert [t.id for t in s.trades()] == [first, second]
    with pytest.raises(KeyError):
        s.save_trade(stored.model_copy(update={"id": 999}))
    assert s.delete_trade(second) and not s.delete_trade(second)


# ---------- API ----------


class FakeEcb:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self) -> FxRates:
        self.calls += 1
        if self.fail:
            raise SourceError("ЕЦБ недоступен")
        return FxRates(rates={Currency.USD: Decimal("1.10")}, spread=SPREAD, source="ECB")


def make_app(storage: Storage | None = None, ecb: FakeEcb | None = None) -> App:
    s = storage or Storage(":memory:")
    rates = Rates(ecb or FakeEcb(), s, lambda: NOW)
    return App(s, SCHEDULE, rates, SPREAD, clock=lambda: NOW)


def call(app: App, method: str, path: str, body: Any = None, **query: str) -> tuple[int, Any]:
    raw = None if body is None else json.dumps(body).encode()
    r = app.handle(method, path, query, raw)
    return r.status, json.loads(r.body) if r.content_type.startswith("application/json") else r.body


FORM = {
    "app_id": 730,
    "name": NAME,
    "bought_at": "2026-10-28",
    "buy_market": "lisskins",
    "buy_price": "9,00",
    "target_market": "skinport",
    "target_price": "13",
    "note": " из сигнала ",
}


def test_page_and_state() -> None:
    app = make_app()
    status, page = call(app, "GET", "/")
    assert status == 200 and b"<title>SkinEdge</title>" in page
    app.storage.set_meta(HEARTBEAT, (NOW - timedelta(minutes=3)).isoformat())
    status, state = call(app, "GET", "/api/state")
    assert status == 200
    assert {"id": "lisskins", "label": "Lis-Skins", "currency": "USD"}.items() <= next(
        m for m in state["markets"] if m["id"] == "lisskins"
    ).items()
    assert state["settings_saved"] is False and state["settings"]["limit"] == 300
    assert state["fx"]["usd_per_eur"] == "1.10" and state["scanner"]["running"] is True
    assert call(app, "GET", "/nope")[0] == 404


def test_settings_api() -> None:
    app = make_app()
    status, body = call(app, "PUT", "/api/settings", {**settings().model_dump(mode="json")})
    assert status == 200 and body["settings_saved"] and body["settings"]["min_roi"] == "0.08"
    status, body = call(app, "PUT", "/api/settings", {**body["settings"], "min_price": "-1"})
    assert status == 400 and body["fields"] == {"min_price": "должно быть больше 0"}
    status, body = call(app, "DELETE", "/api/settings")
    assert status == 200 and not body["settings_saved"]


def test_trade_lifecycle() -> None:
    app = make_app()
    status, created = call(app, "POST", "/api/trades", FORM)
    assert status == 201
    assert created["buy_currency"] == "USD" and created["target_currency"] == "EUR"
    assert created["buy_usd_per_eur"] == "1.10" and created["note"] == "из сигнала"
    assert created["status"] == "locked" and created["tradable_from"] == "2026-11-04"
    assert created["valuation"]["profit"] == "3.69"

    status, journal = call(app, "GET", "/api/trades")
    assert journal["summary"]["open"] == 1 and journal["summary"]["open_profit"] == "3.69"

    sale = {**FORM, "sold_at": "2026-11-05", "sell_market": "skinport", "sell_price": "12.50"}
    status, sold = call(app, "PUT", f"/api/trades/{created['id']}", sale)
    assert status == 200 and sold["status"] == "sold" and sold["payout_at"] == "2026-11-12"
    # 12.50 − 8% = 11.50 EUR, стоимость 8.27 EUR.
    assert sold["valuation"]["profit"] == "3.23"
    summary = call(app, "GET", "/api/trades")[1]["summary"]
    assert (summary["sold"], summary["realized_profit"], summary["open"]) == (1, "3.23", 0)

    status, back = call(app, "PUT", f"/api/trades/{created['id']}", FORM)
    assert back["status"] == "locked" and back["sell_usd_per_eur"] is None

    assert call(app, "DELETE", f"/api/trades/{created['id']}")[0] == 200
    assert call(app, "DELETE", f"/api/trades/{created['id']}")[0] == 404


def test_trade_form_errors_in_russian() -> None:
    app = make_app()
    status, body = call(app, "POST", "/api/trades", {**FORM, "buy_market": "", "buy_price": "x"})
    assert status == 400 and body["fields"] == {"buy_market": "выбери площадку"}
    status, body = call(app, "POST", "/api/trades", {**FORM, "buy_price": "x", "name": ""})
    assert status == 400
    assert body["fields"] == {"buy_price": "нужно число", "name": "заполни поле"}
    created = call(app, "POST", "/api/trades", FORM)[1]
    status, body = call(app, "PUT", f"/api/trades/{created['id']}", {**FORM, "sell_price": "5"})
    assert status == 400 and set(body["fields"]) == {"sell_market", "sold_at"}


def test_rate_falls_back_to_saved_one() -> None:
    s = Storage(":memory:")
    make_app(s).rates.current()  # ЕЦБ ответил, курс сохранён
    rate, source, _ = make_app(s, FakeEcb(fail=True)).rates.current()
    assert rate == Decimal("1.10") and source == "сохранённый ЕЦБ"
    status, created = call(
        make_app(Storage(":memory:"), FakeEcb(fail=True)), "POST", "/api/trades", FORM
    )
    assert status == 201 and created["valuation"] is None and "курса USD" in created["error"]


def test_names_and_quote_hints() -> None:
    s = Storage(":memory:")
    s.save_quotes(
        [
            Quote(
                source="skinport",
                app_id=CS2,
                market_hash_name=NAME,
                currency=Currency.EUR,
                kind=QuoteKind.AGGREGATE,
                min_price=Decimal("12.80"),
                quantity=5,
                fetched_at=NOW - timedelta(minutes=10),
            )
        ]
    )
    app = make_app(s)
    assert call(app, "GET", "/api/names", q="redl")[1]["names"] == [NAME]
    assert call(app, "GET", "/api/names", q="r")[1]["names"] == []
    quote = call(app, "GET", "/api/quote", name=NAME, market="skinport")[1]["quote"]
    assert quote["price"] == "12.80" and quote["currency"] == "EUR"
    assert call(app, "GET", "/api/quote", name=NAME, market="dmarket")[1]["quote"] is None


# ---------- HTTP ----------


def test_http_rejects_foreign_requests() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = make_app(Storage(":memory:", threads=True))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, trust_env=False) as c:
            assert c.get("/").status_code == 200
            assert c.get("/", headers={"Host": "evil.example"}).status_code == 403
            assert c.post("/api/trades", json=FORM).status_code == 403
            assert (
                c.post(
                    "/api/trades",
                    json=FORM,
                    headers={"X-SkinEdge": "1", "Origin": "http://evil.example"},
                ).status_code
                == 403
            )
            ok = c.post("/api/trades", json=FORM, headers={"X-SkinEdge": "1", "Origin": base})
            assert ok.status_code == 201
            assert "frame-ancestors 'none'" in ok.headers["content-security-policy"]
    finally:
        server.shutdown()
        server.server_close()
