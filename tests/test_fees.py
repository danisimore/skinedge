from decimal import Decimal

import pytest

from skinedge.fees.models import FeeKind, FeeRule, Operation, Verification
from skinedge.fees.schedule import FeeSchedule
from skinedge.finance.steam import buyer_price_minor, seller_receives
from skinedge.money import Currency, Money


def rule(**kw: object) -> FeeRule:
    base: dict[str, object] = {
        "id": "t",
        "marketplace": "m",
        "operation": Operation.SELL,
        "kind": FeeKind.PERCENT,
        "currency": Currency.EUR,
        "verification": Verification.OFFICIAL,
    }
    base.update(kw)
    return FeeRule.model_validate(base)


def test_percent_fee_rounds_half_up() -> None:
    r = rule(rate="0.025")
    assert r.compute(Money.of("107.80", "EUR")) == Money.of("2.70", "EUR")


def test_minimum_fee_applies() -> None:
    r = rule(rate="0.05", min_fee="0.25")
    assert r.compute(Money.of("1.00", "EUR")) == Money.of("0.25", "EUR")


def test_maximum_fee_applies() -> None:
    r = rule(rate="0.05", max_fee="10")
    assert r.compute(Money.of("1000", "EUR")) == Money.of("10.00", "EUR")


def test_fixed_fee() -> None:
    r = rule(kind=FeeKind.FIXED, fixed="0.35")
    assert r.compute(Money.of("500", "EUR")) == Money.of("0.35", "EUR")


def test_rate_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        rule(rate="1.2")


def test_rule_currency_mismatch() -> None:
    with pytest.raises(ValueError):
        rule(rate="0.1").compute(Money.of("1", "USD"))


def test_builtin_schedule_loads() -> None:
    schedule = FeeSchedule.load()
    assert {"skinport", "csfloat", "steam"} <= schedule.marketplaces.keys()


@pytest.mark.parametrize(
    ("price", "fee"),
    [("999.99", "80.00"), ("1000.00", "60.00"), ("0.01", "0.00"), ("12.50", "1.00")],
)
def test_skinport_tiered_sell_fee(price: str, fee: str) -> None:
    schedule = FeeSchedule.load()
    base = Money.of(price, "EUR")
    rules = schedule.select("skinport", Operation.SELL, base)
    assert len(rules) == 1
    assert rules[0].compute(base) == Money.of(fee, "EUR")


def test_schedule_rejects_duplicates() -> None:
    data = {
        "marketplaces": {"m": {"currency": "EUR", "payout": "cash"}},
        "rules": [
            {
                "id": "a",
                "marketplace": "m",
                "operation": "sell",
                "kind": "percent",
                "rate": "0.1",
                "currency": "EUR",
                "verification": "official",
            },
        ]
        * 2,
    }
    with pytest.raises(ValueError, match="Повторяющиеся"):
        FeeSchedule.from_dict(data)


def test_schedule_rejects_currency_mismatch() -> None:
    data = {
        "marketplaces": {"m": {"currency": "EUR", "payout": "cash"}},
        "rules": [
            {
                "id": "a",
                "marketplace": "m",
                "operation": "sell",
                "kind": "percent",
                "rate": "0.1",
                "currency": "USD",
                "verification": "official",
            },
        ],
    }
    with pytest.raises(ValueError, match="валюта"):
        FeeSchedule.from_dict(data)


# ---------- Steam ----------

STEAM, GAME = Decimal("0.05"), Decimal("0.10")


def test_steam_buyer_price_for_100() -> None:
    assert buyer_price_minor(10000, STEAM, GAME) == 11500


def test_steam_seller_receives_matches_15_percent() -> None:
    assert seller_receives(Money.of("115.00", "EUR"), STEAM, GAME) == Money.of("100.00", "EUR")


@pytest.mark.parametrize(
    ("buyer", "seller"),
    [("0.03", "0.01"), ("0.02", "0.00"), ("0.20", "0.18"), ("130.00", "113.05")],
)
def test_steam_small_and_odd_prices(buyer: str, seller: str) -> None:
    assert seller_receives(Money.of(buyer, "EUR"), STEAM, GAME) == Money.of(seller, "EUR")


@pytest.mark.parametrize("received", [1, 2, 7, 19, 20, 21, 99, 100, 101, 12345, 999999])
def test_steam_roundtrip(received: int) -> None:
    price = Money.from_minor(buyer_price_minor(received, STEAM, GAME), "EUR")
    assert seller_receives(price, STEAM, GAME) == Money.from_minor(received, "EUR")


def test_schedule_in_former_currency() -> None:
    schedule = FeeSchedule.load()
    eur = schedule.in_currency("marketcsgo", Currency.EUR)
    assert eur.market("marketcsgo").currency is Currency.EUR
    assert schedule.market("marketcsgo").currency is Currency.RUB  # исходное не меняется
    sell = eur.select("marketcsgo", Operation.SELL, Money.of("10", "EUR"))
    assert [r.rate for r in sell] == [Decimal("0.05")]
    assert schedule.in_currency("marketcsgo", Currency.RUB) is schedule
    # У Skinport ставка зависит от цены (до 1000 EUR): в другой валюте это другие деньги.
    with pytest.raises(ValueError, match="суммами"):
        schedule.in_currency("skinport", Currency.USD)
