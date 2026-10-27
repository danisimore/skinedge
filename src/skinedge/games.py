"""Игры, которые сканирует SkinEdge, и их Steam app_id."""

from __future__ import annotations

CS2 = 730
DOTA2 = 570
RUST = 252490

# Короткие имена для командной строки: --games cs2,dota2,rust
GAMES = {"cs2": CS2, "dota2": DOTA2, "rust": RUST}
TITLES = {CS2: "CS2", DOTA2: "Dota 2", RUST: "Rust"}


def title(app_id: int) -> str:
    """Название игры для сообщений."""
    return TITLES.get(app_id, f"app_id {app_id}")


def parse_games(text: str) -> tuple[int, ...]:
    """Список игр из строки вида ``cs2,dota2,rust``.

    Raises:
        ValueError: Если игра неизвестна или список пуст.
    """
    games: list[int] = []
    for part in text.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in GAMES:
            raise ValueError(f"неизвестная игра {part.strip()!r}, доступны: {', '.join(GAMES)}")
        if GAMES[key] not in games:
            games.append(GAMES[key])
    if not games:
        raise ValueError("не указано ни одной игры")
    return tuple(games)
