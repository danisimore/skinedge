"""Командная строка SkinEdge."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer

from skinedge.config import Settings
from skinedge.fees.models import Operation
from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.finance.route import BALANCE, FeeLine, RouteCalculator, RouteResult
from skinedge.fx import FxRates
from skinedge.money import Currency, Money

app = typer.Typer(help="SkinEdge: анализ арбитража игровых предметов. Только анализ, без сделок.")


@app.callback()
def main() -> None:
    """SkinEdge CLI."""


def _decimal(value: str, name: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{name}: не число: {value}") from exc


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _print_lines(title: str, lines: list[FeeLine]) -> None:
    typer.echo(title)
    if not lines:
        typer.echo("  (нет расходов)")
    for line in lines:
        mark = ""
        if line.rule is not None:
            mark = f"  [{line.rule.verification.value}]"
        typer.echo(f"  - {line.label}: база {line.base} → {line.amount}{mark}")


def render(result: RouteResult) -> str:
    """Текстовый отчёт по маршруту."""
    kind = "деньги" if result.payout is Payout.CASH else "Steam Wallet (не выводится)"
    out = [
        f"Маршрут: {result.buy_market} → {result.sell_market} ({kind})",
        f"Цена покупки: {result.buy_price}",
        f"Цена продажи: {result.sell_price}",
        "",
        f"Полная стоимость покупки: {result.total_cost}",
        f"Чистая выручка:           {result.net_proceeds}",
        f"Чистая прибыль:           {result.profit}",
        f"ROI:                      {_pct(result.roi)}",
        f"Безубыточная цена продажи: {result.breakeven_sell or '—'}",
        f"Запас прочности:          {_pct(result.safety_margin)} падения цены продажи",
        f"Капитал заморожен минимум: {result.lock_days} дн.",
        "",
        "Формула: прибыль = чистая выручка − полная стоимость; ROI = прибыль / стоимость.",
    ]
    return "\n".join(out)


@app.command()
def fees(
    buy_market: Annotated[str, typer.Option("--buy-market", help="Площадка покупки")],
    buy: Annotated[str, typer.Option(help="Цена покупки в валюте площадки, например 100.00")],
    sell_market: Annotated[str, typer.Option("--sell-market", help="Площадка продажи")],
    sell: Annotated[str, typer.Option(help="Цена продажи в валюте площадки")],
    usd_per_eur: Annotated[
        str | None, typer.Option("--usd-per-eur", help="Курс: сколько USD за 1 EUR (ЕЦБ)")
    ] = None,
    deposit: Annotated[
        str, typer.Option(help="Способ пополнения площадки покупки или 'balance'")
    ] = BALANCE,
    withdraw: Annotated[str | None, typer.Option(help="Способ вывода с площадки продажи")] = None,
    target_profit: Annotated[
        str | None, typer.Option("--target-profit", help="Целевая прибыль в EUR")
    ] = None,
    target_roi: Annotated[
        str | None, typer.Option("--target-roi", help="Целевой ROI, например 0.10")
    ] = None,
) -> None:
    """Считает комиссии, прибыль, ROI и безубыточность для одного маршрута."""
    settings = Settings()
    schedule = FeeSchedule.load(settings.fee_rules_path)
    buy_ccy = schedule.market(buy_market).currency
    sell_ccy = schedule.market(sell_market).currency
    rates: dict[Currency, Decimal] = {}
    if usd_per_eur is not None:
        rates[Currency.USD] = _decimal(usd_per_eur, "--usd-per-eur")
    elif Currency.USD in (buy_ccy, sell_ccy):
        raise typer.BadParameter("Маршрут с USD: укажи курс --usd-per-eur")
    fx = FxRates(rates=rates, spread=settings.fx_spread, source="cli")

    calc = RouteCalculator(schedule, fx, buy_market, sell_market, deposit, withdraw)
    buy_price = Money.of(_decimal(buy, "--buy"), buy_ccy)
    sell_price = Money.of(_decimal(sell, "--sell"), sell_ccy)
    result = calc.evaluate(buy_price, sell_price)

    typer.echo(render(result))
    typer.echo("")
    _print_lines("Расходы при покупке:", result.buy_lines)
    _print_lines("Расходы при продаже и выводе:", result.sell_lines)

    if target_profit is not None:
        target = Money.of(_decimal(target_profit, "--target-profit"), Currency.EUR)
        max_buy = calc.max_buy_price(sell_price, target)
        typer.echo(f"\nМакс. цена покупки для прибыли {target}: {max_buy or 'недостижимо'}")
    if target_roi is not None:
        roi = _decimal(target_roi, "--target-roi")
        need = calc.required_sell_for_roi(buy_price, roi)
        typer.echo(f"Нужная цена продажи для ROI {_pct(roi)}: {need or 'недостижимо'}")

    if result.warnings:
        typer.echo("\nОговорки:")
        for w in result.warnings:
            typer.echo(f"  ! {w}")
    status = "полный" if result.complete else "НЕПОЛНЫЙ (есть неизвестные комиссии)"
    typer.echo(f"\nРасчёт: {status}. Это оценка, а не гарантированная прибыль.")


@app.command("rules")
def list_rules() -> None:
    """Показывает загруженные правила комиссий и их источники."""
    settings = Settings()
    schedule = FeeSchedule.load(settings.fee_rules_path)
    for market in schedule.marketplaces.values():
        typer.echo(f"{market.name} ({market.currency}, {market.payout.value})")
        for op in Operation:
            for rule in (
                r for r in schedule.rules if r.marketplace == market.name and r.operation is op
            ):
                method = f" [{rule.method}]" if rule.method else ""
                typer.echo(
                    f"  {op.value}{method}: {rule.describe()}  "
                    f"({rule.verification.value}, {rule.verified_at or 'дата не указана'})"
                )
