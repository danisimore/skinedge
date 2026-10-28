"""Журнал реальных сделок: что куплено, где собираешься продать, что вышло.

Записи вводит пользователь в интерфейсе (``skinedge app``); SkinEdge сам ничего
не покупает и не продаёт. Прибыль считается теми же правилами комиссий, что и в
сканере (:class:`skinedge.finance.route.RouteCalculator`): комиссия покупателя,
комиссия продавца и вывод денег, итог в EUR.

Курсы USD (ЕЦБ) и RUB (ЦБ РФ) берутся на момент записи: при покупке для
стоимости, при продаже для выручки. Пока сделка открыта, ожидаемая выручка
считается по текущему курсу.

Цены записываются в валюте площадки на момент записи. Если площадка потом
сменила валюту (Market.CSGO перешёл с EUR на RUB), старая запись считается в
своей валюте, а интерфейс просит вписать цену заново.
"""

from __future__ import annotations

from collections.abc import Mapping
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
        buy_usd_per_eur: Курс USD на момент записи покупки.
        buy_rub_per_eur: Курс RUB на момент записи покупки.
        target_market: Где собираешься продать.
        target_price: Сколько предмет стоит там сейчас, в валюте площадки.
        target_currency: Валюта площадки продажи.
        target_at: Когда записана текущая цена.
        sold_at: Дата продажи.
        sell_market: Где продан.
        sell_price: Цена продажи в валюте площадки.
        sell_currency: Валюта площадки продажи.
        sell_usd_per_eur: Курс USD на момент записи продажи.
        sell_rub_per_eur: Курс RUB на момент записи продажи.
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
    buy_rub_per_eur: Decimal | None = Field(default=None, gt=0)
    target_market: str
    target_price: Decimal = PRICE
    target_currency: Currency
    target_at: datetime
    sold_at: date | None = None
    sell_market: str | None = None
    sell_price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    sell_currency: Currency | None = None
    sell_usd_per_eur: Decimal | None = Field(default=None, gt=0)
    sell_rub_per_eur: Decimal | None = Field(default=None, gt=0)
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

    @property
    def buy_rates(self) -> dict[Currency, Decimal | None]:
        """Курсы на момент записи покупки."""
        return {Currency.USD: self.buy_usd_per_eur, Currency.RUB: self.buy_rub_per_eur}

    @property
    def sell_rates(self) -> dict[Currency, Decimal | None]:
        """Курсы на момент записи продажи."""
        return {Currency.USD: self.sell_usd_per_eur, Currency.RUB: self.sell_rub_per_eur}


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
    """Для расчёта нужен курс валюты к EUR, а его нет."""


def _fx(rates: Mapping[Currency, Decimal | None], spread: Decimal, currency: Currency) -> FxRates:
    known = {c: r for c, r in rates.items() if r is not None}
    if currency is not Currency.EUR and currency not in known:
        raise NoRate(f"нет курса {currency} к EUR: прибыль посчитается, когда курс загрузится")
    return FxRates(rates=known, spread=spread)


def _former(schedule: FeeSchedule, market: str, price: Money, what: str) -> list[str]:
    now = schedule.market(market).currency
    if price.currency is now:
        return []
    return [
        f"{what} записана в {price.currency}, а площадка теперь в {now}:"
        f" открой «Изменить» и впиши цену в {now}"
    ]


def _lines(leg: Leg) -> list[str]:
    return [f"{line.label}: {line.amount}" for line in leg.lines]


def valuate(
    trade: Trade, schedule: FeeSchedule, rates: Mapping[Currency, Decimal], spread: Decimal
) -> Valuation:
    """Считает прибыль сделки в EUR.

    Args:
        trade: Сделка.
        schedule: Площадки и правила комиссий.
        rates: Текущие курсы к EUR (для ожидаемой выручки открытой сделки и для
            покупки, записанной без курса).
        spread: Потери при конвертации валют.

    Raises:
        NoRate: Если нужен курс валюты, а его нет.
        KeyError: Если площадки нет в правилах.
        ValueError: Если запись в прежней валюте площадки, а правила площадки с суммами.
    """
    if trade.sold:
        assert trade.sell_market and trade.sell_price and trade.sell_currency
        market, price = trade.sell_market, Money.of(trade.sell_price, trade.sell_currency)
        sell_rates: Mapping[Currency, Decimal | None] = trade.sell_rates
        what = "Цена продажи"
    else:
        market, price = trade.target_market, Money.of(trade.target_price, trade.target_currency)
        sell_rates = rates
        what = "Текущая цена"
    paid = Money.of(trade.buy_price, trade.buy_currency)
    buy_rates = {c: r or rates.get(c) for c, r in trade.buy_rates.items()}
    buy_fx = _fx(buy_rates, spread, paid.currency)
    sell_fx = _fx(sell_rates, spread, price.currency)

    item = trade.market_hash_name
    # Запись, сделанная до смены валюты площадки, считается в своей валюте.
    buy_schedule = schedule.in_currency(trade.buy_market, paid.currency)
    sell_schedule = schedule.in_currency(market, price.currency)
    buy = RouteCalculator(buy_schedule, buy_fx, trade.buy_market, market, item=item).purchase(paid)
    sell = RouteCalculator(sell_schedule, sell_fx, trade.buy_market, market, item=item).sale(price)
    former = _former(schedule, trade.buy_market, paid, "Цена покупки") + _former(
        schedule, market, price, what
    )
    warnings = list(dict.fromkeys(former + buy.warnings + sell.warnings))
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
