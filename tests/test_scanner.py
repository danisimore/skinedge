from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skinedge.cli import app
from skinedge.fees.schedule import FeeSchedule
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.paper import PaperTrade, evaluate, record, summarize
from skinedge.quotes import Quote, QuoteKind, Sale, SalesStats, SalesWindow
from skinedge.scanner import ScanConfig, Scanner, estimate_sell, stats_from_sales
from skinedge.storage import Storage

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
NAME = "AK-47 | Redline (Field-Tested)"
FX = FxRates(
    rates={Currency.USD: Decimal("1.10"), Currency.RUB: Decimal("90.00")}, spread=Decimal("0.01")
)
CFG = ScanConfig()
SCAN_ARGS = ["scan", "--no-fetch", "--usd-per-eur", "1.10", "--rub-per-eur", "90"]


def q(
    source: str,
    price: str | None,
    qty: int = 20,
    at: datetime = NOW,
    name: str = NAME,
) -> Quote:
    return Quote(
        source=source,
        app_id=730,
        market_hash_name=name,
        currency=Currency.EUR if source == "skinport" else Currency.USD,
        kind=QuoteKind.AGGREGATE,
        min_price=None if price is None else Decimal(price),
        quantity=qty,
        fetched_at=at,
    )


def stats(
    median: str,
    volume: int,
    at: datetime = NOW,
    name: str = NAME,
    source: str = "skinport",
    window: SalesWindow = SalesWindow.D7,
) -> SalesStats:
    return SalesStats(
        source=source,
        app_id=730,
        market_hash_name=name,
        currency=Currency.EUR if source == "skinport" else Currency.USD,
        window=window,
        min=None,
        max=None,
        avg=None,
        median=Decimal(median),
        volume=volume,
        fetched_at=at,
    )


def scanner() -> Scanner:
    return Scanner(FeeSchedule.load(), FX, CFG)


# ---------- оценка цены продажи ----------


def tm(price: str, currency: Currency = Currency.RUB) -> Quote:
    return q("marketcsgo", price).model_copy(update={"currency": currency})


def test_quotes_in_former_currency_are_skipped() -> None:
    """Market.CSGO перешёл на рубли: котировка, сохранённая ещё в EUR, не участвует."""
    sell = {NAME: q("skinport", "14.01")}, {NAME: stats("13.00", 10)}
    old = {NAME: tm("9.00", Currency.EUR)}
    assert scanner().scan("marketcsgo", "skinport", old, *sell, NOW) == []
    assert scanner().candidates("marketcsgo", "skinport", old, sell[0], NOW, 10) == []
    found = scanner().scan("marketcsgo", "skinport", {NAME: tm("810.00")}, *sell, NOW)
    assert [o.result.buy_price for o in found] == [Money.of("810.00", Currency.RUB)]


def test_sales_in_other_currency_are_ignored() -> None:
    eur = stats("27.00", 10, source="marketcsgo").model_copy(update={"currency": Currency.EUR})
    assert estimate_sell(tm("2700.00"), eur, CFG) is None  # 27 EUR с 2700 RUB не сравнить
    rub = eur.model_copy(update={"currency": Currency.RUB, "median": Decimal("2430.00")})
    est = estimate_sell(tm("2700.00"), rub, CFG)
    assert est is not None and est.price == Money.of("2430.00", Currency.RUB)


def test_estimate_uses_median_when_lower() -> None:
    est = estimate_sell(q("skinport", "14.01"), stats("13.00", 10), CFG)
    assert est is not None and est.price == Money.of("13.00", Currency.EUR)


def test_estimate_undercuts_ask() -> None:
    est = estimate_sell(q("skinport", "14.01"), stats("20", 10), CFG)
    assert est is not None and est.price == Money.of("14.00", Currency.EUR)


def test_estimate_requires_sales_volume() -> None:
    assert estimate_sell(q("skinport", "14.01"), stats("13", 4), CFG) is None


def test_estimate_requires_confirmed_sales() -> None:
    assert estimate_sell(q("dmarket", "14.01", qty=500), None, CFG) is None


def test_preliminary_estimate_requires_listings() -> None:
    assert estimate_sell(q("dmarket", "14.01", qty=4), None, CFG, require_sales=False) is None
    est = estimate_sell(q("dmarket", "14.01", qty=5), None, CFG, require_sales=False)
    assert est is not None and est.price == Money.of("14.00", Currency.USD)
    assert "предварительно" in est.basis


def test_estimate_no_listings() -> None:
    assert estimate_sell(q("dmarket", None, qty=0), stats("1", 9, source="dmarket"), CFG) is None


def _sale(price: str, ago: timedelta) -> Sale:
    return Sale(
        source="dmarket",
        app_id=730,
        market_hash_name=NAME,
        currency=Currency.USD,
        price=Decimal(price),
        sold_at=NOW - ago,
        fetched_at=NOW,
    )


def test_stats_from_sales_counts_episodes() -> None:
    d = timedelta(days=1)
    sales = [
        _sale("1.40", 6 * d),
        _sale("1.51", 3 * d),
        _sale("1.07", 2 * d),
        _sale("1.36", 1 * d),
        _sale("9", 20 * d),  # вне окна 7 дней
    ]
    st = stats_from_sales(sales, "dmarket", 730, NAME, Currency.USD, NOW)
    assert st.volume == 4
    assert st.median == Decimal("1.36")  # медиана эпизодов 1.38, но свежий эпизод 1.36
    assert st.min == Decimal("1.07") and st.max == Decimal("1.51")
    empty = stats_from_sales([_sale("9", 20 * d)], "dmarket", 730, NAME, Currency.USD, NOW)
    assert empty.volume == 0 and empty.median is None


def test_bulk_purchase_is_one_episode() -> None:
    # Случай из живой проверки (Stockholm 2021 capsule): 12 покупок за минуту по 2.60–3.03,
    # затем 8 продаж в заявки по ~1.91. Это два эпизода, а не 20 продаж.
    night = NOW - timedelta(hours=19)
    bulk = [
        _sale(p, NOW - night + timedelta(seconds=i))
        for i, p in enumerate(["2.66"] * 6 + ["3.03"] * 6)
    ]
    fresh = [_sale(p, timedelta(hours=3, seconds=i)) for i, p in enumerate(["1.91"] * 7 + ["2.17"])]
    st = stats_from_sales(bulk + fresh, "dmarket", 730, NAME, Currency.USD, NOW)
    assert st.volume == 2
    assert st.median == Decimal("1.91")  # не выше свежего эпизода
    sell = q("dmarket", "2.84", qty=67)
    assert estimate_sell(sell, st, CFG) is None  # двух эпизодов мало для уверенной продажи


# ---------- сканер ----------


def test_dmarket_to_skinport_full_calculation() -> None:
    # Покупка 10.00 USD → 10/1.10·1.01 = 9.1818 → 9.19 EUR (вверх).
    # Продажа: min(14.00, медиана 13.00) = 13.00 − 8% = 11.96 EUR, вывод на банк 0%.
    found = scanner().scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00")},
        {NAME: q("skinport", "14.01")},
        {NAME: stats("13.00", 10)},
        NOW,
    )
    assert len(found) == 1
    r = found[0].result
    assert r.total_cost == Money.of("9.19", Currency.EUR)
    assert r.net_proceeds == Money.of("11.96", Currency.EUR)
    assert r.profit == Money.of("2.77", Currency.EUR)
    assert r.complete
    assert "медиана" in found[0].sell.basis


def test_skinport_to_dmarket_with_withdraw() -> None:
    # 14.00 USD − 10% = 12.60 USD; вывод Visa 0.75% (0.09) + 0.35 → 12.16 USD
    # → 12.16/1.10·0.99 = 10.94 EUR (вниз).
    args = (
        "skinport",
        "dmarket",
        {NAME: q("skinport", "10.00")},
        {NAME: q("dmarket", "14.01")},
        {NAME: stats("20", 8, source="dmarket")},
        NOW,
    )
    found = scanner().scan(*args)
    r = found[0].result
    assert r.net_proceeds == Money.of("10.94", Currency.EUR)
    assert r.profit == Money.of("0.94", Currency.EUR)
    assert r.complete
    assert not any("неполный" in f for f in found[0].flags)

    # SEPA: 6.34 USD с каждой продажи съедает всю прибыль.
    sepa = Scanner(FeeSchedule.load().with_withdraw("dmarket", "sepa"), FX, CFG)
    assert sepa.scan(*args) == []
    with pytest.raises(ValueError, match="mir"):
        FeeSchedule.load().with_withdraw("dmarket", "mir")


def test_thresholds_and_staleness() -> None:
    s = scanner()
    sell = {NAME: q("skinport", "14.01")}
    st = {NAME: stats("13.00", 10)}
    old = NOW - timedelta(minutes=31)
    assert s.scan("dmarket", "skinport", {NAME: q("dmarket", "10", at=old)}, sell, st, NOW) == []
    # Прибыль меньше 0.50 EUR.
    assert s.scan("dmarket", "skinport", {NAME: q("dmarket", "12.60")}, sell, st, NOW) == []
    # Нет лотов на покупку.
    assert s.scan("dmarket", "skinport", {NAME: q("dmarket", None, 0)}, sell, st, NOW) == []


def test_flags_and_order() -> None:
    doppler = "★ Karambit | Doppler (Factory New)"
    found = scanner().scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00"), doppler: q("dmarket", "100", name=doppler)},
        {NAME: q("skinport", "11.40"), doppler: q("skinport", "200", name=doppler)},
        {NAME: stats("20", 10), doppler: stats("180", 10, name=doppler)},
        NOW,
    )
    assert [o.name for o in found] == [doppler, NAME]  # выше ROI первым
    flags = " ".join(found[0].flags)
    assert "фаз" in flags and "подозрительно" in flags
    assert found[0].sell_url is not None and "skinport.com" in found[0].sell_url


def test_dmarket_ask_without_real_sales_is_not_an_opportunity() -> None:
    # Случай из живой проверки: лот на DMarket 2.73 USD, а реальные сделки по ~1.36 USD.
    sticker = "Sticker | Cloud9 (Glitter) | Copenhagen 2024"
    buy = {sticker: q("skinport", "1.16", name=sticker)}
    sell = {sticker: q("dmarket", "2.73", qty=10, name=sticker)}
    s = scanner()
    assert s.candidates("skinport", "dmarket", buy, sell, NOW, 10) == [sticker]
    assert s.scan("skinport", "dmarket", buy, sell, {}, NOW) == []  # продажи не проверены
    real = {sticker: stats("1.36", 14, name=sticker, source="dmarket")}
    assert s.scan("skinport", "dmarket", buy, sell, real, NOW) == []  # по медиане убыток


def test_candidates_skip_hopeless_and_respect_limit() -> None:
    names = [f"item{i}" for i in range(3)]
    buy = {n: q("skinport", "10.00", name=n) for n in names}
    sell = {n: q("dmarket", p, name=n) for n, p in zip(names, ["30", "20", "11"], strict=True)}
    s = scanner()
    assert s.candidates("skinport", "dmarket", buy, sell, NOW, 5) == ["item0", "item1"]
    assert s.candidates("skinport", "dmarket", buy, sell, NOW, 1) == ["item0"]


def test_falling_price_flag() -> None:
    args = (
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00")},
        {NAME: q("skinport", "14.01")},
        {NAME: stats("13.00", 10)},
        NOW,
    )
    falling = scanner().scan(*args, trends={NAME: Decimal("-0.20")})
    assert any("Цена падает" in f and "20.0%" in f for f in falling[0].flags)
    stable = scanner().scan(*args, trends={NAME: Decimal("-0.01")})
    assert not any("Цена падает" in f for f in stable[0].flags)


# ---------- paper trading ----------


def _storage_with_signal() -> tuple[Storage, Scanner]:
    s = Storage(":memory:")
    sc = scanner()
    found = sc.scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00")},
        {NAME: q("skinport", "14.01")},
        {NAME: stats("13.00", 10)},
        NOW,
    )
    assert record(s, found, FX, NOW, CFG) == 1
    assert record(s, found, FX, NOW + timedelta(hours=23), CFG) == 0  # повтор за сутки
    return s, sc


def test_paper_record_and_checkpoints() -> None:
    s, _ = _storage_with_signal()
    trade = s.paper_trades()[0]
    assert trade.profit_eur == Decimal("2.77") and trade.usd_per_eur == Decimal("1.10")
    assert trade.rub_per_eur == Decimal("90.00")

    # Через сутки цена упала: медиана 12.00 → 12.00 − 8% = 11.04, прибыль 1.85.
    s.save_quotes([q("skinport", "13.01", at=NOW + timedelta(days=1, minutes=5))])
    s.save_sales([stats("12.00", 10, at=NOW + timedelta(days=1, minutes=5))])
    # Через 7 дней продаж почти нет: уверенно продать нельзя.
    s.save_quotes([q("skinport", "13.01", at=NOW + timedelta(days=7, minutes=5))])
    s.save_sales([stats("12.00", 2, at=NOW + timedelta(days=7, minutes=5))])

    checks = evaluate(trade, s, FeeSchedule.load(), Decimal("0.01"), CFG, NOW + timedelta(days=8))
    assert checks["24ч"].profit_eur == Decimal("1.85")
    assert "3д" not in checks  # данных около этой точки нет
    assert checks["7д"].sellable is False

    summary = {x.label: x for x in summarize([checks])}
    assert summary["24ч"].profitable == 1 and summary["7д"].unsellable == 1
    assert summary["3д"].evaluated == 0


def test_paper_record_respects_floor() -> None:
    s = Storage(":memory:")
    found = scanner().scan(
        "dmarket",
        "skinport",
        {NAME: q("dmarket", "10.00")},
        {NAME: q("skinport", "14.01")},
        {NAME: stats("13.00", 10)},
        NOW,
    )
    assert record(s, found, FX, NOW, ScanConfig(min_roi=Decimal("0.50"))) == 0
    assert s.paper_trades() == []


def test_old_model_trades_hidden(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,"
        " app_id INTEGER NOT NULL, market_hash_name TEXT NOT NULL, buy_market TEXT NOT NULL,"
        " sell_market TEXT NOT NULL, buy_price TEXT NOT NULL, buy_currency TEXT NOT NULL,"
        " sell_price TEXT NOT NULL, sell_currency TEXT NOT NULL, cost_eur TEXT NOT NULL,"
        " proceeds_eur TEXT NOT NULL, profit_eur TEXT NOT NULL, roi TEXT NOT NULL,"
        " usd_per_eur TEXT, complete INTEGER NOT NULL, lock_days INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO paper_trades VALUES (1, ?, 730, ?, 'skinport', 'dmarket', '1', 'EUR',"
        " '2', 'USD', '1', '2', '1', '1', '1.1', 0, 14)",
        (NOW.isoformat(), NAME),
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.paper_trades() == []  # сделка старой модели оценки не входит в отчёт
    assert len(s.paper_trades(model=1)) == 1
    assert s.last_paper(NAME, "skinport", "dmarket") is None  # и не мешает записать новую


def test_paper_trade_in_former_currency() -> None:
    """Сигнал записан, когда Market.CSGO считался в EUR, а потом площадка перешла на рубли."""
    s = Storage(":memory:")
    s.save_paper(
        PaperTrade(
            created_at=NOW,
            app_id=730,
            market_hash_name=NAME,
            buy_market="skinport",
            sell_market="marketcsgo",
            buy_price=Decimal("10.00"),
            buy_currency=Currency.EUR,
            sell_price=Decimal("12.00"),
            sell_currency=Currency.EUR,
            cost_eur=Decimal("10.00"),
            proceeds_eur=Decimal("10.83"),
            profit_eur=Decimal("0.83"),
            roi=Decimal("0.083"),
            usd_per_eur=Decimal("1.10"),
            complete=True,
            lock_days=15,
        )
    )
    trade = s.paper_trades()[0]
    assert trade.rub_per_eur is None

    def at(days: int) -> datetime:
        return NOW + timedelta(days=days, minutes=5)

    def tm_stats(median: str, currency: Currency, days: int) -> SalesStats:
        st = stats(median, 10, at=at(days), source="marketcsgo")
        return st.model_copy(update={"currency": currency})

    # Через сутки данные ещё в EUR: проверка по ним, в той же валюте, что и сигнал.
    s.save_quotes([tm("12.01", Currency.EUR).model_copy(update={"fetched_at": at(1)})])
    s.save_sales([tm_stats("12.00", Currency.EUR, 1)])
    # Через 7 дней цены уже в рублях: сравнивать с сигналом нечем.
    s.save_quotes([tm("1081.00").model_copy(update={"fetched_at": at(7)})])
    s.save_sales([tm_stats("1080.00", Currency.RUB, 7)])

    checks = evaluate(trade, s, FeeSchedule.load(), Decimal("0.01"), CFG, NOW + timedelta(days=8))
    # 12.00 EUR − 5% продавцу = 11.40, вывод на карту 5% = 10.83.
    assert checks["24ч"].sell_price == Money.of("12.00", Currency.EUR)
    assert checks["24ч"].profit_eur == Decimal("0.83")
    assert "7д" not in checks


def test_paper_future_checkpoints_skipped() -> None:
    s, _ = _storage_with_signal()
    trade = s.paper_trades()[0]
    assert evaluate(trade, s, FeeSchedule.load(), Decimal("0.01"), CFG, NOW) == {}


# ---------- CLI ----------


def test_cli_scan_and_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "x.db"
    now = datetime.now(UTC)
    s = Storage(db)
    s.save_quotes([q("skinport", "14.01", at=now), q("dmarket", "10.00", at=now)])
    s.save_quotes([q("skinport", "1.16", at=now, name="S"), q("dmarket", "2.73", at=now, name="S")])
    s.save_sales(
        [stats("13.00", 10, at=now), stats("1.36", 14, at=now, name="S", source="dmarket")]
    )
    s.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(db))
    result = CliRunner().invoke(app, SCAN_ARGS)
    assert result.exit_code == 0, result.output
    assert "dmarket → skinport" in result.output
    assert "прибыль 2.77 EUR" in result.output
    assert "Записано виртуальных сделок: 1" in result.output
    assert "Возможностей: 1" in result.output  # стикер без реального спроса отсеян

    low = CliRunner().invoke(
        app,
        [*SCAN_ARGS, "--min-roi", "0", "--min-profit", "0"],
    )
    assert low.exit_code == 0, low.output
    assert "Записано виртуальных сделок: 0" in low.output  # диагностика не пишет в журнал

    report = CliRunner().invoke(app, ["paper"])
    assert report.exit_code == 0, report.output
    assert "Сигналов за 30 дн.: 1" in report.output
    assert "данных пока нет" in report.output
