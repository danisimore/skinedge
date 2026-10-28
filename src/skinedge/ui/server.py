"""HTTP-сервер интерфейса: страница и JSON API поверх той же базы, что у сканера.

Сервер слушает только 127.0.0.1. Чтобы чужой сайт, открытый в том же браузере,
не мог ничего изменить через этот адрес, сервер принимает только свой заголовок
``Host``, а запросы на изменение — только с заголовком ``X-SkinEdge`` (браузер
не пошлёт его с чужого сайта без разрешения CORS, которого сервер не даёт).

Вся логика — в :class:`App` (тестируется без сети), HTTP — тонкая обёртка.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from skinedge.fees.schedule import FeeSchedule, Payout
from skinedge.fx import FxRates
from skinedge.games import TITLES
from skinedge.journal import NoRate, Trade, Valuation, payout_at, tradable_from, valuate
from skinedge.money import Currency
from skinedge.pipeline import ScanOptions
from skinedge.runner import HEARTBEAT, LOCK_STALE
from skinedge.sources.base import SourceError
from skinedge.storage import Storage
from skinedge.tuning import ScanSettings
from skinedge.tuning import load as load_tuning
from skinedge.tuning import reset as reset_tuning
from skinedge.tuning import save as save_tuning

log = logging.getLogger(__name__)

LABELS = {
    "skinport": "Skinport",
    "dmarket": "DMarket",
    "csfloat": "CSFloat",
    "marketcsgo": "Market.CSGO",
    "lisskins": "Lis-Skins",
    "steam": "Steam",
}
FX_KEY = "ui.fx"
FX_REFRESH = timedelta(hours=6)
NAMES_REFRESH = 3600.0
QUOTE_MAX_AGE = timedelta(days=2)
MAX_BODY = 64 * 1024

FIELD_LABELS = {
    "min_price": "Цена от",
    "max_price": "Цена до",
    "limit": "Предметов на игру",
    "min_sales_7d": "Продаж за 7 дней",
    "min_profit": "Прибыль от",
    "min_roi": "ROI от",
    "app_id": "Игра",
    "name": "Предмет",
    "bought_at": "Дата покупки",
    "buy_market": "Где куплен",
    "buy_price": "Цена покупки",
    "target_market": "Где продавать",
    "target_price": "Цена там сейчас",
    "sold_at": "Дата продажи",
    "sell_market": "Где продан",
    "sell_price": "Цена продажи",
    "note": "Заметка",
}


class BadRequest(ValueError):
    """Ошибка во входных данных; ``fields`` — ошибки по полям."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


@dataclass(frozen=True)
class Response:
    """Ответ сервера."""

    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


def _json(status: int, data: Any) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False, default=str).encode())


def _ru(error: dict[str, Any]) -> str:
    kind, ctx = error.get("type", ""), error.get("ctx") or {}
    if kind == "greater_than":
        return f"должно быть больше {ctx.get('gt')}"
    if kind == "greater_than_equal":
        return f"не меньше {ctx.get('ge')}"
    if kind in ("less_than_equal", "less_than"):
        return f"не больше {ctx.get('le', ctx.get('lt'))}"
    if kind in ("decimal_parsing", "int_parsing", "decimal_type", "int_type", "int_from_float"):
        return "нужно число"
    if kind in ("date_parsing", "date_from_datetime_parsing", "date_type"):
        return "нужна дата"
    if kind == "missing":
        return "заполни поле"
    if kind in ("string_too_short",):
        return "заполни поле"
    if kind in ("string_too_long",):
        return "слишком длинно"
    if kind == "value_error":
        return str(error.get("msg", "")).removeprefix("Value error, ")
    return str(error.get("msg", "неверное значение"))


def _bad(exc: ValidationError, rename: dict[str, str] | None = None) -> BadRequest:
    fields: dict[str, str] = {}
    general: list[str] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        name = (rename or {}).get(loc[0], loc[0]) if loc else ""
        if name in FIELD_LABELS:
            fields.setdefault(name, _ru(dict(err)))
        else:
            general.append(_ru(dict(err)))
    message = general[0] if general else "Проверь отмеченные поля"
    return BadRequest(message[:1].upper() + message[1:], fields)


@dataclass
class Rates:
    """Курс USD для журнала: ЕЦБ раз в 6 часов, при сбое — последний полученный.

    Args:
        fetch: Загрузка курсов (ЕЦБ).
        storage: База, где хранится последний полученный курс.
        clock: Текущее время UTC.
    """

    fetch: Callable[[], FxRates]
    storage: Storage
    clock: Callable[[], datetime]
    _rate: Decimal | None = None
    _source: str | None = None
    _as_of: datetime | None = None
    _checked: datetime | None = field(default=None, repr=False)

    def current(self) -> tuple[Decimal | None, str | None, datetime | None]:
        """Курс USD за 1 EUR, источник и время курса."""
        now = self.clock()
        if self._checked is None or now - self._checked >= FX_REFRESH:
            self._checked = now
            try:
                fx = self.fetch()
                self._rate = fx.rates[Currency.USD]
                self._source = fx.source
                self._as_of = fx.as_of or now
                self.storage.set_meta(
                    FX_KEY, json.dumps({"rate": str(self._rate), "as_of": self._as_of.isoformat()})
                )
            except (SourceError, KeyError, ValueError) as exc:
                log.warning("Курс ЕЦБ не получен: %s", exc)
                if self._rate is None:
                    self._load_saved()
        return self._rate, self._source, self._as_of

    def _load_saved(self) -> None:
        raw = self.storage.get_meta(FX_KEY)
        if raw is None:
            return
        try:
            data = json.loads(raw)
            self._rate = Decimal(data["rate"])
            self._as_of = datetime.fromisoformat(data["as_of"])
            self._source = "сохранённый ЕЦБ"
        except (ValueError, KeyError, TypeError, ArithmeticError):
            log.warning("Сохранённый курс повреждён")


class App:
    """API интерфейса.

    Args:
        storage: База SkinEdge.
        schedule: Площадки и правила комиссий.
        rates: Курс USD.
        spread: Потери при конвертации валют.
        defaults: Параметры, с которыми сканер работает без настроек из интерфейса.
        clock: Текущее время UTC.
    """

    def __init__(
        self,
        storage: Storage,
        schedule: FeeSchedule,
        rates: Rates,
        spread: Decimal,
        defaults: ScanOptions | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.storage = storage
        self.schedule = schedule
        self.rates = rates
        self.spread = spread
        self.defaults = ScanSettings.of(defaults or ScanOptions())
        self.clock = clock
        self._names: list[str] = []
        self._names_at: float | None = None

    # ---------- маршрутизация ----------

    def handle(self, method: str, path: str, query: dict[str, str], body: bytes | None) -> Response:
        """Обрабатывает запрос. Неизвестный путь — 404, ошибка данных — 400."""
        try:
            return self._route(method, path, query, body)
        except BadRequest as exc:
            return _json(400, {"error": str(exc), "fields": exc.fields})
        except LookupError as exc:
            return _json(404, {"error": str(exc).strip("'\"")})

    def _route(self, method: str, path: str, query: dict[str, str], body: bytes | None) -> Response:
        if method == "GET" and path in ("/", "/index.html"):
            page = files("skinedge.ui").joinpath("index.html").read_bytes()
            return Response(200, page, "text/html; charset=utf-8")
        if path == "/api/state" and method == "GET":
            return _json(200, self.state())
        if path == "/api/settings":
            if method == "PUT":
                return _json(200, self.put_settings(_body(body)))
            if method == "DELETE":
                reset_tuning(self.storage)
                return _json(200, self.state())
        if path == "/api/trades":
            if method == "GET":
                return _json(200, self.journal())
            if method == "POST":
                return _json(201, self.create_trade(_body(body)))
        if path.startswith("/api/trades/"):
            trade_id = _id(path.removeprefix("/api/trades/"))
            if method == "PUT":
                return _json(200, self.update_trade(trade_id, _body(body)))
            if method == "DELETE":
                if not self.storage.delete_trade(trade_id):
                    raise LookupError(f"Сделка {trade_id} не найдена")
                return _json(200, {"deleted": trade_id})
        if path == "/api/names" and method == "GET":
            return _json(200, {"names": self.names(query.get("q", ""))})
        if path == "/api/quote" and method == "GET":
            return _json(200, {"quote": self.quote(query.get("name", ""), query.get("market", ""))})
        raise LookupError("Нет такой страницы")

    # ---------- состояние и настройки ----------

    def state(self) -> dict[str, Any]:
        """Площадки, игры, настройки сканера, курс и состояние фонового запуска."""
        saved = load_tuning(self.storage)
        rate, source, as_of = self.rates.current()
        beat = self.storage.get_meta(HEARTBEAT)
        beat_at = datetime.fromisoformat(beat) if beat else None
        return {
            "markets": [
                {
                    "id": name,
                    "label": LABELS.get(name, name),
                    "currency": m.currency.value,
                    "lock_days": m.transfer_lock_days,
                    "settlement_days": m.settlement_days,
                    "wallet": m.payout is Payout.STEAM_WALLET,
                }
                for name, m in self.schedule.marketplaces.items()
            ],
            "games": [{"app_id": app_id, "title": t} for app_id, t in TITLES.items()],
            "settings": (saved or self.defaults).model_dump(mode="json"),
            "settings_saved": saved is not None,
            "defaults": self.defaults.model_dump(mode="json"),
            "fx": None
            if rate is None
            else {"usd_per_eur": str(rate), "source": source, "as_of": _iso(as_of)},
            "scanner": {
                "heartbeat": _iso(beat_at),
                "running": beat_at is not None and self.clock() - beat_at < LOCK_STALE,
            },
        }

    def put_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        """Проверяет и сохраняет настройки сканера."""
        try:
            settings = ScanSettings.model_validate(data)
        except ValidationError as exc:
            raise _bad(exc) from exc
        save_tuning(self.storage, settings)
        return self.state()

    # ---------- журнал ----------

    def journal(self) -> dict[str, Any]:
        """Все сделки с расчётом прибыли и сводка."""
        rate, _, _ = self.rates.current()
        views = [self._view(t, rate) for t in self.storage.trades()]
        return {"trades": [v for v, _ in views], "summary": self._summary(views)}

    def create_trade(self, data: dict[str, Any]) -> dict[str, Any]:
        """Добавляет сделку в журнал."""
        now = self.clock()
        rate, _, _ = self.rates.current()
        fields = self._input(data, sale=False)
        trade = self._validate(
            {
                **fields,
                "target_at": now,
                "buy_usd_per_eur": rate,
                "created_at": now,
            }
        )
        trade_id = self.storage.save_trade(trade)
        return self._view(trade.model_copy(update={"id": trade_id}), rate)[0]

    def update_trade(self, trade_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Меняет сделку: исправление данных, новая текущая цена, продажа или её отмена."""
        old = self.storage.trade(trade_id)
        if old is None:
            raise LookupError(f"Сделка {trade_id} не найдена")
        now = self.clock()
        rate, _, _ = self.rates.current()
        fields = self._input(data, sale=True)
        update: dict[str, Any] = {**old.model_dump(), **fields, "id": trade_id}
        if (fields["target_market"], fields["target_price"]) != (
            old.target_market,
            old.target_price,
        ):
            update["target_at"] = now
        if update["buy_usd_per_eur"] is None:
            update["buy_usd_per_eur"] = rate
        if fields["sold_at"] is None:
            update["sell_usd_per_eur"] = None
        elif (fields["sell_market"], fields["sell_price"], fields["sold_at"]) != (
            old.sell_market,
            old.sell_price,
            old.sold_at,
        ) or old.sell_usd_per_eur is None:
            update["sell_usd_per_eur"] = rate
        trade = self._validate(update)
        self.storage.save_trade(trade)
        return self._view(trade, rate)[0]

    def _input(self, data: dict[str, Any], sale: bool) -> dict[str, Any]:
        """Поля сделки из формы; валюты берутся из правил площадок."""
        errors: dict[str, str] = {}

        def market(key: str, required: bool = True) -> str | None:
            value = data.get(key) or None
            if value is None:
                if required:
                    errors[key] = "выбери площадку"
                return None
            if value not in self.schedule.marketplaces:
                errors[key] = "неизвестная площадка"
                return None
            return str(value)

        def currency(name: str | None) -> str | None:
            return None if name is None else self.schedule.market(name).currency.value

        buy = market("buy_market")
        target = market("target_market")
        fields: dict[str, Any] = {
            "app_id": data.get("app_id"),
            "market_hash_name": str(data.get("name") or "").strip(),
            "bought_at": data.get("bought_at") or None,
            "buy_market": buy,
            "buy_price": _num(data.get("buy_price")),
            "buy_currency": currency(buy),
            "target_market": target,
            "target_price": _num(data.get("target_price")),
            "target_currency": currency(target),
            "note": str(data.get("note") or "").strip(),
        }
        if sale:
            sell = market("sell_market", required=False)
            sold_at = data.get("sold_at") or None
            price = _num(data.get("sell_price"))
            if sold_at is None and sell is None and price is None:
                fields.update(sold_at=None, sell_market=None, sell_price=None, sell_currency=None)
            else:
                if sell is None and "sell_market" not in errors:
                    errors["sell_market"] = "выбери площадку"
                if price is None:
                    errors["sell_price"] = "укажи цену"
                if sold_at is None:
                    errors["sold_at"] = "укажи дату"
                fields.update(
                    sold_at=sold_at,
                    sell_market=sell,
                    sell_price=price,
                    sell_currency=currency(sell),
                )
        if errors:
            raise BadRequest("Проверь отмеченные поля", errors)
        return fields

    def _validate(self, data: dict[str, Any]) -> Trade:
        try:
            return Trade.model_validate(data)
        except ValidationError as exc:
            raise _bad(exc, {"market_hash_name": "name"}) from exc

    def _view(self, trade: Trade, rate: Decimal | None) -> tuple[dict[str, Any], Valuation | None]:
        today = self.clock().date()
        unlock = tradable_from(trade, self.schedule)
        valuation: Valuation | None = None
        error = None
        try:
            valuation = valuate(trade, self.schedule, rate, self.spread)
        except NoRate as exc:
            error = str(exc)
        except (KeyError, ValueError) as exc:
            error = f"Не посчитать: {str(exc).strip(chr(39))}"
        status = "sold" if trade.sold else "locked" if today < unlock else "tradable"
        data = trade.model_dump(mode="json", exclude={"market_hash_name"})
        data.update(
            name=trade.market_hash_name,
            game=TITLES.get(trade.app_id, str(trade.app_id)),
            status=status,
            tradable_from=unlock.isoformat(),
            payout_at=_iso(payout_at(trade, self.schedule)),
            valuation=None if valuation is None else _valuation(valuation),
            error=error,
        )
        return data, valuation

    @staticmethod
    def _summary(views: list[tuple[dict[str, Any], Valuation | None]]) -> dict[str, Any]:
        sold = [(v, val) for v, val in views if v["status"] == "sold"]
        held = [(v, val) for v, val in views if v["status"] != "sold"]
        cash = [val for _, val in sold if val is not None and not val.wallet]
        wallet = [val for _, val in sold if val is not None and val.wallet]
        open_vals = [val for _, val in held if val is not None]
        realized_cost = sum((v.cost.amount for v in cash), Decimal(0))
        realized = sum((v.profit.amount for v in cash), Decimal(0))
        return {
            "sold": len(sold),
            "open": len(held),
            "realized_profit": str(realized),
            "realized_roi": str((realized / realized_cost).quantize(Decimal("0.0001")))
            if realized_cost
            else None,
            "wallet_profit": str(sum((v.profit.amount for v in wallet), Decimal(0)))
            if wallet
            else None,
            "open_cost": str(sum((v.cost.amount for v in open_vals), Decimal(0))),
            "open_profit": str(sum((v.profit.amount for v in open_vals), Decimal(0))),
            "not_counted": sum(val is None for _, val in views),
            "incomplete": sum(1 for _, val in views if val is not None and not val.complete),
        }

    # ---------- подсказки ----------

    def names(self, q: str) -> list[str]:
        """Названия предметов из базы сканера, содержащие строку."""
        q = q.strip().casefold()
        if len(q) < 2:
            return []
        if self._names_at is None or time.monotonic() - self._names_at > NAMES_REFRESH:
            known = self.storage.conn.execute("SELECT DISTINCT market_hash_name FROM quotes")
            journal = {t.market_hash_name for t in self.storage.trades()}
            self._names = sorted({r[0] for r in known} | journal)
            self._names_at = time.monotonic()
        starts = [n for n in self._names if n.casefold().startswith(q)]
        inside = [n for n in self._names if q in n.casefold() and n not in starts]
        return (starts + inside)[:12]

    def quote(self, name: str, market: str) -> dict[str, Any] | None:
        """Последняя цена предмета на площадке по данным сканера (не старше 2 дней)."""
        if not name or not market:
            return None
        for q in self.storage.latest_quotes(name):
            if q.source != market or q.min_price is None:
                continue
            if self.clock() - q.fetched_at > QUOTE_MAX_AGE:
                return None
            return {
                "price": str(q.min_price),
                "currency": q.currency.value,
                "fetched_at": q.fetched_at.isoformat(),
            }
        return None


def _valuation(v: Valuation) -> dict[str, Any]:
    return {
        "sell_market": v.sell_market,
        "sell_price": str(v.sell_price.amount),
        "sell_currency": v.sell_price.currency.value,
        "cost": str(v.cost.amount),
        "proceeds": str(v.proceeds.amount),
        "profit": str(v.profit.amount),
        "roi": str(v.roi),
        "wallet": v.wallet,
        "complete": v.complete,
        "lines": v.lines,
        "warnings": v.warnings,
    }


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _num(value: Any) -> str | None:
    """Число из формы: принимает запятую как десятичный разделитель."""
    if value is None or value == "":
        return None
    return str(value).strip().replace(",", ".").replace(" ", "")


def _id(text: str) -> int:
    if not text.isdigit():
        raise LookupError("Нет такой страницы")
    return int(text)


def _body(body: bytes | None) -> dict[str, Any]:
    try:
        data = json.loads(body or b"{}")
    except ValueError as exc:
        raise BadRequest("Запрос не в формате JSON") from exc
    if not isinstance(data, dict):
        raise BadRequest("Ожидался объект JSON")
    return data


# ---------- HTTP ----------

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def make_handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    """Класс обработчика запросов для :class:`http.server.HTTPServer`."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    lock = threading.Lock()  # одно соединение с базой: запросы по одному

    class Handler(BaseHTTPRequestHandler):
        server_version = "SkinEdge"
        timeout = 15  # браузер держит пустые соединения про запас

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            if self.headers.get("Host") not in hosts:
                self._send(Response(403, b"Forbidden", "text/plain; charset=utf-8"))
                return
            if method != "GET":
                origin = self.headers.get("Origin")
                if self.headers.get("X-SkinEdge") != "1" or (origin and origin not in origins):
                    self._send(_json(403, {"error": "Запрос не из интерфейса SkinEdge"}))
                    return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(_json(413, {"error": "Слишком большой запрос"}))
                return
            body = self.rfile.read(length) if length else None
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                with lock:
                    response = app.handle(method, url.path, query, body)
            except Exception:
                log.exception("Ошибка интерфейса: %s %s", method, url.path)
                response = _json(500, {"error": "Внутренняя ошибка, подробности в консоли"})
            self._send(response)

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s " + format, self.address_string(), *args)

    return Handler


def serve(app: App, port: int, ready: Callable[[str], None] | None = None) -> None:
    """Запускает сервер на 127.0.0.1 до Ctrl+C.

    Raises:
        OSError: Если порт занят.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    if ready is not None:
        ready(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
