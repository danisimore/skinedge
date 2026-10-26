"""Командная строка SkinEdge."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer

from skinedge.config import Settings
from skinedge.fees.models import Operation
from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.finance.route import BALANCE, FeeLine, RouteCalculator, RouteResult
from skinedge.fx import FxRates
from skinedge.money import Currency, Money
from skinedge.quotes import Quote
from skinedge.sources.base import SourceError
from skinedge.sources.dmarket import DMarketSigner, DMarketSource
from skinedge.sources.skinport import SkinportSource
from skinedge.storage import Storage

app = typer.Typer(help="SkinEdge: анализ арбитража игровых предметов. Только анализ, без сделок.")


@app.callback()
def main() -> None:
    """SkinEdge CLI."""
    # В Windows вывод в файл или pipe по умолчанию идёт в cp1252 и падает на кириллице.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


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
    if not result.complete:
        status = "НЕПОЛНЫЙ: есть неизвестные комиссии"
    elif result.confirmed:
        status = "полный, все комиссии подтверждены"
    else:
        status = "все комиссии известны, но не все подтверждены официально или в аккаунте"
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


def _age(quote: Quote, now: datetime) -> str:
    minutes = int(quote.age(now).total_seconds() // 60)
    return f"{minutes} мин назад"


def _dmarket_source(settings: Settings) -> DMarketSource:
    if not settings.dmarket_public_key or not settings.dmarket_secret_key:
        typer.echo(
            "Нет ключей DMarket: заполни SKINEDGE_DMARKET_PUBLIC_KEY и "
            "SKINEDGE_DMARKET_SECRET_KEY в .env",
            err=True,
        )
        raise typer.Exit(1)
    try:
        signer = DMarketSigner(
            settings.dmarket_public_key, settings.dmarket_secret_key.get_secret_value()
        )
    except ValueError as exc:
        typer.echo(f"Ключи DMarket не подходят: {exc}", err=True)
        raise typer.Exit(1) from exc
    return DMarketSource(signer)


@app.command()
def fetch(
    source: Annotated[str, typer.Argument(help="Источник: skinport или dmarket")] = "skinport",
    app_id: Annotated[int, typer.Option("--app-id", help="Steam app_id (730 = CS2)")] = 730,
    sales: Annotated[bool, typer.Option(help="Skinport: загрузить статистику продаж")] = True,
    limit: Annotated[
        int, typer.Option(help="DMarket: сколько самых ликвидных предметов Skinport проверить")
    ] = 200,
    min_price: Annotated[str, typer.Option("--min-price", help="DMarket: мин. цена, EUR")] = "1",
    max_price: Annotated[str, typer.Option("--max-price", help="DMarket: макс. цена, EUR")] = "500",
    show: Annotated[int, typer.Option(help="Сколько примеров показать")] = 5,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Загружает реальные цены с площадки и сохраняет их в историю."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    for noisy in ("httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings()
    storage = Storage(settings.db_path)
    try:
        if source == "skinport":
            _fetch_skinport(storage, app_id, sales, show)
        elif source == "dmarket":
            names = storage.liquid_names(
                "skinport",
                limit,
                _decimal(min_price, "--min-price"),
                _decimal(max_price, "--max-price"),
            )
            if not names:
                typer.echo("Сначала загрузи Skinport: skinedge fetch skinport", err=True)
                raise typer.Exit(1)
            _fetch_dmarket(storage, _dmarket_source(settings), names, app_id, show)
        else:
            raise typer.BadParameter("Источник: skinport или dmarket")
    except SourceError as exc:
        typer.echo(f"Ошибка источника: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        storage.close()
    typer.echo(f"База: {settings.db_path}")


def _fetch_skinport(storage: Storage, app_id: int, sales: bool, show: int) -> None:
    src = SkinportSource()
    quotes = src.fetch_quotes(app_id)
    n_quotes = storage.save_quotes(quotes)
    n_sales = storage.save_sales(src.fetch_sales(app_id)) if sales else 0

    listed = [q for q in quotes if q.min_price is not None and q.quantity > 0]
    now = datetime.now(UTC)
    typer.echo(f"Skinport, app_id={app_id}: {n_quotes} предметов, из них с лотами {len(listed)}")
    typer.echo(f"Статистика продаж: {n_sales} записей")
    if listed:
        newest = max(q.source_updated_at or q.fetched_at for q in listed)
        typer.echo(f"Самое свежее обновление на площадке: {newest:%Y-%m-%d %H:%M} UTC")
    top = sorted(listed, key=lambda q: q.quantity, reverse=True)[:show]
    if top:
        typer.echo("\nСамые ликвидные по числу лотов:")
    for q in top:
        typer.echo(
            f"  {q.market_hash_name}: от {q.min_price} {q.currency.value}, "
            f"лотов {q.quantity}, обновлено {_age(q, now)}"
        )


def _fetch_dmarket(
    storage: Storage, src: DMarketSource, names: list[str], app_id: int, show: int
) -> None:
    quotes = src.fetch_best_prices(names, app_id)
    storage.save_quotes(quotes)
    with_ask = [q for q in quotes if q.min_price is not None]
    with_bid = [q for q in quotes if q.best_bid is not None]
    typer.echo(
        f"DMarket, app_id={app_id}: запрошено {len(names)}, получено {len(quotes)}, "
        f"с предложениями {len(with_ask)}, с заявками {len(with_bid)}"
    )
    for offer_type in ("dmarket", "p2p"):
        try:
            rule = src.fetch_sell_fees(app_id, offer_type)[0]
            typer.echo(f"Комиссия продавца ({offer_type}) из API: {rule.describe()}")
        except SourceError as exc:
            typer.echo(f"Комиссия продавца ({offer_type}): не получена ({exc})")
    for q in sorted(with_ask, key=lambda q: q.quantity, reverse=True)[:show]:
        typer.echo(
            f"  {q.market_hash_name}: ask {q.min_price} USD ({q.quantity} шт.), "
            f"bid {q.best_bid} USD ({q.bid_quantity} шт.)"
        )


@app.command()
def sales(
    source: Annotated[str, typer.Argument(help="Площадка: dmarket")],
    name: Annotated[str, typer.Argument(help="Точное название предмета")],
    app_id: Annotated[int, typer.Option("--app-id")] = 730,
) -> None:
    """Последние продажи предмета на площадке (DMarket: до 20 сделок)."""
    if source != "dmarket":
        raise typer.BadParameter("Пока поддерживается только dmarket")
    src = _dmarket_source(Settings())
    try:
        items = src.fetch_last_sales(name, app_id)
    except SourceError as exc:
        typer.echo(f"Ошибка источника: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not items:
        typer.echo("Продаж не найдено")
    for s in items:
        extra = []
        if s.float_value is not None:
            extra.append(f"float {s.float_value}")
        if s.phase:
            extra.append(s.phase)
        tail = f" ({', '.join(extra)})" if extra else ""
        typer.echo(f"  {s.sold_at:%Y-%m-%d %H:%M} UTC  {s.price} USD  {s.tx_type or ''}{tail}")
    if items:
        prices = sorted(x.price for x in items)
        mid = len(prices) // 2
        median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
        typer.echo(f"Медиана последних {len(prices)} продаж: {median} USD")


@app.command()
def item(
    name: Annotated[str, typer.Argument(help="Точное или частичное название предмета")],
    max_age_min: Annotated[int, typer.Option("--max-age", help="Допустимый возраст, мин")] = 15,
) -> None:
    """Показывает последние сохранённые цены и продажи предмета."""
    settings = Settings()
    storage = Storage(settings.db_path)
    try:
        quotes = storage.latest_quotes(name)
        if not quotes:
            matches = storage.search_names(name)
            if len(matches) == 1:
                name = matches[0]
                quotes = storage.latest_quotes(name)
            elif matches:
                typer.echo("Найдено несколько предметов, уточни название:")
                for m in matches:
                    typer.echo(f"  {m}")
                return
        if not quotes:
            typer.echo("Нет данных. Сначала выполни: skinedge fetch skinport")
            raise typer.Exit(1)
        now = datetime.now(UTC)
        typer.echo(name)
        for q in quotes:
            stale = " УСТАРЕЛО" if q.is_stale(now, timedelta(minutes=max_age_min)) else ""
            price = f"{q.min_price} {q.currency.value}" if q.min_price is not None else "нет лотов"
            bid = ""
            if q.best_bid is not None:
                bid = f", лучшая заявка {q.best_bid} {q.currency.value} ({q.bid_quantity} шт.)"
            typer.echo(
                f"  {q.source}: от {price}, лотов {q.quantity}{bid}, "
                f"обновлено {_age(q, now)}{stale} ({q.kind.value})"
            )
            if q.url:
                typer.echo(f"    {q.url}")
            for st in storage.latest_sales(name, q.source):
                version = f" [{st.version}]" if st.version else ""
                if st.volume:
                    typer.echo(
                        f"    продажи {st.window.value}{version}: {st.volume} шт., "
                        f"медиана {st.median}, мин {st.min}, макс {st.max}"
                    )
                else:
                    typer.echo(f"    продажи {st.window.value}{version}: нет")
    finally:
        storage.close()
