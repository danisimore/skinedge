"""Конвертация валют.

Курсы задаются относительно EUR (сколько единиц валюты за 1 EUR, как у ЕЦБ).
Реальный курс площадки или банка неизвестен, поэтому поверх курса
применяется явный спред: при оплате мы теряем ``spread``, при получении тоже.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from skinedge.money import Currency, Money, Rounding


class Direction(StrEnum):
    """Направление конвертации: мы платим или получаем."""

    PAY = "pay"  # округляем и сдвигаем курс против нас: платим больше
    RECEIVE = "receive"  # получаем меньше


class FxRates(BaseModel):
    """Набор курсов с базой EUR.

    Attributes:
        rates: Курс валюты за 1 EUR, например ``{"USD": Decimal("1.08")}``.
        spread: Доля потерь при каждой конвертации (0.01 = 1%).
        as_of: Время получения курсов.
        source: Откуда взяты курсы.
    """

    model_config = ConfigDict(frozen=True)

    rates: dict[Currency, Decimal]
    spread: Decimal = Decimal("0.01")
    as_of: datetime | None = None
    source: str = "manual"

    def _rate(self, currency: Currency) -> Decimal:
        if currency is Currency.EUR:
            return Decimal(1)
        try:
            rate = self.rates[currency]
        except KeyError as exc:
            raise KeyError(f"Нет курса для {currency}") from exc
        if rate <= 0:
            raise ValueError(f"Неверный курс {currency}: {rate}")
        return rate

    def describe(self) -> str:
        """Курсы одной строкой: ``1 EUR = 1.1150 USD, 90.0977 RUB``."""
        if not self.rates:
            return "курсов нет"
        order = list(Currency)
        rates = sorted(self.rates.items(), key=lambda kv: order.index(kv[0]))
        return "1 EUR = " + ", ".join(f"{r} {c.value}" for c, r in rates)

    def convert(self, money: Money, to: Currency, direction: Direction) -> Money:
        """Конвертирует сумму с учётом спреда и консервативного округления.

        Args:
            money: Исходная сумма.
            to: Целевая валюта.
            direction: ``PAY``, если сумму нужно заплатить, ``RECEIVE``, если получить.

        Returns:
            Сумма в целевой валюте, округлённая до центов не в нашу пользу.
        """
        if money.currency == to:
            return money
        mid = money.amount / self._rate(money.currency) * self._rate(to)
        if direction is Direction.PAY:
            return Money(mid * (1 + self.spread), to).rounded(Rounding.CEILING)
        return Money(mid * (1 - self.spread), to).rounded(Rounding.FLOOR)


def combine(*parts: FxRates) -> FxRates:
    """Объединяет курсы из нескольких источников (USD от ЕЦБ, RUB от ЦБ РФ).

    Время курса — самое раннее из частей: так видно, насколько устарел худший.

    Raises:
        ValueError: Если у частей разный спред или один курс задан дважды.
    """
    if not parts:
        raise ValueError("Нет курсов")
    rates: dict[Currency, Decimal] = {}
    for part in parts:
        if part.spread != parts[0].spread:
            raise ValueError("У курсов разный спред")
        twice = rates.keys() & part.rates.keys()
        if twice:
            raise ValueError(f"Курс {', '.join(sorted(twice))} задан дважды")
        rates.update(part.rates)
    dates = [p.as_of for p in parts if p.as_of is not None]
    return FxRates(
        rates=rates,
        spread=parts[0].spread,
        as_of=min(dates) if dates else None,
        source=" + ".join(p.source for p in parts),
    )
