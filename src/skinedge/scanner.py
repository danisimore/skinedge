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
from decimal import ROUND_FLOOR, Decimal
from urllib.parse import quote as urlquote

from skinedge.fees.schedule import FeeSchedule
from skinedge.finance.route import RouteCalculator, RouteResult
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.quotes import Quote, QuoteKind, Sale, SalesStats, SalesWindow

UNDERCUT = Decimal("0.01")  # выставляем на цент дешевле текущего минимума
EPISODE_GAP = timedelta(minutes=5)  # сделки ближе друг к другу — одна массовая покупка
EPISODIC = frozenset({"dmarket", "marketcsgo"})  # volume — число эпизодов, а не штук
_PHASED = ("Doppler", "Gamma Doppler")


@dataclass(frozen=True)
class ScanConfig:
    """Пороги сканера.

    Attributes:
        min_profit: Минимальная чистая прибыль в EUR.
        min_roi: Минимальный ROI (0.05 = 5%).
        max_age: Котировки старше этого не используются.
        min_sales_7d: Минимум продаж за 7 дней на площадке продажи.
        min_sell_listings: Минимум лотов для предварительного отбора без статистики продаж.
        suspicious_roi: ROI выше этого помечается как подозрительный.
        falling: Падение медианы за 24 часа относительно 30 дней, после которого
            предмет помечается как дешевеющий (0.05 = 5%).
    """

    min_profit: Decimal = Decimal("0.50")
    min_roi: Decimal = Decimal("0.05")
    max_age: timedelta = timedelta(minutes=30)
    min_sales_7d: int = 5
    min_sell_listings: int = 5
    suspicious_roi: Decimal = Decimal("0.40")
    falling: Decimal = Decimal("0.05")


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
    quote: Quote | None,
    sales_7d: SalesStats | None,
    cfg: ScanConfig,
    *,
    require_sales: bool = True,
) -> SellEstimate | None:
    """Консервативная цена продажи на площадке.

    Берётся минимальное предложение минус цент, но не выше медианы реальных
    продаж за 7 дней. Продаж за 7 дней должно быть не меньше ``min_sales_7d``:
    без подтверждённых сделок цена лота ничего не говорит о спросе.

    Args:
        quote: Котировка площадки продажи.
        sales_7d: Статистика продаж за 7 дней.
        cfg: Пороги.
        require_sales: ``False`` даёт предварительную оценку без статистики продаж
            (верхняя граница цены): по ней отбираются предметы, чьи продажи стоит
            проверить. В отчёт и в виртуальные сделки такая оценка не попадает.

    Returns:
        Оценка или ``None``, если продать уверенно нельзя.
    """
    if quote is None or quote.min_price is None or quote.quantity < 1:
        return None
    if sales_7d is not None and sales_7d.currency is not quote.currency:
        sales_7d = None  # статистика в прежней валюте площадки с ценой не сравнима
    ask = quote.min_price - UNDERCUT
    if ask <= 0:
        return None
    median = sales_7d.median if sales_7d is not None else None
    if sales_7d is None or median is None or sales_7d.volume < cfg.min_sales_7d:
        if require_sales or quote.quantity < cfg.min_sell_listings:
            return None
        return SellEstimate(Money.of(ask, quote.currency), "предварительно: минимум лотов −0.01")
    if median < ask:
        return SellEstimate(
            Money.of(median, quote.currency), f"медиана продаж 7д, {_count(sales_7d)}"
        )
    return SellEstimate(
        Money.of(ask, quote.currency), f"минимум лотов −0.01, продаж 7д: {_count(sales_7d)}"
    )


def _count(stats: SalesStats) -> str:
    unit = "эпизодов" if stats.source in EPISODIC else "шт."
    return f"{stats.volume} {unit}"


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def stats_from_sales(
    sales: list[Sale],
    source: str,
    app_id: int,
    name: str,
    currency: Currency,
    now: datetime,
    window: SalesWindow = SalesWindow.D7,
) -> SalesStats:
    """Статистика продаж за окно по списку отдельных сделок (DMarket, Market.CSGO).

    Сделки считаются по эпизодам, а не по штукам: продажи, между которыми меньше
    ``EPISODE_GAP``, — это одна массовая покупка одного покупателя, и она не должна
    перевешивать остальной рынок. В ``volume`` записывается число эпизодов,
    в ``median`` — медиана цен эпизодов, но не выше цены самого свежего эпизода:
    если рынок только что продавал дешевле, на старую цену рассчитывать нельзя.
    """
    days = {SalesWindow.H24: 1, SalesWindow.D7: 7, SalesWindow.D30: 30, SalesWindow.D90: 90}
    since = now - timedelta(days=days[window])
    recent = sorted(
        (s for s in sales if s.sold_at >= since and s.currency == currency),
        key=lambda s: s.sold_at,
    )
    episodes: list[list[Decimal]] = []
    last: datetime | None = None
    for sale in recent:
        if last is None or sale.sold_at - last > EPISODE_GAP:
            episodes.append([])
        episodes[-1].append(sale.price)
        last = sale.sold_at
    prices = [p for e in episodes for p in e]
    median = None
    if episodes:
        typical = _median([_median(e) for e in episodes])
        median = min(typical, _median(episodes[-1])).quantize(Decimal("0.01"), ROUND_FLOOR)
    return SalesStats(
        source=source,
        app_id=app_id,
        market_hash_name=name,
        currency=currency,
        window=window,
        min=min(prices) if prices else None,
        max=max(prices) if prices else None,
        avg=(sum(prices, Decimal(0)) / len(prices)).quantize(Decimal("0.01")) if prices else None,
        median=median,
        volume=len(episodes),
        fetched_at=now,
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
        trends: dict[str, Decimal] | None = None,
    ) -> list[Opportunity]:
        """Ищет выгодные маршруты ``buy_market → sell_market``.

        Args:
            buy_market: Площадка покупки.
            sell_market: Площадка продажи.
            buy_quotes: Котировки площадки покупки по названию.
            sell_quotes: Котировки площадки продажи по названию.
            sell_sales: Статистика продаж за 7 дней на площадке продажи.
            now: Текущее время (для проверки свежести).
            trends: Изменение цены предмета (медиана 24ч к медиане 30д − 1).

        Returns:
            Возможности, прошедшие пороги: сначала полные расчёты, затем по ROI.
        """
        found: list[Opportunity] = []
        min_profit = Money.of(self.cfg.min_profit, Currency.EUR)
        for name in sorted(buy_quotes.keys() & sell_quotes.keys()):
            bq, sq = buy_quotes[name], sell_quotes[name]
            if bq.min_price is None or bq.quantity < 1:
                continue
            if not self._current(buy_market, bq) or not self._current(sell_market, sq):
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
                    flags=self._flags(name, bq, result, (trends or {}).get(name)),
                    buy_url=self._url(buy_market, name, bq),
                    sell_url=self._url(sell_market, name, sq),
                )
            )
        return sorted(found, key=lambda o: (not o.result.complete, -o.result.roi))

    def candidates(
        self,
        buy_market: str,
        sell_market: str,
        buy_quotes: dict[str, Quote],
        sell_quotes: dict[str, Quote],
        now: datetime,
        limit: int,
    ) -> list[str]:
        """Предметы, у которых стоит проверить реальные продажи на площадке продажи.

        Цена продажи берётся предварительно, по минимальному лоту без статистики.
        Это верхняя граница: медиана продаж может её только понизить. Поэтому
        предмет, невыгодный даже по такой цене, проверять не нужно.

        Returns:
            До ``limit`` названий, самые выгодные по предварительной оценке первыми.
        """
        scored: list[tuple[Decimal, str]] = []
        min_profit = Money.of(self.cfg.min_profit, Currency.EUR)
        for name in buy_quotes.keys() & sell_quotes.keys():
            bq, sq = buy_quotes[name], sell_quotes[name]
            if bq.min_price is None or bq.quantity < 1:
                continue
            if not self._current(buy_market, bq) or not self._current(sell_market, sq):
                continue
            if now - bq.fetched_at > self.cfg.max_age or now - sq.fetched_at > self.cfg.max_age:
                continue
            sell = estimate_sell(sq, None, self.cfg, require_sales=False)
            if sell is None:
                continue
            calc = RouteCalculator(self.schedule, self.fx, buy_market, sell_market, item=name)
            cost = calc.purchase(Money.of(bq.min_price, bq.currency)).total
            profit = calc.sale(sell.price).total - cost
            if profit >= min_profit and profit.amount >= cost.amount * self.cfg.min_roi:
                scored.append((profit.amount / cost.amount, name))
        return [name for _, name in sorted(scored, reverse=True)[:limit]]

    def _current(self, market: str, quote: Quote) -> bool:
        """Котировка в нынешней валюте площадки (не сохранена до смены валюты)."""
        return quote.currency is self.schedule.market(market).currency

    def _flags(
        self, name: str, buy: Quote, result: RouteResult, trend: Decimal | None
    ) -> list[str]:
        flags = []
        if not result.complete:
            flags.append("Расчёт неполный: есть неизвестные комиссии")
        if trend is not None and trend <= -self.cfg.falling:
            flags.append(
                f"Цена падает: медиана Skinport за 24ч на {-trend * 100:.1f}% ниже, чем за 30 дней"
            )
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
        return template.format(name=urlquote(name), app_id=quote.app_id) if template else None
