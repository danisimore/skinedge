"""Отправка уведомлений через Telegram Bot API.

Документация: https://core.telegram.org/bots/api. Используются два метода:
``getUpdates`` (найти чат, в котором пользователь написал боту) и ``sendMessage``.

Токен бота входит в адрес запроса (``/bot<token>/method``), поэтому адрес
никогда не попадает в сообщения об ошибках и логи. Логгер httpx, который
печатает адреса запросов, CLI держит на уровне WARNING.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_LENGTH = 4096  # предел длины одного сообщения


class TelegramError(RuntimeError):
    """Telegram не принял запрос. Текст ошибки не содержит токен."""


@dataclass(frozen=True)
class Chat:
    """Чат, в котором боту писали.

    Attributes:
        id: Идентификатор чата для ``sendMessage``.
        type: ``private``, ``group`` и т.п.
        title: Имя пользователя или название группы.
        last_text: Последнее сообщение в этом чате.
    """

    id: int
    type: str
    title: str
    last_text: str


class TelegramClient:
    """Минимальный клиент Bot API.

    Args:
        token: Токен бота от @BotFather.
        client: HTTP-клиент; по умолчанию создаётся свой.
        sleep: Функция ожидания (для тестов).
    """

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token or ":" not in token:
            raise ValueError("Неверный формат токена бота: ожидается <число>:<строка>")
        self._token = token
        self._client = client or httpx.Client(timeout=30)
        self._sleep = sleep

    def _call(self, method: str, payload: dict[str, Any], attempts: int = 3) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(url, json=payload)
            except httpx.TransportError as exc:
                # repr исключения httpx может содержать адрес, поэтому только тип.
                last = f"сетевая ошибка {type(exc).__name__}"
                wait = 2.0 * attempt
            else:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if response.status_code == 200 and data.get("ok"):
                    return data.get("result")
                description = str(data.get("description") or "").replace(self._token, "***")
                last = f"HTTP {response.status_code}: {description}"
                if response.status_code == 429:
                    retry = (data.get("parameters") or {}).get("retry_after")
                    wait = float(retry) if isinstance(retry, int | float) else 5.0
                elif response.status_code >= 500:
                    wait = 2.0 * attempt
                else:
                    raise TelegramError(f"{method}: {last}")
            if attempt < attempts:
                log.warning("Telegram %s: %s; повтор через %.0f с", method, last, wait)
                self._sleep(min(wait, 60.0))
        raise TelegramError(f"{method}: попытки исчерпаны, {last}")

    def chats(self) -> list[Chat]:
        """Чаты из последних входящих сообщений боту (последнее сообщение каждого)."""
        updates = self._call("getUpdates", {"allowed_updates": ["message"]}) or []
        found: dict[int, Chat] = {}
        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if "id" not in chat:
                continue
            title = chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p
            )
            if chat.get("username"):
                title = f"{title} (@{chat['username']})".strip()
            found[int(chat["id"])] = Chat(
                id=int(chat["id"]),
                type=str(chat.get("type") or ""),
                title=title,
                last_text=str(message.get("text") or ""),
            )
        return list(found.values())

    def send(self, chat_id: str | int, text: str) -> None:
        """Отправляет сообщение в формате HTML, длинное делит на части."""
        for part in split_message(text):
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )


def split_message(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Делит текст на части не длиннее ``limit`` по границам абзацев и строк."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts
