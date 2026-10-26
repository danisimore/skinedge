"""Загрузка и выбор правил комиссий."""

from __future__ import annotations

from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from skinedge.fees.models import FeeRule, Operation
from skinedge.money import Currency, Money


class Payout(StrEnum):
    """Во что превращается выручка с площадки."""

    CASH = "cash"  # деньги, которые можно вывести на банк
    STEAM_WALLET = "steam_wallet"  # баланс Steam, вывести нельзя


class Marketplace(BaseModel):
    """Параметры площадки, важные для расчёта.

    Attributes:
        name: Идентификатор площадки.
        currency: Валюта цен и баланса.
        payout: Тип выручки.
        transfer_lock_days: Сколько дней купленный предмет нельзя передать.
        settlement_days: Через сколько дней после продажи деньги доступны.
        item_url: Шаблон ссылки на предмет, ``{name}`` заменяется названием.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    currency: Currency
    payout: Payout
    transfer_lock_days: int = 0
    settlement_days: int = 0
    item_url: str | None = None


class FeeSchedule:
    """Набор площадок и правил комиссий."""

    def with_rules(
        self, rules: list[FeeRule], replace_marketplace: str | None = None
    ) -> FeeSchedule:
        """Новое расписание с дополнительными правилами.

        Args:
            rules: Добавляемые правила (например, комиссии, полученные по API).
            replace_marketplace: Если указано, сначала убрать правила этой площадки
                для операций, которые есть среди новых правил.

        Returns:
            Новое расписание; исходное не меняется.
        """
        kept = self.rules
        if replace_marketplace is not None:
            ops = {r.operation for r in rules if r.marketplace == replace_marketplace}
            kept = [
                r for r in kept if not (r.marketplace == replace_marketplace and r.operation in ops)
            ]
        return FeeSchedule(self.marketplaces, [*kept, *rules])

    def __init__(self, marketplaces: dict[str, Marketplace], rules: list[FeeRule]) -> None:
        ids = [r.id for r in rules]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Повторяющиеся id правил: {sorted(duplicates)}")
        for rule in rules:
            market = marketplaces.get(rule.marketplace)
            if market is None:
                raise ValueError(f"{rule.id}: неизвестная площадка {rule.marketplace}")
            if rule.currency != market.currency:
                raise ValueError(f"{rule.id}: валюта правила не совпадает с валютой площадки")
        self.marketplaces = marketplaces
        self.rules = rules

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeeSchedule:
        """Создаёт расписание из словаря в формате rules.yaml."""
        markets = {
            name: Marketplace(name=name, **params)
            for name, params in (data.get("marketplaces") or {}).items()
        }
        rules = [FeeRule.model_validate(r) for r in data.get("rules") or []]
        return cls(markets, rules)

    @classmethod
    def load(cls, path: Path | None = None) -> FeeSchedule:
        """Загружает правила из YAML. Без пути берёт встроенный rules.yaml."""
        if path is None:
            text = resources.files("skinedge.fees").joinpath("rules.yaml").read_text("utf-8")
        else:
            text = path.read_text("utf-8")
        return cls.from_dict(yaml.safe_load(text))

    def market(self, name: str) -> Marketplace:
        """Возвращает площадку по имени."""
        try:
            return self.marketplaces[name]
        except KeyError as exc:
            raise KeyError(f"Неизвестная площадка: {name}") from exc

    def methods(self, marketplace: str, operation: Operation) -> list[str]:
        """Способы оплаты или вывода, для которых есть правила."""
        return sorted(
            {
                r.method
                for r in self.rules
                if r.marketplace == marketplace and r.operation is operation and r.method
            }
        )

    def select(
        self,
        marketplace: str,
        operation: Operation,
        base: Money,
        method: str | None = None,
        item: str | None = None,
    ) -> list[FeeRule]:
        """Правила, применимые к операции.

        Правило без ``method`` применяется к любому способу. Правило с ``method``
        применяется только к нему. Если есть правила для конкретного предмета
        (``item``), они заменяют общие правила этой операции.

        Args:
            marketplace: Площадка.
            operation: Операция.
            base: База комиссии (нужна для ценовых диапазонов).
            method: Способ оплаты или вывода.
            item: Название предмета.

        Returns:
            Список подходящих правил; пустой, если правил нет.
        """
        matching = [
            r
            for r in self.rules
            if r.marketplace == marketplace
            and r.operation is operation
            and (r.method is None or r.method == method)
            and (r.item is None or r.item == item)
            and r.applies_to(base)
        ]
        specific = [r for r in matching if r.item is not None]
        return specific or matching
