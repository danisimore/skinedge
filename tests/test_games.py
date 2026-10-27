import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_csfloat import FakeCSFloat
from test_runner import FakeDMarket, FakeSkinport
from test_scanner import FX, NAME
from typer.testing import CliRunner

from skinedge.cli import app
from skinedge.fees.schedule import FeeSchedule
from skinedge.games import CS2, DOTA2, RUST, parse_games, title
from skinedge.pipeline import Pipeline, ScanOptions
from skinedge.quotes import Quote, Sale, SalesStats
from skinedge.sources.base import SourceError
from skinedge.sources.dmarket import _item_url
from skinedge.storage import Storage

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def test_parse_games() -> None:
    assert parse_games("cs2, Dota2,rust,cs2") == (CS2, DOTA2, RUST)
    assert title(RUST) == "Rust" and title(440) == "app_id 440"
    with pytest.raises(ValueError, match="tf2"):
        parse_games("cs2,tf2")
    with pytest.raises(ValueError):
        parse_games(" , ")


def test_dmarket_links_per_game() -> None:
    assert _item_url(DOTA2, "Dragonclaw Hook") == (
        "https://dmarket.com/ingame-items/item-list/dota2-skins?title=Dragonclaw%20Hook"
    )
    assert "rust-skins" in (_item_url(RUST, "Tempered AK47") or "")
    assert _item_url(440, "Key") is None


def test_steam_link_uses_game() -> None:
    template = FeeSchedule.load().market("steam").item_url or ""
    assert template.format(name="X", app_id=RUST) == (
        "https://steamcommunity.com/market/listings/252490/X"
    )


# ---------- проход по нескольким играм ----------


def _game(items: list[Any], app_id: int) -> list[Any]:
    return [i.model_copy(update={"app_id": app_id}) for i in items]


class GameSkinport(FakeSkinport):
    """Одинаковый набор предметов в каждой игре; ``broken`` — игры, где Skinport падает."""

    def __init__(self, now: datetime, broken: tuple[int, ...] = ()) -> None:
        super().__init__(now)
        self.broken = broken
        self.games: list[int] = []

    def fetch_quotes(self, app_id: int = CS2) -> list[Quote]:
        self.games.append(app_id)
        if app_id in self.broken:
            raise SourceError("Skinport /items: HTTP 503")
        return _game(super().fetch_quotes(app_id), app_id)

    def fetch_sales(self, app_id: int = CS2) -> list[SalesStats]:
        return _game(super().fetch_sales(app_id), app_id)


class GameDMarket(FakeDMarket):
    def fetch_best_prices(self, titles: list[str], app_id: int = CS2) -> list[Quote]:
        return _game(super().fetch_best_prices(titles, app_id), app_id)

    def fetch_last_sales(self, title: str, app_id: int = CS2, limit: int = 20) -> list[Sale]:
        return super().fetch_last_sales(title, app_id, limit)


def _pipeline(sp: GameSkinport, storage: Storage, out: list[str], cf: Any = None) -> Pipeline:
    return Pipeline(
        storage,
        FeeSchedule.load(),
        FX,
        skinport=sp,  # type: ignore[arg-type]
        dmarket=GameDMarket(NOW),  # type: ignore[arg-type]
        csfloat=cf,
        out=out.append,
        clock=lambda: NOW,
    )


def test_games_are_scanned_separately() -> None:
    s = Storage(":memory:")
    out: list[str] = []
    cf = FakeCSFloat()
    sp = GameSkinport(NOW)
    opts = ScanOptions(games=(CS2, DOTA2), limit=2, csfloat=5)
    result = _pipeline(sp, s, out, cf).run(opts)

    assert sp.games == [CS2, DOTA2]
    # Одно и то же название в двух играх даёт две независимые возможности.
    games = sorted(o.buy_quote.app_id for o in result.found if o.result.buy_market == "dmarket")
    assert games == [DOTA2, CS2]
    assert result.pairs == 2
    # CSFloat торгует только CS2.
    assert len(cf.asked) == 1
    assert all(o.buy_quote.app_id == CS2 for o in result.found if o.result.buy_market == "csfloat")
    # Виртуальные сделки и цены записаны с игрой.
    trades = s.paper_trades()
    assert {t.app_id for t in trades} == {CS2, DOTA2}
    assert set(s.recent_quotes("skinport", NOW, DOTA2)) == {NAME, "X"}
    assert any(line.startswith("Dota 2 · Skinport:") for line in out)
    assert any(line.startswith("CS2 · CSFloat: проверено") for line in out)
    assert not any(line.startswith("Dota 2 · CSFloat") for line in out)


def test_one_broken_game_does_not_stop_the_pass() -> None:
    out: list[str] = []
    sp = GameSkinport(NOW, broken=(RUST,))
    opts = ScanOptions(games=(CS2, RUST), limit=2, paper=False)
    result = _pipeline(sp, Storage(":memory:"), out).run(opts)
    assert {o.buy_quote.app_id for o in result.found} == {CS2}
    assert "Rust · игра пропущена: Skinport /items: HTTP 503" in out


def test_all_games_broken_is_an_error() -> None:
    sp = GameSkinport(NOW, broken=(CS2, RUST))
    with pytest.raises(SourceError, match=r"CS2: .*; Rust: "):
        _pipeline(sp, Storage(":memory:"), []).scan(ScanOptions(games=(CS2, RUST)))


def test_single_game_error_is_raised_as_before() -> None:
    sp = GameSkinport(NOW, broken=(CS2,))
    with pytest.raises(SourceError, match="HTTP 503"):
        _pipeline(sp, Storage(":memory:"), []).scan(ScanOptions())


# ---------- база ----------


def test_notifications_are_per_game() -> None:
    s = Storage(":memory:")
    s.save_notification(NOW, NAME, "dmarket", "skinport", Decimal("1"), Decimal("0.1"), DOTA2)
    assert s.last_notified(NAME, "dmarket", "skinport", DOTA2) == NOW
    assert s.last_notified(NAME, "dmarket", "skinport", CS2) is None


def test_old_notifications_become_cs2(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL,"
        " market_hash_name TEXT NOT NULL, buy_market TEXT NOT NULL, sell_market TEXT NOT NULL,"
        " profit_eur TEXT NOT NULL, roi TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO notifications (sent_at, market_hash_name, buy_market, sell_market,"
        " profit_eur, roi) VALUES (?, ?, 'dmarket', 'skinport', '1', '0.1')",
        (NOW.isoformat(), NAME),
    )
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.last_notified(NAME, "dmarket", "skinport", CS2) == NOW
    assert s.last_notified(NAME, "dmarket", "skinport", DOTA2) is None
    s.close()


def test_prune_keeps_games_apart() -> None:
    s = Storage(":memory:")
    old = NOW - timedelta(days=3)
    sp = GameSkinport(old)
    s.save_quotes(_game(sp.fetch_quotes(), CS2)[:1] + _game(sp.fetch_quotes(), DOTA2)[:1])
    s.prune(NOW)
    rows = s.conn.execute("SELECT app_id FROM quotes ORDER BY app_id").fetchall()
    assert [r[0] for r in rows] == [DOTA2, CS2]


def test_cli_rejects_unknown_game() -> None:
    result = CliRunner().invoke(app, ["scan", "--no-fetch", "--games", "cs2,tf2"])
    assert result.exit_code != 0
    assert "tf2" in result.output
