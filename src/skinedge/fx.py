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
