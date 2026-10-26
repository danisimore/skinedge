from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skinedge.cli import app
from skinedge.money import Currency
from skinedge.quotes import Quote, QuoteKind, SalesStats, SalesWindow
from skinedge.storage import Storage

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
NAME = "AK-47 | Redline (Field-Tested)"


def quote(price: str | None, at: datetime, source: str = "skinport") -> Quote:
    return Quote(
        source=source,
        app_id=730,
        market_hash_name=NAME,
        currency=Currency.EUR,
        kind=QuoteKind.AGGREGATE,
        min_price=None if price is None else Decimal(price),
        quantity=0 if price is None else 10,
        url="https://skinport.com/x",
        source_updated_at=at,
        fetched_at=at,
    )


def test_quotes_roundtrip_and_history() -> None:
    s = Storage(":memory:")
    s.save_quotes([quote("28.14", T0), quote("27.999", T0 + timedelta(minutes=5))])
    s.save_quotes([quote("9.10", T0, source="other")])
    latest = {q.source: q for q in s.latest_quotes(NAME)}
    assert latest["skinport"].min_price == Decimal("27.999")
    assert latest["skinport"].fetched_at == T0 + timedelta(minutes=5)
    assert latest["other"].min_price == Decimal("9.10")
    assert [q.min_price for q in s.quote_history(NAME, "skinport")] == [
        Decimal("27.999"),
        Decimal("28.14"),
    ]


def test_null_price_roundtrip() -> None:
    s = Storage(":memory:")
    s.save_quotes([quote(None, T0)])
    assert s.latest_quotes(NAME)[0].min_price is None


def test_sales_roundtrip() -> None:
    s = Storage(":memory:")
    stats = [
        SalesStats(
            source="skinport",
            app_id=730,
            market_hash_name=NAME,
            currency=Currency.EUR,
            window=w,
            min=Decimal("1.01"),
            max=None,
            avg=None,
            median=Decimal("2.5"),
            volume=3,
            fetched_at=T0,
        )
        for w in reversed(SalesWindow)
    ]
    s.save_sales(stats)
    got = s.latest_sales(NAME, "skinport")
    assert len(got) == 4
    assert [g.window for g in got] == list(SalesWindow)
    assert all(g.median == Decimal("2.5") and g.max is None for g in got)


def test_search_names() -> None:
    s = Storage(":memory:")
    s.save_quotes([quote("1", T0)])
    assert s.search_names("redline") == [NAME]


def test_cli_item_reads_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "x.db"
    s = Storage(db)
    s.save_quotes([quote("28.14", T0)])
    s.close()
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(db))
    result = CliRunner().invoke(app, ["item", "redline"])
    assert result.exit_code == 0, result.output
    assert "от 28.14 EUR" in result.output
    assert "УСТАРЕЛО" in result.output


def test_cli_item_no_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(tmp_path / "empty.db"))
    result = CliRunner().invoke(app, ["item", "nothing"])
    assert result.exit_code == 1
    assert "Нет данных" in result.output


def test_migration_adds_bid_columns(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE quotes (id INTEGER PRIMARY KEY, source TEXT NOT NULL,"
        " app_id INTEGER NOT NULL, market_hash_name TEXT NOT NULL, currency TEXT NOT NULL,"
        " kind TEXT NOT NULL,"
        " min_price TEXT, median_price TEXT, quantity INTEGER NOT NULL, listing_id TEXT, url TEXT,"
        " source_updated_at TEXT, fetched_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    q = quote("5", T0).model_copy(update={"best_bid": Decimal("4.5"), "bid_quantity": 3})
    s.save_quotes([q])
    got = s.latest_quotes(NAME)[0]
    assert got.best_bid == Decimal("4.5") and got.bid_quantity == 3


def test_liquid_names_uses_latest_fetch_and_price_range() -> None:
    s = Storage(":memory:")
    old = [quote("1", T0).model_copy(update={"market_hash_name": "old"})]
    new = [
        quote("2", T0 + timedelta(minutes=1)).model_copy(
            update={"market_hash_name": n, "quantity": qty, "min_price": Decimal(p)}
        )
        for n, qty, p in [("a", 5, "2"), ("b", 50, "3"), ("c", 500, "900")]
    ]
    s.save_quotes(old)
    s.save_quotes(new)
    assert s.liquid_names("skinport", 10, max_price=Decimal("500")) == ["b", "a"]
    assert s.liquid_names("skinport", 1) == ["c"]


def test_migration_drops_bad_dmarket_rows(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "v0.db"
    s = Storage(db)
    s.close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    s = Storage(db)
    bad = quote("2468", T0, source="dmarket")
    s.save_quotes([bad])
    s.close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.latest_quotes(NAME) == []
    s.save_quotes([quote("24.68", T0, source="dmarket")])
    s.close()
    s = Storage(db)  # повторное открытие ничего не удаляет
    assert s.latest_quotes(NAME)[0].min_price == Decimal("24.68")
