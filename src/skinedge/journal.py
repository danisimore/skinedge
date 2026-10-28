"""Журнал реальных сделок: что куплено, где собираешься продать, что вышло.

Записи вводит пользователь в интерфейсе (``skinedge app``); SkinEdge сам ничего
не покупает и не продаёт. Прибыль считается теми же правилами комиссий, что и в
сканере (:class:`skinedge.finance.route.RouteCalculator`): комиссия покупателя,
комиссия продавца и вывод денег, итог в EUR.

Курс USD берётся на момент записи: при покупке для стоимости, при продаже для
выручки. Пока сделка открыта, ожидаемая выручка считается по текущему курсу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.finance.route import Leg, RouteCalculator
from skinedge.fx import FxRates
from skinedge.games import TITLES
from skinedge.money import Currency, Money

PRICE = Field(gt=0, le=1_000_000)


class Trade(BaseModel):
    """Одна сделка из журнала.

    Attributes:
        id: Номер записи в базе.
        app_id: Игра.
        market_hash_name: Название предмета.
        bought_at: Дата покупки.
        buy_market: Где куплен.
        buy_price: Цена покупки в валюте площадки.
        buy_currency: Валюта площадки покупки.
        buy_usd_per_eur: Курс на момент записи покупки.
        target_market: Где собираешься продать.
        target_price: Сколько предмет стоит там сейчас, в валюте площадки.
        target_currency: Валюта площадки продажи.
        target_at: Когда записана текущая цена.
        sold_at: Дата продажи.
        sell_market: Где продан.
        sell_price: Цена продажи в валюте площадки.
        sell_currency: Валюта площадки продажи.
        sell_usd_per_eur: Курс на момент записи продажи.
        note: Заметка.
        created_at: Когда сделка внесена в журнал.
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    app_id: int
    market_hash_name: str = Field(min_length=1, max_length=300)
    bought_at: date
    buy_market: str
    buy_price: Decimal = PRICE
    buy_currency: Currency
    buy_usd_per_eur: Decimal | None = Field(default=None, gt=0)
    target_market: str
    target_price: Decimal = PRICE
    target_currency: Currency
    target_at: datetime
    sold_at: date | None = None
    sell_market: str | None = None
    sell_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    sell_currency: Currency | None = None
    sell_usd_per_eur: Decimal | None = Field(default=None, gt=0)
    note: str = Field(default="", max_length=2000)
    created_at: datetime

    @model_validator(mode="after")
    def _sale(self) -> Trade:
        if self.app_id not in TITLES:
            raise ValueError(f"неизвестная игра app_id={self.app_id}")
        sale = (self.sold_at, self.sell_market, self.sell_price, self.sell_currency)
        if any(v is not None for v in sale) and any(v is None for v in sale):
            raise ValueError("для продажи нужны дата, площадка и цена")
        if self.sold_at is not None and self.sold_at < self.bought_at:
            raise ValueError("дата продажи раньше даты покупки")
        return self

    @property
    def sold(self) -> bool:
        """Сделка закрыта продажей."""
        return self.sold_at is not None


@dataclass(frozen=True)
class Valuation:
    """Прибыль сделки: по факту продажи или ожидаемая по текущей цене.

    Attributes:
        sell_market: Площадка продажи (фактическая или целевая).
        sell_price: Цена продажи (фактическая или текущая на целевой площадке).
        cost: Полная стоимость покупки, EUR.
        proceeds: Чистая выручка после комиссий и вывода, EUR.
        profit: Чистая прибыль, EUR.
        roi: Прибыль / стоимость покупки.
        wallet: Выручка остаётся в Steam Wallet и не выводится.
        complete: Все нужные комиссии известны.
        lines: Расходы по строкам, для подробностей.
        warnings: Оговорки расчёта.
    """

    sell_market: str
    sell_price: Money
    cost: Money
    proceeds: Money
    profit: Money
    roi: Decimal
    wallet: bool
    complete: bool
    lines: list[str]
    warnings: list[str]


class NoRate(ValueError):
    """Для расчёта нужен курс USD, а его нет."""


def _fx(rate: Decimal | None, spread: Decimal, *currencies: Currency) -> FxRates:
    if rate is None:
        if any(c is not Currency.EUR for c in currencies):
            raise NoRate("нет курса USD к EUR: прибыль посчитается, когда курс загрузится")
        return FxRates(rates={}, spread=spread)
    return FxRates(rates={Currency.USD: rate}, spread=spread)


def _lines(leg: Leg) -> list[str]:
    return [f"{line.label}: {line.amount}" for line in leg.lines]


def valuate(
    trade: Trade, schedule: FeeSchedule, usd_per_eur: Decimal | None, spread: Decimal
) -> Valuation:
    """Считает прибыль сделки в EUR.

    Args:
        trade: Сделка.
        schedule: Площадки и правила комиссий.
        usd_per_eur: Текущий курс (для ожидаемой выручки открытой сделки).
        spread: Потери при конвертации валют.

    Raises:
        NoRate: Если нужен курс USD, а его нет.
        KeyError: Если площадки нет в правилах.
        ValueError: Если валюта записи не совпадает с валютой площадки.
    """
    if trade.sold:
        assert trade.sell_market and trade.sell_price and trade.sell_currency
        market, price = trade.sell_market, Money.of(trade.sell_price, trade.sell_currency)
        sell_rate = trade.sell_usd_per_eur
    else:
        market, price = trade.target_market, Money.of(trade.target_price, trade.target_currency)
        sell_rate = usd_per_eur
    paid = Money.of(trade.buy_price, trade.buy_currency)
    buy_fx = _fx(trade.buy_usd_per_eur or usd_per_eur, spread, paid.currency)
    sell_fx = _fx(sell_rate, spread, price.currency)

    item = trade.market_hash_name
    buy = RouteCalculator(schedule, buy_fx, trade.buy_market, market, item=item).purchase(paid)
    sell = RouteCalculator(schedule, sell_fx, trade.buy_market, market, item=item).sale(price)
    warnings = list(dict.fromkeys(buy.warnings + sell.warnings))
    profit = sell.total - buy.total
    roi = (profit.amount / buy.total.amount).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    return Valuation(
        sell_market=market,
        sell_price=price,
        cost=buy.total,
        proceeds=sell.total,
        profit=profit,
        roi=roi,
        wallet=schedule.market(market).payout is Payout.STEAM_WALLET,
        complete=not any(w.startswith(("Нет правила", "Неизвестная комиссия")) for w in warnings),
        lines=_lines(buy) + _lines(sell),
        warnings=warnings,
    )


def tradable_from(trade: Trade, schedule: FeeSchedule) -> date:
    """С какого дня купленный предмет можно передать (Trade Protection)."""
    return trade.bought_at + timedelta(days=schedule.market(trade.buy_market).transfer_lock_days)


def payout_at(trade: Trade, schedule: FeeSchedule) -> date | None:
    """С какого дня деньги от продажи доступны для вывода."""
    if trade.sold_at is None or trade.sell_market is None:
        return None
    return trade.sold_at + timedelta(days=schedule.market(trade.sell_market).settlement_days)
