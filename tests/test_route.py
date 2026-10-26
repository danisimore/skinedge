from decimal import Decimal

import pytest
from typer.testing import CliRunner

from skinedge.cli import app
from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.finance.route import RouteCalculator
from skinedge.fx import FxRates
from skinedge.money import Currency, Money

SCHEDULE = FeeSchedule.load()
FX = FxRates(rates={Currency.USD: Decimal("1.10")}, spread=Decimal("0.01"))


def eur(x: str) -> Money:
    return Money.of(x, "EUR")


def usd(x: str) -> Money:
    return Money.of(x, "USD")


def test_skinport_to_csfloat_negative_profit() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    r = calc.evaluate(eur("100.00"), usd("110.00"))
    # Продажа: 2% = 2.20 → 107.80; вывод 2.5% = 2.70 → 105.10 USD → 94.59 EUR
    assert r.total_cost == eur("100.00")
    assert r.net_proceeds == eur("94.59")
    assert r.profit == eur("-5.41")
    assert r.roi == Decimal("-0.0541")
    assert r.payout is Payout.CASH
    assert r.lock_days == 14
    assert r.safety_margin is not None and r.safety_margin < 0


def test_csfloat_to_skinport_with_sepa_deposit() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "csfloat", "skinport", deposit_method="sepa")
    r = calc.evaluate(usd("100.00"), eur("120.00"))
    # Пополнение 3.3% = 3.30 → 103.30 USD → 94.85 EUR; продажа 8% = 9.60 → 110.40
    assert r.total_cost == eur("94.85")
    assert r.net_proceeds == eur("110.40")
    assert r.profit == eur("15.55")
    assert r.roi == Decimal("0.1639")
    assert r.complete


def test_steam_route_is_wallet_not_cash() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "steam")
    r = calc.evaluate(eur("100.00"), eur("130.00"))
    assert r.net_proceeds == eur("113.05")
    assert r.profit == eur("13.05")
    assert r.payout is Payout.STEAM_WALLET
    assert any("Steam Wallet" in w for w in r.warnings)


def test_unknown_deposit_fee_marks_incomplete() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat", deposit_method="card")
    r = calc.evaluate(eur("100.00"), usd("130.00"))
    assert not r.complete
    assert any("Неизвестная комиссия" in w for w in r.warnings)


def test_missing_withdraw_rule_marks_incomplete() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat", withdraw_method="paypal")
    r = calc.evaluate(eur("100.00"), usd("130.00"))
    assert not r.complete
    assert any(w.startswith("Нет правила") for w in r.warnings)


def test_third_party_fees_are_not_confirmed() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    r = calc.evaluate(eur("100.00"), usd("130.00"))
    assert not r.confirmed


@pytest.mark.parametrize(
    ("buy_market", "sell_market", "buy", "deposit"),
    [
        ("skinport", "csfloat", eur("100.00"), "balance"),
        ("csfloat", "skinport", usd("100.00"), "sepa"),
        ("skinport", "steam", eur("3.00"), "balance"),
        ("skinport", "csfloat", eur("0.05"), "balance"),
        ("csfloat", "skinport", usd("1500.00"), "card"),
    ],
)
def test_breakeven_is_exact(buy_market: str, sell_market: str, buy: Money, deposit: str) -> None:
    calc = RouteCalculator(SCHEDULE, FX, buy_market, sell_market, deposit_method=deposit)
    be = calc.breakeven_sell(buy)
    assert be is not None
    one_cent = Money.from_minor(1, be.currency)
    assert calc.evaluate(buy, be).profit.amount >= 0
    assert calc.evaluate(buy, be - one_cent).profit.amount < 0


def test_breakeven_crosses_skinport_tier() -> None:
    # Около 1000 EUR комиссия падает с 8% до 6%: безубыточность должна это учитывать.
    calc = RouteCalculator(SCHEDULE, FX, "csfloat", "skinport")
    be = calc.breakeven_sell(usd("1000.00"))
    assert be is not None
    assert calc.evaluate(usd("1000.00"), be).profit.amount >= 0


def test_max_buy_price_for_target_profit() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    target = eur("5.00")
    sell = usd("150.00")
    mx = calc.max_buy_price(sell, target)
    assert mx is not None
    assert calc.evaluate(mx, sell).profit >= target
    assert calc.evaluate(mx + eur("0.01"), sell).profit < target


def test_max_buy_price_unreachable() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    assert calc.max_buy_price(usd("1.00"), eur("100.00")) is None


def test_required_sell_for_roi() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    need = calc.required_sell_for_roi(eur("100.00"), Decimal("0.10"))
    assert need is not None
    assert calc.evaluate(eur("100.00"), need).roi >= Decimal("0.10")
    assert calc.evaluate(eur("100.00"), need - usd("0.01")).roi < Decimal("0.10")


def test_price_in_wrong_currency_rejected() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    with pytest.raises(ValueError):
        calc.evaluate(usd("100"), usd("110"))


def test_zero_prices() -> None:
    calc = RouteCalculator(SCHEDULE, FX, "skinport", "csfloat")
    r = calc.evaluate(eur("0.00"), usd("0.00"))
    assert r.profit == eur("0.00")
    assert r.roi == 0


def test_cli_fees_smoke() -> None:
    result = CliRunner().invoke(
        app,
        [
            "fees",
            "--buy-market",
            "skinport",
            "--buy",
            "100",
            "--sell-market",
            "csfloat",
            "--sell",
            "110",
            "--usd-per-eur",
            "1.10",
            "--target-profit",
            "5",
            "--target-roi",
            "0.1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Чистая прибыль:           -5.41 EUR" in result.output
    assert "НЕ" not in result.output.split("Расчёт:")[1][:3]


def test_cli_requires_fx_for_usd() -> None:
    result = CliRunner().invoke(
        app,
        [
            "fees",
            "--buy-market",
            "skinport",
            "--buy",
            "100",
            "--sell-market",
            "csfloat",
            "--sell",
            "110",
        ],
    )
    assert result.exit_code != 0
