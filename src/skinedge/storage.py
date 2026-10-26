"""История котировок и продаж в SQLite.

Деньги хранятся как TEXT, чтобы не терять точность Decimal. Время хранится
в ISO 8601 UTC. Каждая выборка добавляет строки, ничего не перезаписывая:
так сохраняется полная история цен.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from skinedge.money import Currency
from skinedge.paper import MODEL, PaperTrade
from skinedge.quotes import Quote, QuoteKind, SalesStats, SalesWindow

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    app_id INTEGER NOT NULL,
    market_hash_name TEXT NOT NULL,
    currency TEXT NOT NULL,
    kind TEXT NOT NULL,
    min_price TEXT,
    median_price TEXT,
    quantity INTEGER NOT NULL,
    best_bid TEXT,
    bid_quantity INTEGER,
    listing_id TEXT,
    url TEXT,
    source_updated_at TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_item ON quotes (market_hash_name, source, fetched_at);
CREATE INDEX IF NOT EXISTS quotes_fetch ON quotes (source, fetched_at);

CREATE TABLE IF NOT EXISTS sales_stats (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    app_id INTEGER NOT NULL,
    market_hash_name TEXT NOT NULL,
    version TEXT,
    currency TEXT NOT NULL,
    period TEXT NOT NULL,
    min TEXT,
    max TEXT,
    avg TEXT,
    median TEXT,
    volume INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sales_item ON sales_stats (market_hash_name, source, fetched_at);
CREATE INDEX IF NOT EXISTS sales_fetch ON sales_stats (source, period, fetched_at);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    app_id INTEGER NOT NULL,
    market_hash_name TEXT NOT NULL,
    buy_market TEXT NOT NULL,
    sell_market TEXT NOT NULL,
    buy_price TEXT NOT NULL,
    buy_currency TEXT NOT NULL,
    sell_price TEXT NOT NULL,
    sell_currency TEXT NOT NULL,
    cost_eur TEXT NOT NULL,
    proceeds_eur TEXT NOT NULL,
    profit_eur TEXT NOT NULL,
    roi TEXT NOT NULL,
    usd_per_eur TEXT,
    complete INTEGER NOT NULL,
    lock_days INTEGER NOT NULL,
    model INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS paper_route ON paper_trades (market_hash_name, buy_market, sell_market);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    sent_at TEXT NOT NULL,
    market_hash_name TEXT NOT NULL,
    buy_market TEXT NOT NULL,
    sell_market TEXT NOT NULL,
    profit_eur TEXT NOT NULL,
    roi TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notify_item ON notifications (market_hash_name, buy_market, sell_market);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Старые цены и продажи прореживаются: после RAW_DAYS остаётся одна запись предмета
# за каждые THIN_HOURS часов, после KEEP_DAYS записи удаляются. Этого хватает для
# контрольных точек виртуальных сделок (до 7 дней) в отчёте за 30 дней.
RAW_DAYS = 2
THIN_HOURS = 6
KEEP_DAYS = 40

# Ключ интервала прореживания: дата и номер THIN_HOURS-часового отрезка суток (UTC).
_BUCKET = f"substr(fetched_at, 1, 11), CAST(substr(fetched_at, 12, 2) AS INTEGER) / {THIN_HOURS}"
_GROUPS = {
    "quotes": "source, market_hash_name",
    "sales_stats": "source, market_hash_name, version, period",
}


def _d(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pd(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _t(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _pt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _ptr(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Storage:
    """Хранилище истории.

    Args:
        path: Файл базы; ``":memory:"`` для тестов.
    """

    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Обновляет базу, созданную прежними версиями."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(quotes)")}
        paper_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_trades)")}
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        with self._tx() as c:
            if "best_bid" not in columns:
                c.execute("ALTER TABLE quotes ADD COLUMN best_bid TEXT")
            if "bid_quantity" not in columns:
                c.execute("ALTER TABLE quotes ADD COLUMN bid_quantity INTEGER")
            if "model" not in paper_columns:
                c.execute("ALTER TABLE paper_trades ADD COLUMN model INTEGER NOT NULL DEFAULT 1")
            if version < 1:
                # Версия 50d50f5 записывала цены DMarket в центах как доллары (×100).
                c.execute("DELETE FROM quotes WHERE source = 'dmarket'")
                c.execute("PRAGMA user_version = 1")

    def close(self) -> None:
        """Закрывает соединение."""
        self.conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def save_quotes(self, quotes: Iterable[Quote]) -> int:
        """Сохраняет котировки. Возвращает число записанных строк."""
        rows = [
            (
                q.source,
                q.app_id,
                q.market_hash_name,
                q.currency.value,
                q.kind.value,
                _d(q.min_price),
                _d(q.median_price),
                q.quantity,
                _d(q.best_bid),
                q.bid_quantity,
                q.listing_id,
                q.url,
                _t(q.source_updated_at),
                _t(q.fetched_at),
            )
            for q in quotes
        ]
        with self._tx() as c:
            c.executemany(
                "INSERT INTO quotes (source, app_id, market_hash_name, currency, kind,"
                " min_price, median_price, quantity, best_bid, bid_quantity, listing_id, url,"
                " source_updated_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def save_sales(self, stats: Iterable[SalesStats]) -> int:
        """Сохраняет статистику продаж. Возвращает число записанных строк."""
        rows = [
            (
                s.source,
                s.app_id,
                s.market_hash_name,
                s.version,
                s.currency.value,
                s.window.value,
                _d(s.min),
                _d(s.max),
                _d(s.avg),
                _d(s.median),
                s.volume,
                _t(s.fetched_at),
            )
            for s in stats
        ]
        with self._tx() as c:
            c.executemany(
                "INSERT INTO sales_stats (source, app_id, market_hash_name, version, currency,"
                " period, min, max, avg, median, volume, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def latest_quotes(self, market_hash_name: str) -> list[Quote]:
        """Последняя котировка по предмету от каждого источника."""
        cur = self.conn.execute(
            "SELECT q.* FROM quotes q JOIN ("
            "  SELECT source, MAX(fetched_at) AS f FROM quotes"
            "  WHERE market_hash_name = ? GROUP BY source"
            ") m ON q.source = m.source AND q.fetched_at = m.f"
            " WHERE q.market_hash_name = ? ORDER BY q.source",
            (market_hash_name, market_hash_name),
        )
        return [self._quote(r) for r in cur]

    def quote_history(self, market_hash_name: str, source: str, limit: int = 50) -> list[Quote]:
        """История котировок предмета на площадке, новые первыми."""
        cur = self.conn.execute(
            "SELECT * FROM quotes WHERE market_hash_name = ? AND source = ?"
            " ORDER BY fetched_at DESC LIMIT ?",
            (market_hash_name, source, limit),
        )
        return [self._quote(r) for r in cur]

    def latest_sales(self, market_hash_name: str, source: str) -> list[SalesStats]:
        """Последняя статистика продаж предмета по всем окнам и вариантам."""
        cur = self.conn.execute(
            "SELECT * FROM sales_stats WHERE market_hash_name = ? AND source = ? AND fetched_at = ("
            "  SELECT MAX(fetched_at) FROM sales_stats WHERE market_hash_name = ? AND source = ?"
            ")",
            (market_hash_name, source, market_hash_name, source),
        )
        order = list(SalesWindow)
        stats = [self._sales(r) for r in cur]
        return sorted(stats, key=lambda st: (st.version or "", order.index(st.window)))

    def liquid_names(
        self,
        source: str,
        limit: int,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
    ) -> list[str]:
        """Самые ликвидные предметы последней загрузки источника (по числу лотов).

        Args:
            source: Площадка, по чьей последней загрузке отбирать.
            limit: Сколько названий вернуть.
            min_price: Нижняя граница минимальной цены.
            max_price: Верхняя граница минимальной цены.

        Returns:
            Названия, от самых ликвидных.
        """
        rows = self.conn.execute(
            "SELECT market_hash_name, min_price, quantity FROM quotes WHERE source = ?"
            " AND fetched_at = (SELECT MAX(fetched_at) FROM quotes WHERE source = ?)"
            " AND min_price IS NOT NULL ORDER BY quantity DESC",
            (source, source),
        )
        names: list[str] = []
        for r in rows:
            price = Decimal(r["min_price"])
            if min_price is not None and price < min_price:
                continue
            if max_price is not None and price > max_price:
                continue
            names.append(r["market_hash_name"])
            if len(names) >= limit:
                break
        return names

    def recent_quotes(self, source: str, since: datetime) -> dict[str, Quote]:
        """Последняя котировка каждого предмета площадки, полученная не раньше ``since``."""
        cur = self.conn.execute(
            "SELECT * FROM quotes WHERE source = ? AND fetched_at >= ? ORDER BY fetched_at",
            (source, since.isoformat()),
        )
        return {r["market_hash_name"]: self._quote(r) for r in cur}

    def recent_sales(
        self, source: str, window: SalesWindow, since: datetime
    ) -> dict[str, SalesStats]:
        """Последняя статистика продаж каждого предмета за окно ``window``.

        Берутся только строки без варианта (``version``): сводка по всему названию.
        """
        cur = self.conn.execute(
            "SELECT * FROM sales_stats WHERE source = ? AND period = ? AND version IS NULL"
            " AND fetched_at >= ? ORDER BY fetched_at",
            (source, window.value, since.isoformat()),
        )
        return {r["market_hash_name"]: self._sales(r) for r in cur}

    def quote_after(self, market_hash_name: str, source: str, at: datetime) -> Quote | None:
        """Первая котировка предмета, полученная в момент ``at`` или позже."""
        row = self.conn.execute(
            "SELECT * FROM quotes WHERE market_hash_name = ? AND source = ? AND fetched_at >= ?"
            " ORDER BY fetched_at LIMIT 1",
            (market_hash_name, source, at.isoformat()),
        ).fetchone()
        return None if row is None else self._quote(row)

    def sales_after(
        self, market_hash_name: str, source: str, window: SalesWindow, at: datetime
    ) -> SalesStats | None:
        """Первая статистика продаж предмета, полученная в момент ``at`` или позже."""
        row = self.conn.execute(
            "SELECT * FROM sales_stats WHERE market_hash_name = ? AND source = ? AND period = ?"
            " AND version IS NULL AND fetched_at >= ? ORDER BY fetched_at LIMIT 1",
            (market_hash_name, source, window.value, at.isoformat()),
        ).fetchone()
        return None if row is None else self._sales(row)

    def save_paper(self, trade: PaperTrade) -> int:
        """Записывает виртуальную сделку. Возвращает её id."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO paper_trades (created_at, app_id, market_hash_name, buy_market,"
                " sell_market, buy_price, buy_currency, sell_price, sell_currency, cost_eur,"
                " proceeds_eur, profit_eur, roi, usd_per_eur, complete, lock_days, model)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _t(trade.created_at),
                    trade.app_id,
                    trade.market_hash_name,
                    trade.buy_market,
                    trade.sell_market,
                    str(trade.buy_price),
                    trade.buy_currency.value,
                    str(trade.sell_price),
                    trade.sell_currency.value,
                    str(trade.cost_eur),
                    str(trade.proceeds_eur),
                    str(trade.profit_eur),
                    str(trade.roi),
                    _d(trade.usd_per_eur),
                    int(trade.complete),
                    trade.lock_days,
                    trade.model,
                ),
            )
        return int(cur.lastrowid or 0)

    def paper_trades(self, since: datetime | None = None, model: int = MODEL) -> list[PaperTrade]:
        """Виртуальные сделки текущей модели оценки, старые первыми."""
        cur = self.conn.execute(
            "SELECT * FROM paper_trades WHERE created_at >= ? AND model = ?"
            " ORDER BY created_at, id",
            (_t(since) or "", model),
        )
        return [
            PaperTrade(
                id=r["id"],
                created_at=_ptr(r["created_at"]),
                app_id=r["app_id"],
                market_hash_name=r["market_hash_name"],
                buy_market=r["buy_market"],
                sell_market=r["sell_market"],
                buy_price=Decimal(r["buy_price"]),
                buy_currency=Currency(r["buy_currency"]),
                sell_price=Decimal(r["sell_price"]),
                sell_currency=Currency(r["sell_currency"]),
                cost_eur=Decimal(r["cost_eur"]),
                proceeds_eur=Decimal(r["proceeds_eur"]),
                profit_eur=Decimal(r["profit_eur"]),
                roi=Decimal(r["roi"]),
                usd_per_eur=_pd(r["usd_per_eur"]),
                complete=bool(r["complete"]),
                lock_days=r["lock_days"],
                model=r["model"],
            )
            for r in cur
        ]

    def last_paper(
        self, market_hash_name: str, buy_market: str, sell_market: str
    ) -> datetime | None:
        """Когда последний раз записана виртуальная сделка по этому маршруту."""
        row = self.conn.execute(
            "SELECT MAX(created_at) FROM paper_trades WHERE market_hash_name = ?"
            " AND buy_market = ? AND sell_market = ? AND model = ?",
            (market_hash_name, buy_market, sell_market, MODEL),
        ).fetchone()
        return None if row[0] is None else _ptr(row[0])

    def last_notified(
        self, market_hash_name: str, buy_market: str, sell_market: str
    ) -> datetime | None:
        """Когда последний раз отправлялось уведомление по этому маршруту."""
        row = self.conn.execute(
            "SELECT MAX(sent_at) FROM notifications WHERE market_hash_name = ?"
            " AND buy_market = ? AND sell_market = ?",
            (market_hash_name, buy_market, sell_market),
        ).fetchone()
        return None if row[0] is None else _ptr(row[0])

    def save_notification(
        self,
        sent_at: datetime,
        market_hash_name: str,
        buy_market: str,
        sell_market: str,
        profit_eur: Decimal,
        roi: Decimal,
    ) -> None:
        """Отмечает, что уведомление по маршруту отправлено."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO notifications (sent_at, market_hash_name, buy_market, sell_market,"
                " profit_eur, roi) VALUES (?,?,?,?,?,?)",
                (
                    sent_at.isoformat(),
                    market_hash_name,
                    buy_market,
                    sell_market,
                    str(profit_eur),
                    str(roi),
                ),
            )

    def get_meta(self, key: str) -> str | None:
        """Служебное значение (время последнего отчёта, сигнал фонового запуска)."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """Записывает служебное значение."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        """Удаляет служебное значение."""
        with self._tx() as c:
            c.execute("DELETE FROM meta WHERE key = ?", (key,))

    def prune(self, now: datetime) -> dict[str, int]:
        """Прореживает и удаляет старые цены и продажи.

        Виртуальные сделки и отметки об уведомлениях не трогаются.

        Returns:
            Сколько строк удалено из каждой таблицы.
        """
        drop = (now - timedelta(days=KEEP_DAYS)).isoformat()
        thin = (now - timedelta(days=RAW_DAYS)).isoformat()
        removed: dict[str, int] = {}
        with self._tx() as c:
            for table, group in _GROUPS.items():
                n = c.execute(f"DELETE FROM {table} WHERE fetched_at < ?", (drop,)).rowcount
                n += c.execute(
                    f"DELETE FROM {table} WHERE fetched_at < ? AND id NOT IN ("
                    f"  SELECT MIN(id) FROM {table} WHERE fetched_at < ?"
                    f"  GROUP BY {group}, {_BUCKET})",
                    (thin, thin),
                ).rowcount
                removed[table] = n
        return removed

    def search_names(self, pattern: str, limit: int = 20) -> list[str]:
        """Названия предметов, содержащие подстроку (без учёта регистра)."""
        cur = self.conn.execute(
            "SELECT DISTINCT market_hash_name FROM quotes WHERE market_hash_name LIKE ?"
            " ORDER BY market_hash_name LIMIT ?",
            (f"%{pattern}%", limit),
        )
        return [r[0] for r in cur]

    @staticmethod
    def _sales(r: sqlite3.Row) -> SalesStats:
        return SalesStats(
            source=r["source"],
            app_id=r["app_id"],
            market_hash_name=r["market_hash_name"],
            version=r["version"],
            currency=Currency(r["currency"]),
            window=SalesWindow(r["period"]),
            min=_pd(r["min"]),
            max=_pd(r["max"]),
            avg=_pd(r["avg"]),
            median=_pd(r["median"]),
            volume=r["volume"],
            fetched_at=_ptr(r["fetched_at"]),
        )

    @staticmethod
    def _quote(r: sqlite3.Row) -> Quote:
        return Quote(
            source=r["source"],
            app_id=r["app_id"],
            market_hash_name=r["market_hash_name"],
            currency=Currency(r["currency"]),
            kind=QuoteKind(r["kind"]),
            min_price=_pd(r["min_price"]),
            median_price=_pd(r["median_price"]),
            quantity=r["quantity"],
            best_bid=_pd(r["best_bid"]),
            bid_quantity=r["bid_quantity"],
            listing_id=r["listing_id"],
            url=r["url"],
            source_updated_at=_pt(r["source_updated_at"]),
            fetched_at=_ptr(r["fetched_at"]),
        )
