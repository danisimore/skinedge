"""Расчёт прибыли по маршруту «купить на площадке A → продать на площадке B».

Все суммы считаются в ``Decimal`` с округлением до центов. Итоги приводятся
к базовой валюте (EUR) через :class:`skinedge.fx.FxRates` консервативно:
расходы округляются вверх, доходы вниз.

Безубыточность и предельные цены ищутся бинарным поиском по центам. Это даёт
точный ответ с учётом минимальных комиссий, округления и ценовых порогов,
при условии что выручка не убывает с ростом цены продажи (так и есть для всех
текущих правил).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from skinedge.fees.models import FeeKind, FeeRule, Operation, Verification
from skinedge.fees.schedule import FeeSchedule, Marketplace, Payout
from skinedge.finance.steam import seller_receives
from skinedge.fx import Direction, FxRates
from skinedge.money import Currency, Money

BALANCE = "balance"  # способ оплаты: деньги уже лежат на балансе площадки
_SEARCH_CAP_MINOR = 10**9  # 10 млн в валюте: верхняя граница поиска цены


@dataclass(frozen=True)
class FeeLine:
    """Одна строка расчёта: комиссия или конвертация.

    Attributes:
        label: Что это за расход.
        base: С какой суммы считается.
        amount: Размер расхода.
        rule: Правило комиссии, если расход задан правилом.
    """

    label: str
    base: Money
    amount: Money
    rule: FeeRule | None = None

    @property
    def verification(self) -> Verification | None:
        """Насколько подтверждён расход."""
        return self.rule.verification if self.rule else None


@dataclass(frozen=True)
class Leg:
    """Результат одной стороны маршрута (покупка или продажа)."""

    total: Money  # в базовой валюте: полная стоимость покупки или чистая выручка
    native: Money  # в валюте площадки, до конвертации
    lines: list[FeeLine]
    warnings: list[str]


@dataclass(frozen=True)
class RouteResult:
    """Полный расчёт маршрута.

    Attributes:
        buy_market: Площадка покупки.
        sell_market: Площадка продажи.
        buy_price: Цена лота на площадке покупки.
        sell_price: Предполагаемая цена продажи на площадке продажи.
        total_cost: Полная стоимость покупки в базовой валюте.
        net_proceeds: Чистая выручка в базовой валюте.
        profit: Чистая прибыль.
        roi: Прибыль / вложенный капитал.
        payout: Деньги или баланс Steam.
        lock_days: Минимум дней, на которые заморожен капитал.
        breakeven_sell: Минимальная цена продажи без убытка (в валюте площадки продажи).
        safety_margin: На сколько может упасть цена продажи до безубыточности (доля).
        buy_lines: Расходы при покупке.
        sell_lines: Расходы при продаже.
        warnings: Неизвестные или неподтверждённые комиссии и другие оговорки.
    """

    buy_market: str
    sell_market: str
    buy_price: Money
    sell_price: Money
    total_cost: Money
    net_proceeds: Money
    profit: Money
    roi: Decimal
    payout: Payout
    lock_days: int
    breakeven_sell: Money | None
    safety_margin: Decimal | None
    buy_lines: list[FeeLine]
    sell_lines: list[FeeLine]
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Все комиссии известны (возможно, не подтверждены)."""
        return not any(
            line.verification is Verification.UNKNOWN for line in self.buy_lines + self.sell_lines
        ) and not any(w.startswith("Нет правила") for w in self.warnings)

    @property
    def confirmed(self) -> bool:
        """Все комиссии подтверждены официально или в аккаунте."""
        return all(
            line.rule is None or line.rule.is_confirmed for line in self.buy_lines + self.sell_lines
        )


class RouteCalculator:
    """Считает маршрут между двумя площадками по заданным правилам.

    Args:
        schedule: Площадки и правила комиссий.
        fx: Курсы валют.
        buy_market: Площадка покупки.
        sell_market: Площадка продажи.
        deposit_method: Способ пополнения площадки покупки или ``"balance"``.
        withdraw_method: Способ вывода с площадки продажи.
        base_currency: Валюта, в которой считается прибыль.
        item: Название предмета: включает комиссии, заданные для конкретного предмета.
    """

    def __init__(
        self,
        schedule: FeeSchedule,
        fx: FxRates,
        buy_market: str,
        sell_market: str,
        deposit_method: str = BALANCE,
        withdraw_method: str | None = None,
        base_currency: Currency = Currency.EUR,
        item: str | None = None,
    ) -> None:
        self.schedule = schedule
        self.fx = fx
        self.buy: Marketplace = schedule.market(buy_market)
        self.sell: Marketplace = schedule.market(sell_market)
        self.deposit_method = deposit_method
        self.withdraw_method = withdraw_method
        self.base = base_currency
        self.item = item
        if self.sell.payout is Payout.CASH and withdraw_method is None:
            methods = schedule.methods(sell_market, Operation.WITHDRAW)
            self.withdraw_method = methods[0] if len(methods) == 1 else None

    # ---------- стороны маршрута ----------

    def _apply(
        self, operation: Operation, market: str, base: Money, method: str | None, label: str
    ) -> tuple[Money, list[FeeLine], list[str]]:
        rules = self.schedule.select(market, operation, base, method, self.item)
        if not rules:
            suffix = f" ({method})" if method else ""
            return Money.zero(base.currency), [], [f"Нет правила: {market} {operation}{suffix}"]
        total = Money.zero(base.currency)
        lines = []
        warnings = []
        for rule in rules:
            fee = rule.compute(base)
            total += fee
            lines.append(FeeLine(f"{label}: {rule.describe()}", base, fee, rule))
            if rule.verification is Verification.UNKNOWN:
                warnings.append(f"Неизвестная комиссия: {rule.id}. {rule.note}".strip())
        return total, lines, warnings

    def purchase(self, price: Money) -> Leg:
        """Полная стоимость покупки лота по цене ``price``."""
        self._check_currency(price, self.buy)
        lines: list[FeeLine] = []
        warnings: list[str] = []

        buyer_fee, ls, ws = self._apply(
            Operation.BUY, self.buy.name, price, None, "Комиссия покупателя"
        )
        lines += ls
        warnings += ws
        needed = price + buyer_fee

        paid = needed
        if self.deposit_method != BALANCE:
            dep_fee, ls, ws = self._apply(
                Operation.DEPOSIT, self.buy.name, needed, self.deposit_method, "Пополнение"
            )
            lines += ls
            warnings += ws
            paid = needed + dep_fee

        total = self.fx.convert(paid, self.base, Direction.PAY)
        if paid.currency != self.base:
            lines.append(
                FeeLine(
                    self._fx_label(paid),
                    paid,
                    total,
                )
            )
        return Leg(total=total, native=paid, lines=lines, warnings=warnings)

    def sale(self, price: Money) -> Leg:
        """Чистая выручка от продажи по цене ``price``."""
        self._check_currency(price, self.sell)
        lines: list[FeeLine] = []
        warnings: list[str] = []

        rules = self.schedule.select(self.sell.name, Operation.SELL, price, item=self.item)
        steam_rules = [r for r in rules if r.kind is FeeKind.STEAM]
        if steam_rules:
            rule = steam_rules[0]
            received = seller_receives(price, rule.extra["steam_rate"], rule.extra["game_rate"])
            lines.append(
                FeeLine(f"Комиссия продавца: {rule.describe()}", price, price - received, rule)
            )
            balance = received
        else:
            fee, ls, ws = self._apply(
                Operation.SELL, self.sell.name, price, None, "Комиссия продавца"
            )
            lines += ls
            warnings += ws
            balance = price - fee

        cash = balance
        if self.sell.payout is Payout.CASH:
            if self.withdraw_method is None:
                warnings.append(f"Нет правила: {self.sell.name} withdraw (способ вывода не выбран)")
            else:
                w_fee, ls, ws = self._apply(
                    Operation.WITHDRAW, self.sell.name, balance, self.withdraw_method, "Вывод"
                )
                lines += ls
                warnings += ws
                cash = balance - w_fee
        else:
            warnings.append("Выручка остаётся в Steam Wallet: это не деньги, их нельзя вывести")

        total = self.fx.convert(cash, self.base, Direction.RECEIVE)
        if cash.currency != self.base:
            lines.append(
                FeeLine(
                    self._fx_label(cash),
                    cash,
                    total,
                )
            )
        return Leg(total=total, native=cash, lines=lines, warnings=warnings)

    # ---------- итог ----------

    def evaluate(self, buy_price: Money, sell_price: Money) -> RouteResult:
        """Считает маршрут целиком.

        Args:
            buy_price: Цена лота на площадке покупки.
            sell_price: Предполагаемая цена продажи на площадке продажи.

        Returns:
            Полный расчёт с комиссиями, прибылью, ROI и безубыточностью.
        """
        buy = self.purchase(buy_price)
        sell = self.sale(sell_price)
        profit = sell.total - buy.total
        roi = _ratio(profit.amount, buy.total.amount)
        breakeven = self.breakeven_sell(buy_price)
        margin = None
        if breakeven is not None and sell_price.amount > 0:
            margin = _ratio(sell_price.amount - breakeven.amount, sell_price.amount)
        warnings = list(dict.fromkeys(buy.warnings + sell.warnings))
        for line in buy.lines + sell.lines:
            if line.rule is not None and line.verification is Verification.THIRD_PARTY:
                warnings.append(f"Комиссия не подтверждена официально: {line.rule.id}")
        return RouteResult(
            buy_market=self.buy.name,
            sell_market=self.sell.name,
            buy_price=buy_price,
            sell_price=sell_price,
            total_cost=buy.total,
            net_proceeds=sell.total,
            profit=profit,
            roi=roi,
            payout=self.sell.payout,
            lock_days=self.buy.transfer_lock_days + self.sell.settlement_days,
            breakeven_sell=breakeven,
            safety_margin=margin,
            buy_lines=buy.lines,
            sell_lines=sell.lines,
            warnings=list(dict.fromkeys(warnings)),
        )

    def breakeven_sell(self, buy_price: Money) -> Money | None:
        """Минимальная цена продажи, при которой прибыль не отрицательна."""
        cost = self.purchase(buy_price).total
        return self._min_sell(lambda proceeds: proceeds >= cost)

    def required_sell_for_roi(self, buy_price: Money, target_roi: Decimal) -> Money | None:
        """Минимальная цена продажи для заданного ROI (0.10 = 10%)."""
        cost = self.purchase(buy_price).total
        target = cost + cost * target_roi
        return self._min_sell(lambda proceeds: proceeds.amount >= target.amount)

    def max_buy_price(self, sell_price: Money, target_profit: Money) -> Money | None:
        """Максимальная цена покупки, при которой прибыль не меньше ``target_profit``.

        Returns:
            Цена в валюте площадки покупки или ``None``, если цель недостижима.
        """
        if target_profit.currency != self.base:
            raise ValueError("Целевая прибыль задаётся в базовой валюте")
        proceeds = self.sale(sell_price).total

        def ok(minor: int) -> bool:
            cost = self.purchase(Money.from_minor(minor, self.buy.currency)).total
            return proceeds - cost >= target_profit

        if not ok(0):
            return None
        lo, hi = 0, _SEARCH_CAP_MINOR
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid - 1
        return Money.from_minor(lo, self.buy.currency)

    def _min_sell(self, ok: Callable[[Money], bool]) -> Money | None:
        def passes(minor: int) -> bool:
            return ok(self.sale(Money.from_minor(minor, self.sell.currency)).total)

        hi = 1
        while not passes(hi):
            hi *= 2
            if hi > _SEARCH_CAP_MINOR:
                return None
        lo = hi // 2
        while lo < hi:
            mid = (lo + hi) // 2
            if passes(mid):
                hi = mid
            else:
                lo = mid + 1
        return Money.from_minor(lo, self.sell.currency)

    def _fx_label(self, money: Money) -> str:
        spread = (self.fx.spread * 100).normalize()
        return f"Конвертация {money.currency}→{self.base} (спред {spread:f}%)"

    @staticmethod
    def _check_currency(price: Money, market: Marketplace) -> None:
        if price.currency != market.currency:
            raise ValueError(f"Цена {price} не в валюте площадки {market.name} ({market.currency})")


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (numerator / denominator).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
