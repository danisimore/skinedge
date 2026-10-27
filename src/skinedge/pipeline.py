"""Один полный проход сканера: загрузка цен → проверка продаж → поиск → журнал → уведомление.

Используется и командой ``skinedge scan``, и фоновым запуском ``skinedge run``.
Источники, Telegram и часы передаются снаружи, поэтому проход тестируется без сети.

Чтобы база не росла на десятки мегабайт за каждый проход, сохраняются только
предметы, которые сканер реально сравнивает («вселенная»): самые ликвидные
предметы Skinport в заданном ценовом диапазоне и предметы открытых виртуальных
сделок, для которых нужны контрольные точки.

CSFloat отдаёт 200 запросов в час, по одному на предмет, поэтому за проход
проверяется только часть вселенной (``ScanOptions.csfloat``), по кругу.
Market.CSGO и Lis-Skins отдают весь прайс-лист игры одним файлом.

Продажу на DMarket и Market.CSGO сканер подтверждает реальными сделками: их
история загружается по одному предмету и только для кандидатов, которые выгодны
даже по предварительной цене (минимальный лот без статистики продаж).

Игры (CS2, Dota 2, Rust) сканируются по очереди и независимо: сбой одной игры
не останавливает остальные, проход считается неудачным, только если не
удалось ни одной.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from skinedge.fees.schedule import FeeSchedule
from skinedge.fx import FxRates
from skinedge.games import CS2, title
from skinedge.money import Currency
from skinedge.notify.message import format_digest
from skinedge.notify.telegram import TelegramClient, TelegramError
from skinedge.paper import record as paper_record
from skinedge.quotes import Quote, SalesStats, SalesWindow
from skinedge.scanner import Opportunity, ScanConfig, Scanner, stats_from_sales
from skinedge.sources.base import SourceError
from skinedge.sources.csfloat import CSFloatSource
from skinedge.sources.dmarket import DMarketSource
from skinedge.sources.lisskins import LisSkinsSource
from skinedge.sources.marketcsgo import MarketCSGOSource
from skinedge.sources.skinport import SkinportSource
from skinedge.storage import Storage

MARKETS = ("skinport", "dmarket", "csfloat", "marketcsgo", "lisskins")
# Маршруты «купить → продать». На CSFloat и Lis-Skins только покупаем: истории
# продаж в их API нет.
ROUTES = (
    ("skinport", "dmarket"),
    ("dmarket", "skinport"),
    ("csfloat", "skinport"),
    ("csfloat", "dmarket"),
    ("marketcsgo", "skinport"),
    ("marketcsgo", "dmarket"),
    ("skinport", "marketcsgo"),
    ("dmarket", "marketcsgo"),
    ("csfloat", "marketcsgo"),
    ("lisskins", "skinport"),
    ("lisskins", "dmarket"),
    ("lisskins", "marketcsgo"),
)
CSFLOAT_OFFSET = "csfloat.offset"
NOTIFY_COOLDOWN = timedelta(hours=24)
NOTIFY_MAX = 5
PAPER_HORIZON = timedelta(days=8)  # сделки, для которых ещё нужны контрольные точки
STATS_MAX_AGE = timedelta(hours=6)
# Файлы истории Market.CSGO обновляются примерно раз в час: чаще не перечитываем.
MARKETCSGO_STATS_TTL = timedelta(hours=1)
# Окна продаж, которые нужны сканеру: 7 дней для цены, 24 часа и 30 дней для тренда.
SALES_WINDOWS = frozenset({SalesWindow.H24, SalesWindow.D7, SalesWindow.D30})


@dataclass(frozen=True)
class ScanOptions:
    """Параметры прохода.

    Attributes:
        games: Steam app_id игр, по очереди.
        limit: Сколько самых ликвидных предметов Skinport сравнивать в каждой игре.
        min_price: Нижняя граница цены Skinport, EUR.
        max_price: Верхняя граница цены Skinport, EUR.
        config: Пороги сканера.
        verify: Сколько кандидатов на продажу в DMarket и в Market.CSGO проверять
            по сделкам (для каждой площадки покупки).
        fetch: Загружать свежие цены (иначе работать по сохранённым).
        paper: Записывать виртуальные сделки.
        notify: Отправлять уведомления.
        csfloat: Сколько предметов проверить на CSFloat за проход (0 — не проверять).
    """

    games: tuple[int, ...] = (CS2,)
    limit: int = 300
    min_price: Decimal = Decimal("1")
    max_price: Decimal = Decimal("500")
    config: ScanConfig = field(default_factory=ScanConfig)
    verify: int = 40
    fetch: bool = True
    paper: bool = True
    notify: bool = True
    csfloat: int = 40


@dataclass
class ScanResult:
    """Итог прохода.

    Attributes:
        at: Момент скана.
        found: Найденные возможности, лучшие первыми.
        pairs: Сколько предметов сравнено на Skinport и DMarket (во всех играх).
        recorded: Сколько виртуальных сделок записано.
        notified: Сколько возможностей отправлено в Telegram.
    """

    at: datetime
    found: list[Opportunity] = field(default_factory=list)
    pairs: int = 0
    recorded: int = 0
    notified: int = 0


def liquid(quotes: list[Quote], limit: int, min_price: Decimal, max_price: Decimal) -> list[str]:
    """Самые ликвидные предметы (по числу лотов) в ценовом диапазоне."""
    eligible = [
        q
        for q in quotes
        if q.min_price is not None and q.quantity > 0 and min_price <= q.min_price <= max_price
    ]
    eligible.sort(key=lambda q: q.quantity, reverse=True)
    return [q.market_hash_name for q in eligible[:limit]]


def trends(storage: Storage, since: datetime, app_id: int = CS2) -> dict[str, Decimal]:
    """Изменение цены на Skinport: медиана продаж за 24ч к медиане за 30д − 1."""
    day = storage.recent_sales("skinport", SalesWindow.H24, since, app_id)
    month = storage.recent_sales("skinport", SalesWindow.D30, since, app_id)
    out: dict[str, Decimal] = {}
    for name, d in day.items():
        m = month.get(name)
        if d.median is None or d.volume < 3 or m is None or not m.median:
            continue
        out[name] = d.median / m.median - 1
    return out


class Pipeline:
    """Проход сканера.

    Args:
        storage: База.
        schedule: Площадки и комиссии.
        fx: Курсы валют.
        skinport: Источник Skinport (нужен при ``fetch``).
        dmarket: Источник DMarket (нужен при ``fetch``).
        csfloat: Источник CSFloat; ``None`` — CSFloat не проверяется.
        marketcsgo: Источник Market.CSGO; ``None`` — Market.CSGO не проверяется.
        lisskins: Источник Lis-Skins; ``None`` — Lis-Skins не проверяется.
        telegram: Клиент Telegram и чат; ``None`` — уведомления не настроены.
        out: Куда писать ход работы (консоль или лог).
        clock: Текущее время (для тестов).
    """

    def __init__(
        self,
        storage: Storage,
        schedule: FeeSchedule,
        fx: FxRates,
        skinport: SkinportSource | None = None,
        dmarket: DMarketSource | None = None,
        csfloat: CSFloatSource | None = None,
        marketcsgo: MarketCSGOSource | None = None,
        lisskins: LisSkinsSource | None = None,
        telegram: tuple[TelegramClient, str] | None = None,
        out: Callable[[str], None] = print,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.storage = storage
        self.schedule = schedule
        self.fx = fx
        self.skinport = skinport
        self.dmarket = dmarket
        self.csfloat = csfloat
        self.marketcsgo = marketcsgo
        self.lisskins = lisskins
        self.telegram = telegram
        self.out = out
        self.clock = clock
        self._prefix = ""  # название игры в сообщениях, когда игр несколько
        # Статистика Skinport кешируется на час: по каждой игре помним, какие
        # предметы из последней загрузки уже записаны, чтобы не дублировать строки.
        self._sales_saved: dict[int, tuple[datetime, set[str]]] = {}

    def run(self, opts: ScanOptions) -> ScanResult:
        """Полный проход: скан, журнал виртуальных сделок, уведомление.

        Raises:
            SourceError: Если площадка не отдала цены.
        """
        result = self.scan(opts)
        if opts.paper:
            result.recorded = self.record(result)
        if opts.notify:
            result.notified = self.notify(result.found, result.at)
        return result

    def scan(self, opts: ScanOptions) -> ScanResult:
        """Загружает цены (если ``fetch``) и ищет возможности во всех играх.

        Ничего не отправляет. Сбой одной игры пишется в ``out``, остальные игры
        сканируются дальше.

        Raises:
            SourceError: Если не удалось просканировать ни одной игры.
        """
        found: list[Opportunity] = []
        pairs = 0
        errors: list[str] = []
        for app_id in opts.games:
            self._prefix = f"{title(app_id)} · " if len(opts.games) > 1 else ""
            try:
                game_found, game_pairs = self._scan_game(opts, app_id)
            except SourceError as exc:
                if len(opts.games) == 1:
                    raise
                errors.append(f"{title(app_id)}: {exc}")
                self._say(f"игра пропущена: {exc}")
                continue
            finally:
                self._prefix = ""
            found += game_found
            pairs += game_pairs
        if errors and len(errors) == len(opts.games):
            raise SourceError("; ".join(errors))
        found.sort(key=lambda o: (not o.result.complete, -o.result.roi))
        return ScanResult(at=self.clock(), found=found, pairs=pairs)

    def _scan_game(self, opts: ScanOptions, app_id: int) -> tuple[list[Opportunity], int]:
        cfg = opts.config
        schedule = self.schedule
        if opts.fetch:
            schedule = self._fetch(opts, app_id)

        now = self.clock()
        quotes = {m: self.storage.recent_quotes(m, now - cfg.max_age, app_id) for m in MARKETS}
        scanner = Scanner(schedule, self.fx, cfg)
        if opts.fetch:
            # Продажу на DMarket подтверждаем реальными сделками: минимальный лот
            # без сделок может стоять месяцами и не говорит о спросе.
            self._verify_dmarket(
                self._to_verify(scanner, quotes, "dmarket", now, app_id, opts.verify), app_id, now
            )
            if self.marketcsgo is not None:
                self._verify_marketcsgo(
                    self._to_verify(scanner, quotes, "marketcsgo", now, app_id, opts.verify),
                    app_id,
                    now,
                )

        stats_since = now - STATS_MAX_AGE
        sales = {
            m: self.storage.recent_sales(m, SalesWindow.D7, stats_since, app_id) for m in MARKETS
        }
        changes = trends(self.storage, stats_since, app_id)
        found: list[Opportunity] = []
        for buy, sell in ROUTES:
            found += scanner.scan(buy, sell, quotes[buy], quotes[sell], sales[sell], now, changes)
        return found, len(quotes["skinport"].keys() & quotes["dmarket"].keys())

    def record(self, result: ScanResult) -> int:
        """Записывает виртуальные сделки по стандартным порогам. Возвращает их число."""
        if not result.found:
            return 0
        return paper_record(self.storage, result.found, self.fx, result.at, ScanConfig())

    # ---------- загрузка ----------

    def _say(self, text: str) -> None:
        self.out(self._prefix + text)

    def _universe_extra(self, now: datetime, app_id: int) -> set[str]:
        trades = self.storage.paper_trades(now - PAPER_HORIZON)
        return {t.market_hash_name for t in trades if t.app_id == app_id}

    def _fetch(self, opts: ScanOptions, app_id: int) -> FeeSchedule:
        if self.skinport is None or self.dmarket is None:
            raise SourceError("Для загрузки цен нужны источники Skinport и DMarket")
        now = self.clock()
        sp_quotes = self.skinport.fetch_quotes(app_id)
        names = liquid(sp_quotes, opts.limit, opts.min_price, opts.max_price)
        universe = set(names) | self._universe_extra(now, app_id)
        self.storage.save_quotes(q for q in sp_quotes if q.market_hash_name in universe)
        sp_sales = self.skinport.fetch_sales(app_id)
        self._save_skinport_sales(app_id, sp_sales, universe)
        self._say(
            f"Skinport: {len(sp_quotes)} предметов, сравниваю {len(names)} самых ликвидных "
            f"от {opts.min_price} до {opts.max_price} EUR"
        )

        dm_names = list(dict.fromkeys([*names, *sorted(universe - set(names))]))
        dm_quotes = self.dmarket.fetch_best_prices(dm_names, app_id)
        self.storage.save_quotes(dm_quotes)
        self._say(
            f"DMarket: запрошено {len(dm_names)}, с предложениями "
            f"{sum(q.min_price is not None for q in dm_quotes)}"
        )
        schedule = self.schedule
        try:
            fee = self.dmarket.fetch_sell_fees(app_id, "dmarket")[0]
            self._say(f"Комиссия продавца DMarket из API: {fee.describe()}")
            schedule = schedule.with_rules([fee], replace_marketplace="dmarket")
        except SourceError as exc:
            self._say(f"Комиссия продавца DMarket не получена ({exc}), беру из правил")
        self._fetch_price_list(self.marketcsgo, "Market.CSGO", dm_names, app_id)
        self._fetch_price_list(self.lisskins, "Lis-Skins", dm_names, app_id)
        if app_id == CS2:  # CSFloat торгует только CS2
            self._fetch_csfloat(names, sp_sales, opts)
        return schedule

    def _fetch_price_list(
        self,
        source: MarketCSGOSource | LisSkinsSource | None,
        label: str,
        names: list[str],
        app_id: int,
    ) -> None:
        """Цены площадки, которая отдаёт прайс-лист игры одним файлом.

        Сбой площадки не останавливает проход.
        """
        if source is None:
            return
        try:
            quotes = source.fetch_best_prices(names, app_id)
        except SourceError as exc:
            self._say(f"{label} пропущен: {exc}")
            return
        self.storage.save_quotes(quotes)
        self._say(
            f"{label}: запрошено {len(names)}, с лотами "
            f"{sum(q.min_price is not None for q in quotes)}"
        )

    def _save_skinport_sales(
        self, app_id: int, sales: list[SalesStats], universe: set[str]
    ) -> None:
        if not sales:
            return
        stamp = sales[0].fetched_at
        saved_at, saved = self._sales_saved.get(app_id, (None, set()))
        if saved_at != stamp:
            saved = set()
        new = universe - saved
        self.storage.save_sales(s for s in sales if _keep_sales(s, new))
        self._sales_saved[app_id] = (stamp, saved | new)

    def _fetch_csfloat(self, names: list[str], sales: list[SalesStats], opts: ScanOptions) -> None:
        """Проверяет на CSFloat очередную порцию предметов CS2, у которых есть спрос.

        Сбой CSFloat не останавливает проход: остальные площадки важнее.
        """
        if self.csfloat is None or opts.csfloat <= 0:
            return
        sold = {
            s.market_hash_name
            for s in sales
            if s.window is SalesWindow.D7
            and s.version is None
            and s.volume >= opts.config.min_sales_7d
        }
        pool = [n for n in names if n in sold]
        if not pool:
            return
        start = int(self.storage.get_meta(CSFLOAT_OFFSET) or 0) % len(pool)
        batch = (pool[start:] + pool[:start])[: opts.csfloat]
        try:
            quotes = self.csfloat.fetch_lowest(batch, CS2)
        except SourceError as exc:
            self._say(f"CSFloat пропущен: {exc}")
            return
        self.storage.save_quotes(quotes)
        self.storage.set_meta(CSFLOAT_OFFSET, str(start + len(quotes)))
        left = (
            ""
            if self.csfloat.remaining is None
            else f", лимит API: осталось {self.csfloat.remaining}"
        )
        self._say(
            f"CSFloat: проверено {len(quotes)} из {len(pool)} предметов со спросом, "
            f"с лотами {sum(q.min_price is not None for q in quotes)}{left}"
        )

    def _to_verify(
        self,
        scanner: Scanner,
        quotes: dict[str, dict[str, Quote]],
        sell: str,
        now: datetime,
        app_id: int,
        limit: int,
    ) -> list[str]:
        """Предметы, чьи продажи на ``sell`` нужно проверить: кандидаты и открытые сделки."""
        check = [
            name
            for buy, to in ROUTES
            if to == sell
            for name in scanner.candidates(buy, sell, quotes[buy], quotes[sell], now, limit)
        ]
        open_trades = sorted(
            {
                t.market_hash_name
                for t in self.storage.paper_trades(now - PAPER_HORIZON)
                if t.sell_market == sell and t.app_id == app_id
            }
        )
        return list(dict.fromkeys([*check, *open_trades]))

    def _verify_marketcsgo(self, names: list[str], app_id: int, now: datetime) -> None:
        """Статистика продаж Market.CSGO по истории сделок, в евро.

        Предметы, проверенные меньше часа назад, пропускаются. Ошибка прерывает
        проверку: все файлы лежат на одном сервере.
        """
        if not names or self.marketcsgo is None:
            return
        fresh = self.storage.recent_sales(
            "marketcsgo", SalesWindow.D7, now - MARKETCSGO_STATS_TTL, app_id
        )
        todo = [name for name in names if name not in fresh]
        if not todo:
            return
        self._say(f"Проверяю реальные продажи Market.CSGO: {len(todo)} предметов")
        stats = []
        for name in todo:
            try:
                deals = self.marketcsgo.fetch_last_sales(name, app_id)
            except SourceError as exc:
                self._say(f"Market.CSGO: продажи не получены ({exc}), проверка прервана")
                break
            stats.append(stats_from_sales(deals, "marketcsgo", app_id, name, Currency.EUR, now))
        self.storage.save_sales(stats)

    def _verify_dmarket(self, names: list[str], app_id: int, now: datetime) -> None:
        if not names or self.dmarket is None:
            return
        self._say(f"Проверяю реальные продажи DMarket: {len(names)} предметов (~{len(names)} с)")
        stats = []
        for name in names:
            try:
                deals = self.dmarket.fetch_last_sales(name, app_id)
            except SourceError as exc:
                self._say(f"  {name}: продажи не получены ({exc})")
                continue
            stats.append(stats_from_sales(deals, "dmarket", app_id, name, Currency.USD, now))
        self.storage.save_sales(stats)

    # ---------- уведомления ----------

    def notify(self, found: list[Opportunity], now: datetime) -> int:
        """Отправляет возможности, по которым не было уведомления за сутки.

        Returns:
            Сколько возможностей отправлено.
        """
        if self.telegram is None:
            self.out("Telegram не настроен: уведомления не отправлены (skinedge telegram setup)")
            return 0
        floor = ScanConfig()
        fresh = []
        for opp in found:
            r = opp.result
            if r.roi < floor.min_roi or r.profit.amount < floor.min_profit:
                continue
            last = self.storage.last_notified(
                opp.name, r.buy_market, r.sell_market, opp.buy_quote.app_id
            )
            if last is None or now - last >= NOTIFY_COOLDOWN:
                fresh.append(opp)
        if not fresh:
            self.out("Новых возможностей для уведомления нет")
            return 0
        batch = fresh[:NOTIFY_MAX]
        bot, chat_id = self.telegram
        try:
            bot.send(chat_id, format_digest(batch, len(found)))
        except TelegramError as exc:
            self.out(f"Telegram: уведомление не отправлено ({exc})")
            return 0
        for opp in batch:
            r = opp.result
            self.storage.save_notification(
                now,
                opp.name,
                r.buy_market,
                r.sell_market,
                r.profit.amount,
                r.roi,
                opp.buy_quote.app_id,
            )
        self.out(f"Отправлено в Telegram: {len(batch)} (без повторов за сутки)")
        return len(batch)


def _keep_sales(stats: SalesStats, universe: set[str]) -> bool:
    return (
        stats.market_hash_name in universe
        and stats.version is None
        and stats.window in SALES_WINDOWS
    )
