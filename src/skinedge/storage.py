"""История котировок и продаж в SQLite.

Деньги хранятся как TEXT, чтобы не терять точность Decimal. Время хранится
в ISO 8601 UTC. Каждая выборка добавляет строки, ничего не перезаписывая:
так сохраняется полная история цен.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from skinedge.money import Currency
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
"""


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
                q.listing_id,
                q.url,
                _t(q.source_updated_at),
                _t(q.fetched_at),
            )
            for q in quotes
        ]
        with self._tx() as c:
            c.executemany(
                "INSERT INTO quotes (source, app_id, market_hash_name, currency, kind, min_price,"
                " median_price, quantity, listing_id, url, source_updated_at, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
            ") ORDER BY version, period",
            (market_hash_name, source, market_hash_name, source),
        )
        return [
            SalesStats(
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
            for r in cur
        ]

    def search_names(self, pattern: str, limit: int = 20) -> list[str]:
        """Названия предметов, содержащие подстроку (без учёта регистра)."""
        cur = self.conn.execute(
            "SELECT DISTINCT market_hash_name FROM quotes WHERE market_hash_name LIKE ?"
            " ORDER BY market_hash_name LIMIT ?",
            (f"%{pattern}%", limit),
        )
        return [r[0] for r in cur]

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
            listing_id=r["listing_id"],
            url=r["url"],
            source_updated_at=_pt(r["source_updated_at"]),
            fetched_at=_ptr(r["fetched_at"]),
        )
