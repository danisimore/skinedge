"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки SkinEdge. Все переменные имеют префикс ``SKINEDGE_``.

    Attributes:
        db_path: Файл базы SQLite с историей цен.
        log_path: Лог фонового запуска ``skinedge run``.
        fee_rules_path: Свой файл правил комиссий вместо встроенного.
        fx_spread: Потери при конвертации валют (доля).
        dmarket_withdraw: Способ вывода с DMarket вместо заданного в правилах (visa, sepa...).
        csfloat_api_key: Ключ CSFloat API (после первой сделки на CSFloat).
        dmarket_public_key: Публичный ключ DMarket Trading API.
        dmarket_secret_key: Секретный ключ DMarket Trading API.
        telegram_bot_token: Токен Telegram-бота.
        telegram_chat_id: Чат для уведомлений.

    Ключ CSFloat, токен бота и чат читаются и под короткими именами
    ``SKINEDGE_CSFLOAT_KEY``, ``TG_BOT_TOKEN`` и ``TG_CHAT_ID``.
    """

    model_config = SettingsConfigDict(env_prefix="SKINEDGE_", env_file=".env", extra="ignore")

    db_path: Path = Path("data/skinedge.db")
    log_path: Path = Path("logs/skinedge.log")
    fee_rules_path: Path | None = None
    fx_spread: Decimal = Decimal("0.01")
    dmarket_withdraw: str | None = None
    csfloat_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("SKINEDGE_CSFLOAT_API_KEY", "SKINEDGE_CSFLOAT_KEY")
    )
    dmarket_public_key: str | None = None
    dmarket_secret_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = Field(
        None, validation_alias=AliasChoices("SKINEDGE_TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN")
    )
    telegram_chat_id: str | None = Field(
        None, validation_alias=AliasChoices("SKINEDGE_TELEGRAM_CHAT_ID", "TG_CHAT_ID")
    )
