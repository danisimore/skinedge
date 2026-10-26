"""Комиссии Steam Community Market.

Steam считает комиссии от суммы, которую получает продавец, а не от цены
покупателя. Алгоритм повторяет логику страницы продажи Steam
(``CalculateAmountToSendForDesiredReceivedAmount`` в economy_common.js):
каждая комиссия равна ``floor(max(получено × ставка, 1 цент))``.
Источник сторонний, поэтому правило помечено как требующее подтверждения.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from skinedge.money import Money

_MIN_FEE_MINOR = 1


def _fee_minor(received_minor: int, rate: Decimal) -> int:
    if rate == 0:
        return 0
    raw = max(Decimal(received_minor) * rate, Decimal(_MIN_FEE_MINOR))
    return int(raw.to_integral_value(rounding=ROUND_FLOOR))


def buyer_price_minor(received_minor: int, steam_rate: Decimal, game_rate: Decimal) -> int:
    """Цена для покупателя (в центах), при которой продавец получит ``received_minor``."""
    if received_minor < 0:
        raise ValueError("Сумма не может быть отрицательной")
    return (
        received_minor
        + _fee_minor(received_minor, steam_rate)
        + _fee_minor(received_minor, game_rate)
    )


def seller_receives(buyer_price: Money, steam_rate: Decimal, game_rate: Decimal) -> Money:
    """Сколько получит продавец, если покупатель заплатит ``buyer_price``.

    Возвращает наибольшую сумму, для которой цена покупателя не превышает
    ``buyer_price``. Это консервативная оценка для цен, которые нельзя
    выставить точно.

    Args:
        buyer_price: Цена, которую видит и платит покупатель.
        steam_rate: Комиссия Steam (0.05).
        game_rate: Комиссия издателя игры (0.10 для CS2).

    Returns:
        Сумма, зачисляемая продавцу в Steam Wallet.
    """
    price_minor = buyer_price.to_minor()
    if price_minor < 3:
        # Минимальная цена в Steam: 1 цент продавцу + 1 + 1 цент комиссий.
        return Money.zero(buyer_price.currency)
    lo, hi = 0, price_minor
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if buyer_price_minor(mid, steam_rate, game_rate) <= price_minor:
            lo = mid
        else:
            hi = mid - 1
    return Money.from_minor(lo, buyer_price.currency)
