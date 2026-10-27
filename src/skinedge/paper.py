"""Виртуальные сделки (paper trading).

Каждая найденная возможность записывается как сделка, которую мы «совершили бы».
Реальный результат станет известен только после блокировки (7 дней Trade
Protection и 7 дней до выплаты), поэтому сделка проверяется по контрольным
точкам: что показала бы площадка продажи через 24 часа, 3 дня и 7 дней.
Главная точка: 7 дней, когда купленный предмет можно передать и выставить.

Пересчёт идёт по тем же правилам, что и сигнал: та же оценка цены продажи,
те же комиссии и тот же курс, чтобы результат отражал изменение рынка, а не
изменение курса.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from skinedge.fees.schedule import FeeSchedule
from skinedge.finance.route import RouteCalculator
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.quotes import SalesWindow

if TYPE_CHECKING:
    from skinedge.scanner import Opportunity, ScanConfig
    from skinedge.storage import Storage

CHECKPOINTS: tuple[tuple[str, timedelta], ...] = (
    ("24ч", timedelta(days=1)),
    ("3д", timedelta(days=3)),
    ("7д", timedelta(days=7)),
)
COOLDOWN = timedelta(hours=24)  # один и тот же маршрут записываем не чаще раза в сутки

# Версия модели оценки. Сделки старой модели остаются в базе, но не входят в отчёт:
# модель 1 оценивала продажу на DMarket по минимальному лоту без проверки реальных
# сделок и находила ложные возможности.
MODEL = 2


class PaperTrade(BaseModel):
    """Виртуальная сделка в момент сигнала. Суммы в EUR уже с комиссиями."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    created_at: datetime
    app_id: int
    market_hash_name: str
    buy_market: str
    sell_market: str
    buy_price: Decimal
    buy_currency: Currency
    sell_price: Decimal
    sell_currency: Currency
    cost_eur: Decimal
    proceeds_eur: Decimal
    profit_eur: Decimal
    roi: Decimal
    usd_per_eur: Decimal | None
    complete: bool
    lock_days: int
    model: int = MODEL

    @classmethod
    def from_opportunity(cls, opp: Opportunity, fx: FxRates, now: datetime) -> PaperTrade:
        """Сделка по найденной возможности."""
        r = opp.result
        return cls(
            created_at=now,
            app_id=opp.buy_quote.app_id,
            market_hash_name=opp.name,
            buy_market=r.buy_market,
            sell_market=r.sell_market,
            buy_price=r.buy_price.amount,
            buy_currency=r.buy_price.currency,
            sell_price=r.sell_price.amount,
            sell_currency=r.sell_price.currency,
            cost_eur=r.total_cost.amount,
            proceeds_eur=r.net_proceeds.amount,
            profit_eur=r.profit.amount,
            roi=r.roi,
            usd_per_eur=fx.rates.get(Currency.USD),
            complete=r.complete,
            lock_days=r.lock_days,
        )


def record(
    storage: Storage,
    opportunities: list[Opportunity],
    fx: FxRates,
    now: datetime,
    floor: ScanConfig,
) -> int:
    """Записывает новые возможности, пропуская маршруты, записанные за последние сутки.

    Записываются только возможности, прошедшие пороги ``floor`` (стандартные
    пороги сканера). Так прогоны с пониженными порогами для диагностики не
    засоряют статистику.

    Returns:
        Сколько сделок записано.
    """
    count = 0
    for opp in opportunities:
        r = opp.result
        if r.roi < floor.min_roi or r.profit.amount < floor.min_profit:
            continue
        last = storage.last_paper(opp.name, r.buy_market, r.sell_market, opp.buy_quote.app_id)
        if last is not None and now - last < COOLDOWN:
            continue
        storage.save_paper(PaperTrade.from_opportunity(opp, fx, now))
        count += 1
    return count


@dataclass(frozen=True)
class Check:
    """Результат сделки в контрольной точке.

    Attributes:
        label: Контрольная точка.
        sellable: Нашлась ли уверенная цена продажи.
        sell_price: Оценка цены продажи в этот момент.
        profit_eur: Прибыль, если бы продали по этой цене.
        roi: ROI.
    """

    label: str
    sellable: bool
    sell_price: Money | None
    profit_eur: Decimal | None
    roi: Decimal | None


def evaluate(
    trade: PaperTrade,
    storage: Storage,
    schedule: FeeSchedule,
    spread: Decimal,
    cfg: ScanConfig,
    now: datetime,
) -> dict[str, Check]:
    """Пересчитывает сделку по данным, собранным после сигнала.

    Returns:
        Результат по каждой наступившей контрольной точке, для которой есть данные.
    """
    from skinedge.scanner import estimate_sell

    rates = {Currency.USD: trade.usd_per_eur} if trade.usd_per_eur is not None else {}
    fx = FxRates(rates=rates, spread=spread, source="paper")
    calc = RouteCalculator(
        schedule, fx, trade.buy_market, trade.sell_market, item=trade.market_hash_name
    )
    checks: dict[str, Check] = {}
    for label, delta in CHECKPOINTS:
        at = trade.created_at + delta
        if at > now:
            break
        quote = storage.quote_after(trade.market_hash_name, trade.sell_market, at, trade.app_id)
        if quote is None or quote.fetched_at - at > timedelta(days=1):
            continue  # нет данных около контрольной точки
        sales = storage.sales_after(
            trade.market_hash_name, trade.sell_market, SalesWindow.D7, at, trade.app_id
        )
        est = estimate_sell(quote, sales, cfg)
        if est is None:
            checks[label] = Check(label, False, None, None, None)
            continue
        proceeds = calc.sale(est.price).total.amount
        profit = proceeds - trade.cost_eur
        roi = (profit / trade.cost_eur).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        checks[label] = Check(label, True, est.price, profit, roi)
    return checks


@dataclass(frozen=True)
class Summary:
    """Сводка по контрольной точке.

    Attributes:
        label: Контрольная точка.
        evaluated: Сколько сделок дошли до точки и имеют данные.
        profitable: Сколько из них в плюсе.
        unsellable: Сколько нельзя было уверенно продать.
        avg_roi: Средний ROI проверенных сделок.
        total_profit: Суммарная прибыль, EUR.
    """

    label: str
    evaluated: int
    profitable: int
    unsellable: int
    avg_roi: Decimal | None
    total_profit: Decimal


def summarize(results: list[dict[str, Check]]) -> list[Summary]:
    """Сводка по всем сделкам для каждой контрольной точки."""
    out = []
    for label, _ in CHECKPOINTS:
        checks = [r[label] for r in results if label in r]
        sold = [c for c in checks if c.sellable and c.profit_eur is not None]
        rois = [c.roi for c in sold if c.roi is not None]
        avg = None
        if rois:
            avg = (sum(rois, Decimal(0)) / len(rois)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        out.append(
            Summary(
                label=label,
                evaluated=len(checks),
                profitable=sum(1 for c in sold if c.profit_eur is not None and c.profit_eur > 0),
                unsellable=len(checks) - len(sold),
                avg_roi=avg,
                total_profit=sum((c.profit_eur or Decimal(0) for c in sold), Decimal(0)),
            )
        )
    return out


@dataclass(frozen=True)
class Report:
    """Отчёт по виртуальным сделкам.

    Attributes:
        trades: Сделки за период, старые первыми.
        results: Контрольные точки каждой сделки (в том же порядке).
        summaries: Сводка по каждой контрольной точке.
    """

    trades: list[PaperTrade]
    results: list[dict[str, Check]]
    summaries: list[Summary]

    @property
    def avg_signal_roi(self) -> Decimal | None:
        """Средний ROI в момент сигнала."""
        if not self.trades:
            return None
        return sum((t.roi for t in self.trades), Decimal(0)) / len(self.trades)


def build_report(
    storage: Storage,
    schedule: FeeSchedule,
    spread: Decimal,
    cfg: ScanConfig,
    now: datetime,
    since: datetime,
) -> Report:
    """Пересчитывает виртуальные сделки, записанные после ``since``."""
    trades = storage.paper_trades(since)
    results = [evaluate(t, storage, schedule, spread, cfg, now) for t in trades]
    return Report(trades=trades, results=results, summaries=summarize(results))
