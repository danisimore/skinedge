"""Котировки и статистика продаж, общие для всех источников."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from skinedge.money import Currency


class QuoteKind(StrEnum):
    """Насколько конкретна котировка."""

    AGGREGATE = "aggregate"  # сводка по названию: минимальная цена и количество лотов
    LISTING = "listing"  # конкретный лот с идентификатором


class SalesWindow(StrEnum):
    """Окно статистики продаж."""

    H24 = "24h"
    D7 = "7d"
    D30 = "30d"
    D90 = "90d"


class Quote(BaseModel):
    """Цена предложения (ask) на площадке.

    Attributes:
        source: Площадка (``skinport``, ``csfloat``...).
        app_id: Steam app_id игры (730 для CS2).
        market_hash_name: Название предмета в формате Steam.
        currency: Валюта цен.
        kind: Сводка по названию или конкретный лот.
        min_price: Минимальная цена предложения; ``None``, если лотов нет.
        median_price: Медиана цен предложений.
        quantity: Количество лотов.
        best_bid: Лучшая заявка на покупку (bid), если площадка её отдаёт.
        bid_quantity: Количество заявок на покупку.
        listing_id: Идентификатор лота для ``QuoteKind.LISTING``.
        url: Ссылка на предмет или лот.
        source_updated_at: Когда данные обновила площадка.
        fetched_at: Когда мы получили данные.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    app_id: int
    market_hash_name: str
    currency: Currency
    kind: QuoteKind
    min_price: Decimal | None
    median_price: Decimal | None = None
    quantity: int
    best_bid: Decimal | None = None
    bid_quantity: int | None = None
    listing_id: str | None = None
    url: str | None = None
    source_updated_at: datetime | None = None
    fetched_at: datetime

    def age(self, now: datetime) -> timedelta:
        """Возраст котировки: от обновления на площадке, если оно известно."""
        reference = self.source_updated_at or self.fetched_at
        return now - reference

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        """Устарела ли котировка."""
        return self.age(now) > max_age


class Sale(BaseModel):
    """Одна совершённая продажа.

    Attributes:
        source: Площадка.
        app_id: Steam app_id игры.
        market_hash_name: Название предмета.
        currency: Валюта.
        price: Цена сделки.
        sold_at: Время сделки.
        tx_type: Как совершена сделка (``Offer`` — покупка лота, ``Target`` — по заявке).
        float_value: Float предмета, если известен.
        phase: Фаза (Doppler и т.п.), если известна.
        fetched_at: Когда мы получили данные.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    app_id: int
    market_hash_name: str
    currency: Currency
    price: Decimal
    sold_at: datetime
    tx_type: str | None = None
    float_value: Decimal | None = None
    phase: str | None = None
    fetched_at: datetime


class SalesStats(BaseModel):
    """Статистика реальных продаж за окно времени.

    Attributes:
        source: Площадка.
        app_id: Steam app_id игры.
        market_hash_name: Название предмета.
        version: Вариант предмета (например, фаза Doppler), если площадка его различает.
        currency: Валюта.
        window: Окно статистики.
        min: Минимальная цена продажи.
        max: Максимальная цена продажи.
        avg: Средняя цена продажи.
        median: Медианная цена продажи.
        volume: Количество продаж.
        fetched_at: Когда мы получили данные.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    app_id: int
    market_hash_name: str
    version: str | None = None
    currency: Currency
    window: SalesWindow
    min: Decimal | None
    max: Decimal | None
    avg: Decimal | None
    median: Decimal | None
    volume: int
    fetched_at: datetime
