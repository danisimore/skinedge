"""Денежные значения на Decimal.

Все денежные операции в SkinEdge идут через :class:`Money`. Float запрещён:
конструктор принимает только ``Decimal``, ``int`` или строку.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

CENT = Decimal("0.01")


class Currency(StrEnum):
    """Поддерживаемые валюты."""

    EUR = "EUR"
    USD = "USD"


class Rounding(StrEnum):
    """Режим округления до минимальной денежной единицы."""

    HALF_UP = "half_up"
    FLOOR = "floor"
    CEILING = "ceiling"

    @property
    def decimal_mode(self) -> str:
        """Соответствующая константа модуля ``decimal``."""
        return {
            Rounding.HALF_UP: ROUND_HALF_UP,
            Rounding.FLOOR: ROUND_FLOOR,
            Rounding.CEILING: ROUND_CEILING,
        }[self]


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Преобразует значение в Decimal, отвергая float.

    Args:
        value: Число в виде Decimal, int или строки.

    Returns:
        Значение как Decimal.

    Raises:
        TypeError: Если передан float или другой неподдерживаемый тип.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"Денежные значения не могут быть {type(value).__name__}")
    return Decimal(value)


@dataclass(frozen=True, slots=True)
class Money:
    """Сумма в конкретной валюте.

    Attributes:
        amount: Сумма (Decimal, без потери точности).
        currency: Валюта.
    """

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount))
        object.__setattr__(self, "currency", Currency(self.currency))

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: Currency | str) -> Money:
        """Создаёт сумму из Decimal, int или строки."""
        return cls(to_decimal(amount), Currency(currency))

    @classmethod
    def from_minor(cls, minor: int, currency: Currency | str) -> Money:
        """Создаёт сумму из минимальных единиц (центов)."""
        return cls(Decimal(minor) * CENT, Currency(currency))

    @classmethod
    def zero(cls, currency: Currency | str) -> Money:
        """Нулевая сумма в заданной валюте."""
        return cls(Decimal(0), Currency(currency))

    def to_minor(self) -> int:
        """Сумма в минимальных единицах. Требует, чтобы сумма уже была округлена."""
        minor = self.amount / CENT
        if minor != minor.to_integral_value():
            raise ValueError(f"{self} не округлена до центов")
        return int(minor)

    def rounded(self, mode: Rounding = Rounding.HALF_UP) -> Money:
        """Округляет до центов заданным способом."""
        return Money(self.amount.quantize(CENT, rounding=mode.decimal_mode), self.currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"Разные валюты: {self.currency} и {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money(self.amount * to_decimal(factor), self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency.value}"
