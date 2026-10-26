"""Общая инфраструктура источников цен: лимит запросов и повторы."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from skinedge.quotes import Quote

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceError(RuntimeError):
    """Источник не вернул пригодные данные."""


class MarketSource(Protocol):
    """Источник котировок одной площадки."""

    name: str

    def fetch_quotes(self, app_id: int) -> list[Quote]:
        """Получает текущие котировки по игре."""
        ...


class RateLimiter:
    """Скользящее окно: не больше ``max_calls`` вызовов за ``period`` секунд.

    Args:
        max_calls: Сколько запросов разрешено в окне.
        period: Длина окна в секундах.
        clock: Источник монотонного времени (для тестов).
        sleep: Функция ожидания (для тестов).
    """

    def __init__(
        self,
        max_calls: int,
        period: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1 or period <= 0:
            raise ValueError("Неверные параметры лимита")
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        """Ждёт, пока в окне освободится место, и регистрирует вызов."""
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return
            wait = self.period - (now - self._calls[0])
            log.info("Лимит запросов: ждём %.1f с", wait)
            self._sleep(wait)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    limiter: RateLimiter,
    attempts: int = 3,
    backoff: float = 5.0,
    max_wait: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: object,
) -> httpx.Response:
    """Выполняет запрос с учётом лимита и повторяет при 429, 5xx и сетевых ошибках.

    Args:
        client: HTTP-клиент.
        method: HTTP-метод.
        url: Адрес.
        limiter: Лимит запросов источника.
        attempts: Максимум попыток.
        backoff: Базовая пауза; растёт вдвое с каждой попыткой.
        max_wait: Самая долгая пауза между попытками.
        sleep: Функция ожидания (для тестов).
        **kwargs: Параметры для ``httpx.Client.request``.

    Returns:
        Успешный ответ (2xx).

    Raises:
        SourceError: Если попытки исчерпаны или ответ не подлежит повтору.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        limiter.acquire()
        try:
            response = client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as exc:
            last_error = f"сетевая ошибка: {exc!r}"
            wait = backoff * 2 ** (attempt - 1)
        else:
            if response.is_success:
                return response
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in RETRY_STATUSES:
                raise SourceError(f"{method} {url}: {last_error}")
            wait = _retry_after(response) or backoff * 2 ** (attempt - 1)
        if attempt < attempts:
            wait = min(wait, max_wait)
            log.warning("%s %s: %s; повтор через %.0f с", method, url, last_error, wait)
            sleep(wait)
    raise SourceError(f"{method} {url}: попытки исчерпаны, последняя ошибка {last_error}")
