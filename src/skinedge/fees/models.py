"""Модели правил комиссий."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skinedge.money import Currency, Money, Rounding


class Operation(StrEnum):
    """Тип операции, к которой применяется комиссия."""

    BUY = "buy"  # комиссия покупателя при покупке предмета
    SELL = "sell"  # комиссия продавца при продаже предмета
    DEPOSIT = "deposit"  # пополнение баланса площадки
    WITHDRAW = "withdraw"  # вывод денег с площадки


class FeeKind(StrEnum):
    """Способ расчёта комиссии."""

    PERCENT = "percent"  # rate × база, с опциональным минимумом и максимумом
    FIXED = "fixed"  # фиксированная сумма
    STEAM = "steam"  # схема Steam: комиссии считаются от суммы продавцу (см. finance.steam)


class Verification(StrEnum):
    """Насколько надёжно подтверждена комиссия."""

    OFFICIAL = "official"  # официальная страница площадки
    ACCOUNT = "account"  # проверено в аккаунте пользователя
    THIRD_PARTY = "third_party"  # сторонний обзор, нужно подтвердить
    UNKNOWN = "unknown"  # размер неизвестен; расчёт с таким правилом неполный


class FeeRule(BaseModel):
    """Одно правило комиссии для площадки и операции.

    Attributes:
        id: Уникальный идентификатор правила.
        marketplace: Площадка (``skinport``, ``csfloat``, ``steam``...).
        operation: Операция, к которой относится правило.
        method: Способ оплаты или вывода (``sepa``, ``card``...). ``None`` значит любой.
        kind: Способ расчёта.
        rate: Доля для процентных комиссий (0.08 = 8%).
        fixed: Фиксированная сумма для ``FeeKind.FIXED``.
        min_fee: Минимальный размер комиссии.
        max_fee: Максимальный размер комиссии.
        price_from: Правило действует, если база не меньше этой суммы.
        price_to: Правило действует, если база строго меньше этой суммы.
        currency: Валюта, в которой заданы суммы правила.
        rounding: Режим округления комиссии до центов.
        verification: Источник уверенности в размере комиссии.
        source: Ссылка на источник.
        verified_at: Дата последней проверки.
        note: Пояснение для человека.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    marketplace: str
    operation: Operation
    method: str | None = None
    kind: FeeKind
    rate: Decimal = Decimal(0)
    fixed: Decimal = Decimal(0)
    min_fee: Decimal | None = None
    max_fee: Decimal | None = None
    price_from: Decimal | None = None
    price_to: Decimal | None = None
    currency: Currency
    rounding: Rounding = Rounding.HALF_UP
    verification: Verification
    source: str | None = None
    verified_at: date | None = None
    note: str = ""
    extra: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> FeeRule:
        if not Decimal(0) <= self.rate < Decimal(1):
            raise ValueError(f"{self.id}: rate должен быть в [0, 1)")
        if self.fixed < 0:
            raise ValueError(f"{self.id}: fixed не может быть отрицательным")
        if self.kind is FeeKind.STEAM and not {"steam_rate", "game_rate"} <= self.extra.keys():
            raise ValueError(f"{self.id}: для схемы steam нужны extra.steam_rate и extra.game_rate")
        return self

    def applies_to(self, base: Money) -> bool:
        """Проверяет, попадает ли база в ценовой диапазон правила."""
        if base.currency != self.currency:
            raise ValueError(f"{self.id}: правило в {self.currency}, база в {base.currency}")
        if self.price_from is not None and base.amount < self.price_from:
            return False
        return not (self.price_to is not None and base.amount >= self.price_to)

    def compute(self, base: Money) -> Money:
        """Считает комиссию с базы.

        Args:
            base: Сумма, с которой берётся комиссия, в валюте правила.

        Returns:
            Комиссия, округлённая до центов.

        Raises:
            ValueError: Если валюта не совпадает или схема требует отдельного расчёта.
        """
        if base.currency != self.currency:
            raise ValueError(f"{self.id}: правило в {self.currency}, база в {base.currency}")
        if self.kind is FeeKind.STEAM:
            raise ValueError(f"{self.id}: комиссию Steam считает finance.steam")
        if self.kind is FeeKind.FIXED:
            fee = Money(self.fixed, self.currency)
        else:
            fee = (base * self.rate).rounded(self.rounding)
        if self.min_fee is not None:
            fee = max(fee, Money(self.min_fee, self.currency))
        if self.max_fee is not None:
            fee = min(fee, Money(self.max_fee, self.currency))
        return fee.rounded(self.rounding)

    @property
    def is_confirmed(self) -> bool:
        """Подтверждена ли комиссия официально или в аккаунте."""
        return self.verification in (Verification.OFFICIAL, Verification.ACCOUNT)

    def describe(self) -> str:
        """Короткое описание правила для отчёта."""
        if self.verification is Verification.UNKNOWN:
            return "размер неизвестен"
        if self.kind is FeeKind.FIXED:
            text = f"{self.fixed} {self.currency}"
        elif self.kind is FeeKind.STEAM:
            steam, game = self.extra["steam_rate"], self.extra["game_rate"]
            text = f"Steam {_pct(steam)}% + игра {_pct(game)}% от суммы продавцу"
        else:
            text = f"{_pct(self.rate)}%"
            if self.min_fee is not None:
                text += f", мин. {self.min_fee} {self.currency}"
        if self.price_from is not None:
            text += f" от {self.price_from} {self.currency}"
        if self.price_to is not None:
            text += f" до {self.price_to} {self.currency}"
        return text


def _pct(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}"
