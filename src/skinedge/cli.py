"""Командная строка SkinEdge."""

from __future__ import annotations

import logging
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

import typer

from skinedge.config import Settings
from skinedge.fees.models import FeeRule, Operation
from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.finance.route import BALANCE, FeeLine, RouteCalculator, RouteResult
from skinedge.fx import FxRates
from skinedge.games import GAMES, parse_games
from skinedge.games import title as game_title
from skinedge.money import Currency, Money
from skinedge.notify.telegram import TelegramClient, TelegramError
from skinedge.paper import CHECKPOINTS, build_report
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote
from skinedge.runner import MIN_EVERY, AlreadyRunning, Runner, RunOptions
from skinedge.scanner import Opportunity, ScanConfig
from skinedge.sources import ecb
from skinedge.sources.base import SourceError
from skinedge.sources.csfloat import CSFloatSource
from skinedge.sources.dmarket import DMarketSigner, DMarketSource
from skinedge.sources.lisskins import LisSkinsSource
from skinedge.sources.marketcsgo import MarketCSGOSource
from skinedge.sources.skinport import SkinportSource
from skinedge.storage import Storage
from skinedge.tuning import load as load_tuning
from skinedge.ui.server import App as UiApp
from skinedge.ui.server import Rates, serve

app = typer.Typer(help="SkinEdge: анализ арбитража игровых предметов. Только анализ, без сделок.")


@app.callback()
def main() -> None:
    """SkinEdge CLI."""
    # В Windows вывод в файл или pipe по умолчанию идёт в cp1252 и падает на кириллице.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _quiet_http() -> None:
    # httpx печатает адреса запросов, а в адресе Telegram есть токен бота.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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


def _schedule(settings: Settings) -> FeeSchedule:
    """Правила комиссий с учётом выбранного способа вывода DMarket."""
    schedule = FeeSchedule.load(settings.fee_rules_path)
    if settings.dmarket_withdraw:
        try:
            schedule = schedule.with_withdraw("dmarket", settings.dmarket_withdraw)
        except ValueError as exc:
            typer.echo(f"SKINEDGE_DMARKET_WITHDRAW: {exc}", err=True)
            raise typer.Exit(1) from exc
    return schedule


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
    schedule = _schedule(settings)
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
    schedule = _schedule(settings)
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
    _quiet_http()
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
) -> FeeRule | None:
    """Загружает цены DMarket. Возвращает комиссию продавца для обычных лотов."""
    quotes = src.fetch_best_prices(names, app_id)
    storage.save_quotes(quotes)
    with_ask = [q for q in quotes if q.min_price is not None]
    with_bid = [q for q in quotes if q.best_bid is not None]
    typer.echo(
        f"DMarket, app_id={app_id}: запрошено {len(names)}, получено {len(quotes)}, "
        f"с предложениями {len(with_ask)}, с заявками {len(with_bid)}"
    )
    fee: FeeRule | None = None
    for offer_type in ("dmarket", "p2p"):
        try:
            rule = src.fetch_sell_fees(app_id, offer_type)[0]
            typer.echo(f"Комиссия продавца ({offer_type}) из API: {rule.describe()}")
            if offer_type == "dmarket":
                fee = rule
        except SourceError as exc:
            typer.echo(f"Комиссия продавца ({offer_type}): не получена ({exc})")
    for q in sorted(with_ask, key=lambda q: q.quantity, reverse=True)[:show]:
        typer.echo(
            f"  {q.market_hash_name}: ask {q.min_price} USD ({q.quantity} шт.), "
            f"bid {q.best_bid} USD ({q.bid_quantity} шт.)"
        )
    return fee


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


def _fx(settings: Settings, usd_per_eur: str | None) -> FxRates:
    if usd_per_eur is not None:
        return _manual_fx(settings, usd_per_eur)
    try:
        return ecb.fetch_rates(settings.fx_spread)
    except SourceError as exc:
        typer.echo(f"Не удалось получить курс ЕЦБ ({exc}). Укажи --usd-per-eur", err=True)
        raise typer.Exit(1) from exc


def _manual_fx(settings: Settings, usd_per_eur: str) -> FxRates:
    rate = _decimal(usd_per_eur, "--usd-per-eur")
    return FxRates(rates={Currency.USD: rate}, spread=settings.fx_spread, source="cli")


def render_opportunity(n: int, opp: Opportunity) -> str:
    """Короткий отчёт по возможности."""
    r = opp.result
    lines = [
        f"{n}. {opp.name} ({game_title(opp.buy_quote.app_id)})",
        f"   {r.buy_market} → {r.sell_market}: купить {r.buy_price}, "
        f"продать ~{r.sell_price} ({opp.sell.basis})",
        f"   стоимость {r.total_cost}, выручка {r.net_proceeds}, прибыль {r.profit}, "
        f"ROI {_pct(r.roi)}, запас {_pct(r.safety_margin)}, капитал заморожен {r.lock_days} дн.",
    ]
    lines += [f"   ! {f}" for f in opp.flags]
    lines += [f"   {u}" for u in (opp.buy_url, opp.sell_url) if u]
    return "\n".join(lines)


def _telegram(settings: Settings) -> TelegramClient:
    if settings.telegram_bot_token is None:
        typer.echo(
            "Нет токена бота: заполни TG_BOT_TOKEN (или SKINEDGE_TELEGRAM_BOT_TOKEN)", err=True
        )
        raise typer.Exit(1)
    try:
        return TelegramClient(settings.telegram_bot_token.get_secret_value())
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _telegram_target(settings: Settings) -> tuple[TelegramClient, str] | None:
    """Бот и чат для уведомлений или ``None``, если Telegram не настроен."""
    if settings.telegram_bot_token is None or not settings.telegram_chat_id:
        return None
    try:
        bot = TelegramClient(settings.telegram_bot_token.get_secret_value())
    except ValueError as exc:
        typer.echo(f"Telegram: {exc}", err=True)
        return None
    return bot, settings.telegram_chat_id


def _scan_options(
    games: str,
    limit: int,
    min_price: str,
    max_price: str,
    min_profit: str,
    min_roi: str,
    max_age: int,
    verify: int,
    fetch: bool,
    paper: bool,
    notify: bool,
    csfloat: int,
) -> ScanOptions:
    try:
        game_ids = parse_games(games)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--games") from exc
    return ScanOptions(
        games=game_ids,
        limit=limit,
        min_price=_decimal(min_price, "--min-price"),
        max_price=_decimal(max_price, "--max-price"),
        config=ScanConfig(
            min_profit=_decimal(min_profit, "--min-profit"),
            min_roi=_decimal(min_roi, "--min-roi"),
            max_age=timedelta(minutes=max_age),
        ),
        verify=verify,
        fetch=fetch,
        paper=paper,
        notify=notify,
        csfloat=csfloat,
    )


Games = Annotated[str, typer.Option("--games", help=f"Игры через запятую: {', '.join(GAMES)}")]
ALL_GAMES = ",".join(GAMES)
Limit = Annotated[
    int, typer.Option(help="Сколько ликвидных предметов Skinport сравнивать в каждой игре")
]
MinPrice = Annotated[str, typer.Option("--min-price", help="Мин. цена Skinport, EUR")]
MaxPrice = Annotated[str, typer.Option("--max-price", help="Макс. цена Skinport, EUR")]
MinProfit = Annotated[str, typer.Option("--min-profit", help="Мин. прибыль, EUR")]
MinRoi = Annotated[str, typer.Option("--min-roi", help="Мин. ROI, 0.05 = 5%")]
Verify = Annotated[
    int,
    typer.Option(
        help="Сколько кандидатов проверить по реальным сделкам DMarket и Market.CSGO "
        "(для каждой площадки покупки)"
    ),
]
UsdPerEur = Annotated[str | None, typer.Option("--usd-per-eur", help="Курс вручную вместо ЕЦБ")]
Paper = Annotated[bool, typer.Option("--paper/--no-paper", help="Записывать виртуальные сделки")]
Notify = Annotated[
    bool, typer.Option("--notify/--no-notify", help="Отправлять новые возможности в Telegram")
]
Verbose = Annotated[bool, typer.Option("--verbose", "-v")]
CSFloatItems = Annotated[
    int,
    typer.Option(
        "--csfloat",
        help="Сколько предметов проверить на CSFloat за проход (0 — не проверять). "
        "Лимит CSFloat API: 200 запросов в час",
    ),
]
MarketCSGO = Annotated[
    bool,
    typer.Option(
        "--marketcsgo/--no-marketcsgo",
        help="Сравнивать с Market.CSGO (публичные цены и история продаж, ключ не нужен)",
    ),
]
LisSkins = Annotated[
    bool,
    typer.Option(
        "--lisskins/--no-lisskins",
        help="Покупка на Lis-Skins (публичный прайс-лист, ключ не нужен)",
    ),
]


def _csfloat_source(settings: Settings) -> CSFloatSource | None:
    if settings.csfloat_api_key is None:
        typer.echo("Нет ключа CSFloat (SKINEDGE_CSFLOAT_KEY в .env): CSFloat не проверяется")
        return None
    return CSFloatSource(settings.csfloat_api_key.get_secret_value())


@app.command()
def scan(
    fetch_first: Annotated[
        bool, typer.Option("--fetch/--no-fetch", help="Сначала загрузить свежие цены")
    ] = True,
    games: Games = ALL_GAMES,
    limit: Limit = 300,
    min_price: MinPrice = "1",
    max_price: MaxPrice = "500",
    min_profit: MinProfit = "0.50",
    min_roi: MinRoi = "0.05",
    max_age: Annotated[int, typer.Option("--max-age", help="Макс. возраст цен, мин")] = 30,
    verify: Verify = 40,
    usd_per_eur: UsdPerEur = None,
    top: Annotated[int, typer.Option(help="Сколько возможностей показать")] = 15,
    paper: Paper = True,
    notify: Notify = True,
    csfloat: CSFloatItems = 40,
    marketcsgo: MarketCSGO = True,
    lisskins: LisSkins = True,
    verbose: Verbose = False,
) -> None:
    """Ищет выгодные маршруты между Skinport, DMarket и Market.CSGO.

    Покупка также на Lis-Skins и на CSFloat (только CS2).

    Только анализ, без сделок.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    _quiet_http()
    opts = _scan_options(
        games,
        limit,
        min_price,
        max_price,
        min_profit,
        min_roi,
        max_age,
        verify,
        fetch_first,
        paper,
        notify,
        csfloat,
    )
    settings = Settings()
    schedule = _schedule(settings)
    fx = _fx(settings, usd_per_eur)
    typer.echo(f"Курс: 1 EUR = {fx.rates[Currency.USD]} USD ({fx.source}), спред {_pct(fx.spread)}")
    storage = Storage(settings.db_path)
    tuning = load_tuning(storage)
    if tuning is not None:
        opts = tuning.apply(opts)
        typer.echo(f"Настройки из интерфейса: {tuning.describe()}")
    try:
        pipeline = Pipeline(
            storage,
            schedule,
            fx,
            skinport=SkinportSource() if fetch_first else None,
            dmarket=_dmarket_source(settings) if fetch_first else None,
            csfloat=_csfloat_source(settings) if fetch_first and csfloat > 0 else None,
            marketcsgo=MarketCSGOSource() if fetch_first and marketcsgo else None,
            lisskins=LisSkinsSource() if fetch_first and lisskins else None,
            telegram=_telegram_target(settings) if notify else None,
            out=typer.echo,
        )
        try:
            result = pipeline.scan(opts)
        except SourceError as exc:
            typer.echo(f"Ошибка источника: {exc}", err=True)
            raise typer.Exit(1) from exc
        found = result.found
        typer.echo(
            f"\nСравнено предметов на Skinport и DMarket: {result.pairs}. "
            f"Возможностей: {len(found)} (полных расчётов {sum(o.result.complete for o in found)})"
        )
        if result.pairs == 0:
            typer.echo("Нет свежих цен с обеих площадок. Запусти без --no-fetch.")
        for n, opp in enumerate(found[:top], 1):
            typer.echo("")
            typer.echo(render_opportunity(n, opp))
        if paper and found:
            recorded = pipeline.record(result)
            typer.echo(
                f"\nЗаписано виртуальных сделок: {recorded} "
                "(только по стандартным порогам, повторы за сутки пропущены)"
            )
        if notify:
            pipeline.notify(found, result.at)
    finally:
        storage.close()
    typer.echo(
        "\nЭто оценка по текущим ценам, а не гарантированная прибыль. Сделки не совершаются."
    )


def _run_logging(path: Path, verbose: bool) -> logging.Logger:
    """Лог фонового запуска: консоль и файл с ротацией (5 файлов по 1 МБ)."""
    _quiet_http()
    logger = logging.getLogger("skinedge")
    _close_handlers(logger)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    path.parent.mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console)
    logger.addHandler(file)
    # Ожидание лимита запросов DMarket (раз в секунду) засоряет лог.
    logging.getLogger("skinedge.sources").setLevel(logging.DEBUG if verbose else logging.WARNING)
    return logger


def _close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@app.command("run")
def run_command(
    every: Annotated[int, typer.Option(help="Интервал между проходами, мин (не меньше 5)")] = 15,
    iterations: Annotated[
        int, typer.Option(help="Сколько проходов сделать (0 = пока не остановят)")
    ] = 0,
    games: Games = ALL_GAMES,
    limit: Limit = 300,
    min_price: MinPrice = "1",
    max_price: MaxPrice = "500",
    min_profit: MinProfit = "0.50",
    min_roi: MinRoi = "0.05",
    verify: Verify = 40,
    usd_per_eur: UsdPerEur = None,
    paper: Paper = True,
    notify: Notify = True,
    report: Annotated[
        bool, typer.Option("--report/--no-report", help="Ежедневный отчёт в Telegram")
    ] = True,
    report_hour: Annotated[
        int, typer.Option("--report-hour", min=0, max=23, help="Час отчёта, местное время")
    ] = 10,
    force: Annotated[
        bool, typer.Option(help="Запустить, даже если другой запуск недавно оставил отметку")
    ] = False,
    csfloat: CSFloatItems = 40,
    marketcsgo: MarketCSGO = True,
    lisskins: LisSkins = True,
    verbose: Verbose = False,
) -> None:
    """Сканирует по расписанию, пока не остановят (Ctrl+C). Только анализ, без сделок."""
    if every < MIN_EVERY.seconds // 60:
        raise typer.BadParameter(f"--every: не меньше {MIN_EVERY.seconds // 60} минут")
    settings = Settings()
    logger = _run_logging(settings.log_path, verbose)
    schedule = _schedule(settings)
    telegram = _telegram_target(settings)
    if telegram is None:
        logger.warning("Telegram не настроен: уведомлений, отчёта и тревог не будет")
    opts = _scan_options(
        games,
        limit,
        min_price,
        max_price,
        min_profit,
        min_roi,
        30,
        verify,
        True,
        paper,
        notify and telegram is not None,
        csfloat,
    )
    manual = _manual_fx(settings, usd_per_eur) if usd_per_eur is not None else None

    def fx_source() -> FxRates:
        return manual or ecb.fetch_rates(settings.fx_spread)

    storage = Storage(settings.db_path)
    pipeline = Pipeline(
        storage,
        schedule,
        FxRates(rates={}, spread=settings.fx_spread),
        skinport=SkinportSource(),
        dmarket=_dmarket_source(settings),
        csfloat=_csfloat_source(settings) if csfloat > 0 else None,
        marketcsgo=MarketCSGOSource() if marketcsgo else None,
        lisskins=LisSkinsSource() if lisskins else None,
        telegram=telegram,
        out=logger.info,
    )
    runner = Runner(
        pipeline,
        opts,
        RunOptions(
            every=timedelta(minutes=every),
            iterations=iterations,
            report_hour=report_hour if report else None,
            force=force,
        ),
        fx_source,
        settings.fx_spread,
    )
    logger.info(
        "SkinEdge: проход каждые %d мин, лог %s. Только анализ, сделки не совершаются. "
        "Остановить: Ctrl+C",
        every,
        settings.log_path,
    )
    if len(opts.games) >= 3:
        logger.info(
            "%s сканируется каждый проход, %s — по очереди, по одной за проход "
            "(Skinport блокирует соединение, если запросов в час слишком много)",
            game_title(opts.games[0]),
            " и ".join(game_title(g) for g in opts.games[1:]),
        )
    try:
        done = runner.run()
        logger.info("Выполнено проходов: %d", done)
    except AlreadyRunning as exc:
        logger.error(
            "Не запускаю: %s. Если он закрыт аварийно, подожди 20 минут или добавь --force", exc
        )
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        logger.info("Остановлено")
    finally:
        storage.close()
        _close_handlers(logger)


telegram_app = typer.Typer(help="Уведомления в Telegram.")
app.add_typer(telegram_app, name="telegram")


@telegram_app.command("setup")
def telegram_setup(
    save: Annotated[bool, typer.Option(help="Записать найденный чат в .env")] = False,
    chat_id: Annotated[
        int | None, typer.Option("--chat-id", help="Какой чат выбрать, если их несколько")
    ] = None,
) -> None:
    """Находит чат, в котором ты написал боту /start, и показывает его id."""
    _quiet_http()
    settings = Settings()
    bot = _telegram(settings)
    if settings.telegram_chat_id:
        typer.echo(f"Чат уже задан: {settings.telegram_chat_id}")
    try:
        chats = bot.chats()
    except TelegramError as exc:
        typer.echo(f"Telegram: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not chats:
        typer.echo(
            "Боту пока никто не писал (или сообщения старше суток). "
            "Отправь боту /start и повтори команду."
        )
        raise typer.Exit(1)
    typer.echo("Чаты, в которых писали боту:")
    for c in chats:
        typer.echo(f"  {c.id}  {c.type}  {c.title}  последнее сообщение: {c.last_text[:30]!r}")
    if chat_id is not None:
        chosen = [c for c in chats if c.id == chat_id]
    else:
        chosen = chats if len(chats) == 1 else []
    if not chosen:
        typer.echo("Выбери чат: skinedge telegram setup --save --chat-id <id>")
        return
    if not save:
        typer.echo(f"Добавь в .env строку: SKINEDGE_TELEGRAM_CHAT_ID={chosen[0].id}")
        typer.echo("или запусти: skinedge telegram setup --save")
        return
    if settings.telegram_chat_id:
        typer.echo("В настройках уже есть чат, .env не меняю. Удали старую строку и повтори.")
        return
    env = Path(".env")
    prefix = "" if not env.exists() or env.read_bytes().endswith(b"\n") else "\n"
    with env.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}SKINEDGE_TELEGRAM_CHAT_ID={chosen[0].id}\n")
    typer.echo(f"Записал в .env: SKINEDGE_TELEGRAM_CHAT_ID={chosen[0].id}")


@telegram_app.command("test")
def telegram_test() -> None:
    """Отправляет тестовое сообщение в настроенный чат."""
    _quiet_http()
    settings = Settings()
    bot = _telegram(settings)
    if not settings.telegram_chat_id:
        typer.echo("Нет чата: запусти skinedge telegram setup --save", err=True)
        raise typer.Exit(1)
    try:
        bot.send(
            settings.telegram_chat_id,
            "✅ SkinEdge подключён. Сюда будут приходить новые арбитражные возможности. "
            "Бот только уведомляет и ничего не покупает и не продаёт.",
        )
    except TelegramError as exc:
        typer.echo(f"Telegram: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Тестовое сообщение отправлено")


@app.command("paper")
def paper_report(
    days: Annotated[int, typer.Option(help="За сколько последних дней")] = 30,
    show: Annotated[int, typer.Option(help="Сколько сделок показать подробно")] = 10,
) -> None:
    """Отчёт по виртуальным сделкам: что стало с сигналами через 24ч, 3д и 7д."""
    settings = Settings()
    schedule = _schedule(settings)
    cfg = ScanConfig()
    now = datetime.now(UTC)
    storage = Storage(settings.db_path)
    try:
        report = build_report(
            storage, schedule, settings.fx_spread, cfg, now, now - timedelta(days=days)
        )
    finally:
        storage.close()
    trades, results = report.trades, report.results
    if not trades:
        typer.echo("Виртуальных сделок нет. Они записываются командами skinedge scan и run.")
        return
    complete = sum(t.complete for t in trades)
    typer.echo(
        f"Сигналов за {days} дн.: {len(trades)} (полных расчётов {complete}), "
        f"средний ROI в момент сигнала {_pct(report.avg_signal_roi)}"
    )
    typer.echo("\nЧто показал бы рынок продажи позже (по тем же комиссиям и курсу):")
    for s in report.summaries:
        if s.evaluated == 0:
            typer.echo(f"  {s.label}: данных пока нет")
            continue
        typer.echo(
            f"  {s.label}: проверено {s.evaluated}, в плюсе {s.profitable}, "
            f"не продать уверенно {s.unsellable}, средний ROI {_pct(s.avg_roi)}, "
            f"сумма прибыли {s.total_profit} EUR"
        )
    typer.echo("\nПоследние сделки:")
    labels = [label for label, _ in CHECKPOINTS]
    for t, res in list(zip(trades, results, strict=True))[-show:]:
        marks = []
        for label in labels:
            c = res.get(label)
            if c is None:
                marks.append(f"{label} —")
            elif not c.sellable:
                marks.append(f"{label} не продать")
            else:
                marks.append(f"{label} {_pct(c.roi)}")
        typer.echo(
            f"  {t.created_at:%m-%d %H:%M} {t.market_hash_name} ({game_title(t.app_id)}): "
            f"{t.buy_market} → "
            f"{t.sell_market}, сигнал {_pct(t.roi)}; " + ", ".join(marks)
        )
    typer.echo(
        "\nГлавная точка: 7д, когда купленный предмет можно передать. "
        "Для контрольных точек нужны свежие цены: держи skinedge run включённым "
        "или запускай skinedge scan хотя бы раз в день."
    )


@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option(help="Порт на 127.0.0.1")] = 8765,
    browser: Annotated[
        bool, typer.Option("--browser/--no-browser", help="Сразу открыть в браузере")
    ] = True,
    verbose: Verbose = False,
) -> None:
    """Интерфейс в браузере: настройки сканера и журнал сделок (только на этом компьютере)."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    _quiet_http()
    settings = Settings()
    schedule = _schedule(settings)
    storage = Storage(settings.db_path, threads=True)
    rates = Rates(lambda: ecb.fetch_rates(settings.fx_spread), storage, lambda: datetime.now(UTC))
    ui = UiApp(storage, schedule, rates, settings.fx_spread)

    def ready(url: str) -> None:
        typer.echo(f"Интерфейс SkinEdge: {url}")
        typer.echo("Остановить: Ctrl+C или закрыть это окно. Сканер при этом не останавливается.")
        if browser:
            webbrowser.open(url)

    try:
        serve(ui, port, ready)
    except OSError as exc:
        typer.echo(
            f"Не удалось занять порт {port} ({exc}). Интерфейс уже открыт в другом окне? "
            f"Или запусти с другим портом: --port {port + 1}",
            err=True,
        )
        raise typer.Exit(1) from exc
    finally:
        storage.close()
