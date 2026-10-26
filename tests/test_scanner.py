from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skinedge.cli import app
from skinedge.fees.schedule import FeeSchedule
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.paper import evaluate, record, summarize
from skinedge.quotes import Quote, QuoteKind, SalesStats, SalesWindow
from skinedge.scanner import ScanConfig, Scanner, estimate_sell
from skinedge.storage import Storage

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
NAME = "AK-47 | Redline (Field-Tested)"
FX = FxRates(rates={Currency.USD: Decimal("1.10")}, spread=Decimal("0.01"))
CFG = ScanConfig()


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


def stats(median: str, volume: int, at: datetime = NOW, name: str = NAME) -> SalesStats:
    return SalesStats(
        source="skinport",
        app_id=730,
        market_hash_name=name,
        currency=Currency.EUR,
        window=SalesWindow.D7,
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


def test_estimate_uses_median_when_lower() -> None:
    est = estimate_sell(q("skinport", "14.01"), stats("13.00", 10), CFG)
    assert est is not None and est.price == Money.of("13.00", Currency.EUR)


def test_estimate_undercuts_ask() -> None:
    est = estimate_sell(q("skinport", "14.01"), stats("20", 10), CFG)
    assert est is not None and est.price == Money.of("14.00", Currency.EUR)


def test_estimate_requires_sales_volume() -> None:
    assert estimate_sell(q("skinport", "14.01"), stats("13", 4), CFG) is None


def test_estimate_without_stats_requires_listings() -> None:
    assert estimate_sell(q("dmarket", "14.01", qty=4), None, CFG) is None
    est = estimate_sell(q("dmarket", "14.01", qty=5), None, CFG)
    assert est is not None and est.price == Money.of("14.00", Currency.USD)


def test_estimate_no_listings() -> None:
    assert estimate_sell(q("dmarket", None, qty=0), None, CFG) is None


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


def test_skinport_to_dmarket_incomplete_withdraw() -> None:
    # 14.00 USD − 10% = 12.60 USD → 12.60/1.10·0.99 = 11.34 EUR (вниз). Вывод DMarket неизвестен.
    found = scanner().scan(
        "skinport",
        "dmarket",
        {NAME: q("skinport", "10.00")},
        {NAME: q("dmarket", "14.01")},
        {},
        NOW,
    )
    r = found[0].result
    assert r.net_proceeds == Money.of("11.34", Currency.EUR)
    assert r.profit == Money.of("1.34", Currency.EUR)
    assert not r.complete
    assert any("неполный" in f for f in found[0].flags)


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
    assert record(s, found, FX, NOW) == 1
    assert record(s, found, FX, NOW + timedelta(hours=23)) == 0  # повтор за сутки
    return s, sc


def test_paper_record_and_checkpoints() -> None:
    s, _ = _storage_with_signal()
    trade = s.paper_trades()[0]
    assert trade.profit_eur == Decimal("2.77") and trade.usd_per_eur == Decimal("1.10")

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
    s.save_sales([stats("13.00", 10, at=now)])
    s.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(db))
    result = CliRunner().invoke(app, ["scan", "--no-fetch", "--usd-per-eur", "1.10"])
    assert result.exit_code == 0, result.output
    assert "dmarket → skinport" in result.output
    assert "прибыль 2.77 EUR" in result.output
    assert "Записано виртуальных сделок: 1" in result.output

    report = CliRunner().invoke(app, ["paper"])
    assert report.exit_code == 0, report.output
    assert "Сигналов за 30 дн.: 1" in report.output
    assert "данных пока нет" in report.output
