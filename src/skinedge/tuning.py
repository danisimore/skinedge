"""Настройки сканера, сохранённые из интерфейса (``skinedge ui``).

Хранятся в базе (таблица ``meta``), поэтому их видят и интерфейс, и фоновый
запуск. ``skinedge run`` перечитывает их в начале каждого прохода: перезапуск не
нужен. Пока настройки не сохранены, действуют флаги запуска.

Виртуальные сделки по-прежнему записываются по стандартным порогам
(``ScanConfig()``), чтобы их итоги можно было сравнивать между собой.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from skinedge.pipeline import ScanOptions
from skinedge.storage import Storage

log = logging.getLogger(__name__)

KEY = "ui.scan_settings"


class ScanSettings(BaseModel):
    """Что искать и какие находки считать сигналом.

    Attributes:
        min_price: Нижняя граница цены предмета на Skinport, EUR.
        max_price: Верхняя граница цены предмета на Skinport, EUR.
        limit: Сколько самых ликвидных предметов сравнивать в каждой игре.
        min_sales_7d: Продаваемость: минимум продаж за 7 дней на площадке продажи.
        min_profit: Минимальная чистая прибыль сигнала, EUR.
        min_roi: Минимальный ROI сигнала (0.05 = 5%).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_price: Decimal = Field(gt=0, le=100_000)
    max_price: Decimal = Field(gt=0, le=100_000)
    limit: int = Field(ge=10, le=2000)
    min_sales_7d: int = Field(ge=1, le=10_000)
    min_profit: Decimal = Field(ge=0, le=100_000)
    min_roi: Decimal = Field(ge=0, le=10)

    @model_validator(mode="after")
    def _range(self) -> ScanSettings:
        if self.max_price <= self.min_price:
            raise ValueError("максимальная цена должна быть больше минимальной")
        return self

    @classmethod
    def of(cls, opts: ScanOptions) -> ScanSettings:
        """Настройки, которые сейчас заданы параметрами прохода."""
        return cls(
            min_price=opts.min_price,
            max_price=opts.max_price,
            limit=opts.limit,
            min_sales_7d=opts.config.min_sales_7d,
            min_profit=opts.config.min_profit,
            min_roi=opts.config.min_roi,
        )

    def apply(self, opts: ScanOptions) -> ScanOptions:
        """Параметры прохода с этими настройками."""
        return replace(
            opts,
            limit=self.limit,
            min_price=self.min_price,
            max_price=self.max_price,
            config=replace(
                opts.config,
                min_sales_7d=self.min_sales_7d,
                min_profit=self.min_profit,
                min_roi=self.min_roi,
            ),
        )

    def describe(self) -> str:
        """Одна строка для лога."""
        return (
            f"цены {self.min_price}–{self.max_price} EUR, {self.limit} предметов на игру, "
            f"продаж за 7 дней от {self.min_sales_7d}, прибыль от {self.min_profit} EUR, "
            f"ROI от {self.min_roi * 100:.1f}%"
        )


def load(storage: Storage) -> ScanSettings | None:
    """Сохранённые настройки или ``None``, если их нет или запись повреждена."""
    raw = storage.get_meta(KEY)
    if raw is None:
        return None
    try:
        return ScanSettings.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("Настройки из интерфейса повреждены, действуют флаги запуска: %s", exc)
        return None


def save(storage: Storage, settings: ScanSettings) -> None:
    """Сохраняет настройки; фоновый запуск применит их со следующего прохода."""
    storage.set_meta(KEY, settings.model_dump_json())


def reset(storage: Storage) -> None:
    """Удаляет сохранённые настройки: снова действуют флаги запуска."""
    storage.delete_meta(KEY)
