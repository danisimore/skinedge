from datetime import UTC, datetime
from decimal import Decimal

import pytest

from skinedge.fx import Direction, FxRates, combine
from skinedge.money import Currency, Money, Rounding


def test_money_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money.of(1.1, "EUR")  # type: ignore[arg-type]


def test_money_from_string_and_minor() -> None:
    assert Money.of("10.05", "EUR") == Money.from_minor(1005, Currency.EUR)
    assert Money.of("10.05", "EUR").to_minor() == 1005


def test_to_minor_requires_rounded() -> None:
    with pytest.raises(ValueError):
        Money.of("1.005", "EUR").to_minor()


def test_currency_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        Money.of("1", "EUR") + Money.of("1", "USD")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Rounding.HALF_UP, "2.70"),
        (Rounding.FLOOR, "2.69"),
        (Rounding.CEILING, "2.70"),
    ],
)
def test_rounding_modes(mode: Rounding, expected: str) -> None:
    assert Money.of("2.695", "USD").rounded(mode).amount == Decimal(expected)


FX = FxRates(rates={Currency.USD: Decimal("1.10")}, spread=Decimal("0.01"), as_of=datetime.now(UTC))


def test_fx_same_currency_is_identity() -> None:
    m = Money.of("12.34", "EUR")
    assert FX.convert(m, Currency.EUR, Direction.PAY) == m


def test_fx_receive_is_conservative() -> None:
    # 105.10 / 1.10 = 95.5454…; × 0.99 = 94.59; округление вниз
    assert FX.convert(Money.of("105.10", "USD"), Currency.EUR, Direction.RECEIVE) == Money.of(
        "94.59", "EUR"
    )


def test_fx_pay_is_conservative() -> None:
    # 103.30 / 1.10 = 93.9090…; × 1.01 = 94.8481…; округление вверх
    assert FX.convert(Money.of("103.30", "USD"), Currency.EUR, Direction.PAY) == Money.of(
        "94.85", "EUR"
    )


def test_fx_pay_costs_more_than_receive() -> None:
    m = Money.of("50", "USD")
    assert FX.convert(m, Currency.EUR, Direction.PAY) > FX.convert(
        m, Currency.EUR, Direction.RECEIVE
    )


def test_fx_missing_rate() -> None:
    with pytest.raises(KeyError):
        FxRates(rates={}).convert(Money.of("1", "USD"), Currency.EUR, Direction.PAY)


def test_combine_official_rates() -> None:
    spread = Decimal("0.01")
    usd = FxRates(
        rates={Currency.USD: Decimal("1.0812")},
        spread=spread,
        as_of=datetime(2026, 10, 27, tzinfo=UTC),
        source="ECB",
    )
    rub = FxRates(
        rates={Currency.RUB: Decimal("90.0977")},
        spread=spread,
        as_of=datetime(2026, 10, 28, tzinfo=UTC),
        source="ЦБ РФ",
    )
    fx = combine(usd, rub)
    assert fx.rates == {Currency.USD: Decimal("1.0812"), Currency.RUB: Decimal("90.0977")}
    assert fx.as_of == usd.as_of and fx.source == "ECB + ЦБ РФ"
    assert fx.describe() == "1 EUR = 1.0812 USD, 90.0977 RUB"
    # 900.98 RUB = 10.00 EUR по курсу ЦБ, минус спред 1% при получении.
    assert fx.convert(Money.of("900.98", "RUB"), Currency.EUR, Direction.RECEIVE) == Money.of(
        "9.90", "EUR"
    )
    with pytest.raises(ValueError, match="дважды"):
        combine(usd, usd)
    with pytest.raises(ValueError, match="спред"):
        combine(usd, rub.model_copy(update={"spread": Decimal("0.02")}))
