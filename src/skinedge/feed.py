"""Найденные сделки для интерфейса: итог последних проходов сканера.

После каждого прохода фоновый сканер записывает в базу итог прохода и лучшие
возможности по каждой просканированной игре, а интерфейс показывает их
карточками. Игры, которые в этом проходе не сканировались (см.
:func:`skinedge.runner.games_for_pass`), сохраняют сделки своего последнего
прохода. Сделки старше двух часов не показываются: цены уже могли уйти.

Сделка, найденная и в прошлом проходе своей игры, сохраняет время, когда её
нашли впервые: так интерфейс отличает новые карточки.

Картинки предметов отдаёт DMarket (список предложений с ``withImages``,
см. :meth:`skinedge.sources.dmarket.DMarketSource.fetch_image`). Картинка
ищется один раз на предмет и запоминается в базе, не больше нескольких поисков
за проход; если её не нашлось, следующая попытка через неделю.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from skinedge.fees.schedule import Payout
from skinedge.scanner import Opportunity
from skinedge.sources.base import SourceError
from skinedge.storage import Storage

log = logging.getLogger(__name__)

PASS_KEY = "feed.pass"
DEALS_KEY = "feed.deals"
PER_GAME = 20
MAX_AGE = timedelta(hours=2)
IMAGE_LOOKUPS = 8
IMAGE_RETRY = timedelta(days=7)

# Откуда интерфейс может загружать картинки (их же разрешает CSP страницы).
IMAGE_HOSTS = ("steamcommunity-a.akamaihd.net",)

ImageFetch = Callable[[str, int], str | None]


def image_url(value: Any) -> str | None:
    """Ссылка на картинку, если она https и с разрешённого адреса."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    parts = urlsplit(value)
    return value if parts.scheme == "https" and parts.hostname in IMAGE_HOSTS else None


def _link(value: str | None) -> str | None:
    return value if value and value.startswith("https://") else None


def key(app_id: int, name: str, buy_market: str, sell_market: str) -> str:
    """Ключ сделки: предмет и маршрут."""
    return f"{app_id}|{name}|{buy_market}|{sell_market}"


def card(opp: Opportunity, found_at: datetime, first_seen: datetime) -> dict[str, Any]:
    """Сделка в виде, который показывает интерфейс."""
    r = opp.result
    app_id = opp.buy_quote.app_id
    return {
        "key": key(app_id, opp.name, r.buy_market, r.sell_market),
        "app_id": app_id,
        "name": opp.name,
        "buy_market": r.buy_market,
        "sell_market": r.sell_market,
        "buy_price": str(r.buy_price.amount),
        "buy_currency": r.buy_price.currency.value,
        "sell_price": str(r.sell_price.amount),
        "sell_currency": r.sell_price.currency.value,
        "cost": str(r.total_cost.amount),
        "proceeds": str(r.net_proceeds.amount),
        "profit": str(r.profit.amount),
        "currency": r.profit.currency.value,
        "roi": str(r.roi),
        "margin": None if r.safety_margin is None else str(r.safety_margin),
        "lock_days": r.lock_days,
        "wallet": r.payout is Payout.STEAM_WALLET,
        "complete": r.complete,
        "basis": opp.sell.basis,
        "flags": list(opp.flags),
        "buy_url": _link(opp.buy_url),
        "sell_url": _link(opp.sell_url),
        "image": None,
        "found_at": found_at.isoformat(),
        "first_seen": first_seen.isoformat(),
    }


def _load_json(storage: Storage, name: str) -> Any:
    raw = storage.get_meta(name)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("Запись %s в базе повреждена, пропускаю", name)
        return None


def record_pass(storage: Storage, summary: dict[str, Any]) -> None:
    """Запоминает итог прохода (номер, время, сколько найдено или ошибку)."""
    storage.set_meta(PASS_KEY, json.dumps(summary, ensure_ascii=False))


def _stored_deals(storage: Storage) -> list[dict[str, Any]]:
    data = _load_json(storage, DEALS_KEY)
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("app_id"), int)]


def update_deals(
    storage: Storage,
    at: datetime,
    games: Iterable[int],
    found: list[Opportunity],
    fetch_image: ImageFetch | None,
    now: datetime,
) -> None:
    """Заменяет сделки просканированных игр найденными в этом проходе.

    Args:
        storage: База.
        at: Время прохода.
        games: Игры, которые сканировались в этом проходе.
        found: Найденные возможности, лучшие первыми.
        fetch_image: Поиск картинки предмета; ``None`` — картинки не искать.
        now: Текущее время UTC.
    """
    scanned = set(games)
    old = _stored_deals(storage)
    seen = {d.get("key"): d.get("first_seen") for d in old}
    kept = [d for d in old if d["app_id"] not in scanned]
    fresh: list[dict[str, Any]] = []
    per_game: dict[int, int] = {}
    for opp in found:
        app_id = opp.buy_quote.app_id
        if app_id not in scanned or per_game.get(app_id, 0) >= PER_GAME:
            continue
        per_game[app_id] = per_game.get(app_id, 0) + 1
        item = card(opp, at, at)
        first = seen.get(item["key"])
        if isinstance(first, str):
            item["first_seen"] = first
        fresh.append(item)
    deals = fresh + kept
    _attach_images(storage, deals, fetch_image, now)
    storage.set_meta(DEALS_KEY, json.dumps(deals, ensure_ascii=False))


def _attach_images(
    storage: Storage, deals: list[dict[str, Any]], fetch: ImageFetch | None, now: datetime
) -> None:
    budget = IMAGE_LOOKUPS if fetch is not None else 0
    for deal in deals:
        app_id, name = deal["app_id"], deal.get("name")
        if not isinstance(name, str):
            continue
        saved = storage.image(app_id, name)
        if saved is not None and (saved[0] is not None or now - saved[1] < IMAGE_RETRY):
            deal["image"] = image_url(saved[0])
            continue
        if budget <= 0 or fetch is None:
            continue
        budget -= 1
        try:
            url = image_url(fetch(name, app_id))
        except SourceError as exc:
            log.warning("Картинки предметов не загружены: %s", exc)
            budget = 0  # площадка не отвечает: следующая попытка в следующем проходе
            continue
        storage.save_image(app_id, name, url, now)
        deal["image"] = url


def load(storage: Storage, now: datetime) -> dict[str, Any]:
    """Итог последнего прохода и сделки не старше :data:`MAX_AGE`, лучшие первыми."""
    summary = _load_json(storage, PASS_KEY)
    deals = []
    for d in _stored_deals(storage):
        try:
            found_at = datetime.fromisoformat(str(d.get("found_at")))
        except ValueError:
            continue
        if now - found_at <= MAX_AGE:
            deals.append(d)
    deals.sort(key=lambda d: (not d.get("complete"), -float(d.get("roi") or 0)))
    return {"last": summary if isinstance(summary, dict) else None, "deals": deals}
