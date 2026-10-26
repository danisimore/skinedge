"""Поиск арбитражных возможностей между двумя площадками.

Сканер берёт сохранённые котировки двух площадок, сопоставляет предметы по
``market_hash_name`` и для каждого считает полный маршрут «купить по минимальной
цене на площадке A → выставить на площадке B» через :class:`RouteCalculator`.

Цена продажи оценивается консервативно (см. :func:`estimate_sell`): мы не можем
продать дороже текущего минимального предложения, а при наличии статистики
продаж и дороже медианы реальных сделок.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import quote as urlquote

from skinedge.fees.schedule import FeeSchedule
from skinedge.finance.route import RouteCalculator, RouteResult
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.quotes import Quote, QuoteKind, SalesStats

UNDERCUT = Decimal("0.01")  # выставляем на цент дешевле текущего минимума
_PHASED = ("Doppler", "Gamma Doppler")


@dataclass(frozen=True)
class ScanConfig:
    """Пороги сканера.

    Attributes:
        min_profit: Минимальная чистая прибыль в EUR.
        min_roi: Минимальный ROI (0.05 = 5%).
        max_age: Котировки старше этого не используются.
        min_sales_7d: Минимум продаж за 7 дней на площадке продажи (если есть статистика).
        min_sell_listings: Минимум лотов на площадке продажи без статистики продаж.
        suspicious_roi: ROI выше этого помечается как подозрительный.
    """

    min_profit: Decimal = Decimal("0.50")
    min_roi: Decimal = Decimal("0.05")
    max_age: timedelta = timedelta(minutes=30)
    min_sales_7d: int = 5
    min_sell_listings: int = 5
    suspicious_roi: Decimal = Decimal("0.40")


@dataclass(frozen=True)
class SellEstimate:
    """Оценка цены, по которой предмет реально продастся.

    Attributes:
        price: Цена выставления в валюте площадки.
        basis: Как получена цена (для отчёта).
    """

    price: Money
    basis: str


def estimate_sell(
    quote: Quote | None, sales_7d: SalesStats | None, cfg: ScanConfig
) -> SellEstimate | None:
    """Консервативная цена продажи на площадке.

    Берётся минимальное предложение минус цент. Если есть статистика продаж
    за 7 дней, цена не выше медианы реальных сделок, а продаж должно быть не
    меньше ``min_sales_7d``. Без статистики нужно не меньше ``min_sell_listings``
    лотов, иначе спрос не подтверждён.

    Returns:
        Оценка или ``None``, если продать уверенно нельзя.
    """
    if quote is None or quote.min_price is None or quote.quantity < 1:
        return None
    ask = quote.min_price - UNDERCUT
    if ask <= 0:
        return None
    if sales_7d is not None:
        if sales_7d.volume < cfg.min_sales_7d or sales_7d.median is None:
            return None
        if sales_7d.median < ask:
            return SellEstimate(
                Money.of(sales_7d.median, quote.currency),
                f"медиана продаж 7д, {sales_7d.volume} шт.",
            )
        return SellEstimate(
            Money.of(ask, quote.currency), f"минимум лотов −0.01, продаж 7д {sales_7d.volume}"
        )
    if quote.quantity < cfg.min_sell_listings:
        return None
    return SellEstimate(
        Money.of(ask, quote.currency),
        f"минимум лотов −0.01, лотов {quote.quantity}, статистики продаж нет",
    )


@dataclass(frozen=True)
class Opportunity:
    """Найденная возможность.

    Attributes:
        name: Название предмета.
        buy_quote: Котировка на площадке покупки.
        sell_quote: Котировка на площадке продажи.
        sell: Оценка цены продажи.
        result: Полный расчёт маршрута.
        flags: Риски, которые нужно проверить вручную.
        buy_url: Ссылка на предмет на площадке покупки.
        sell_url: Ссылка на предмет на площадке продажи.
    """

    name: str
    buy_quote: Quote
    sell_quote: Quote
    sell: SellEstimate
    result: RouteResult
    flags: list[str] = field(default_factory=list)
    buy_url: str | None = None
    sell_url: str | None = None


class Scanner:
    """Сканер маршрутов между площадками.

    Args:
        schedule: Площадки и комиссии.
        fx: Курсы валют.
        config: Пороги.
    """

    def __init__(self, schedule: FeeSchedule, fx: FxRates, config: ScanConfig) -> None:
        self.schedule = schedule
        self.fx = fx
        self.cfg = config

    def scan(
        self,
        buy_market: str,
        sell_market: str,
        buy_quotes: dict[str, Quote],
        sell_quotes: dict[str, Quote],
        sell_sales: dict[str, SalesStats],
        now: datetime,
    ) -> list[Opportunity]:
        """Ищет выгодные маршруты ``buy_market → sell_market``.

        Args:
            buy_market: Площадка покупки.
            sell_market: Площадка продажи.
            buy_quotes: Котировки площадки покупки по названию.
            sell_quotes: Котировки площадки продажи по названию.
            sell_sales: Статистика продаж за 7 дней на площадке продажи.
            now: Текущее время (для проверки свежести).

        Returns:
            Возможности, прошедшие пороги: сначала полные расчёты, затем по ROI.
        """
        found: list[Opportunity] = []
        min_profit = Money.of(self.cfg.min_profit, Currency.EUR)
        for name in sorted(buy_quotes.keys() & sell_quotes.keys()):
            bq, sq = buy_quotes[name], sell_quotes[name]
            if bq.min_price is None or bq.quantity < 1:
                continue
            if now - bq.fetched_at > self.cfg.max_age or now - sq.fetched_at > self.cfg.max_age:
                continue
            sell = estimate_sell(sq, sell_sales.get(name), self.cfg)
            if sell is None:
                continue
            calc = RouteCalculator(self.schedule, self.fx, buy_market, sell_market, item=name)
            buy_price = Money.of(bq.min_price, bq.currency)
            # Быстрая отсечка до полного расчёта с бинарным поиском безубыточности.
            if calc.sale(sell.price).total - calc.purchase(buy_price).total < min_profit:
                continue
            result = calc.evaluate(buy_price, sell.price)
            if result.roi < self.cfg.min_roi:
                continue
            found.append(
                Opportunity(
                    name=name,
                    buy_quote=bq,
                    sell_quote=sq,
                    sell=sell,
                    result=result,
                    flags=self._flags(name, bq, result),
                    buy_url=self._url(buy_market, name, bq),
                    sell_url=self._url(sell_market, name, sq),
                )
            )
        return sorted(found, key=lambda o: (not o.result.complete, -o.result.roi))

    def _flags(self, name: str, buy: Quote, result: RouteResult) -> list[str]:
        flags = []
        if not result.complete:
            flags.append("Расчёт неполный: есть неизвестные комиссии")
        if result.roi > self.cfg.suspicious_roi:
            flags.append(
                "ROI подозрительно высокий: проверь лот вручную (наклейки, паттерн, float)"
            )
        if any(p in name for p in _PHASED):
            flags.append("Цена зависит от фазы, а сводка смешивает фазы")
        if buy.kind is QuoteKind.AGGREGATE:
            flags.append("Цена покупки из сводки: проверь float и наклейки самого дешёвого лота")
        return flags

    def _url(self, market: str, name: str, quote: Quote) -> str | None:
        if quote.url:
            return quote.url
        template = self.schedule.market(market).item_url
        return template.format(name=urlquote(name)) if template else None
