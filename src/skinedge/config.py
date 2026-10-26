"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки SkinEdge. Все переменные имеют префикс ``SKINEDGE_``.

    Attributes:
        fee_rules_path: Свой файл правил комиссий вместо встроенного.
        fx_spread: Потери при конвертации валют (доля).
        csfloat_api_key: Ключ CSFloat API (понадобится на этапе 3).
        telegram_bot_token: Токен Telegram-бота (этап 6).
        telegram_chat_id: Чат для уведомлений (этап 6).
    """

    model_config = SettingsConfigDict(env_prefix="SKINEDGE_", env_file=".env", extra="ignore")

    fee_rules_path: Path | None = None
    fx_spread: Decimal = Decimal("0.01")
    csfloat_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
