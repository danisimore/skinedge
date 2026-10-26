from pathlib import Path

import pytest

from skinedge.config import Settings


@pytest.mark.parametrize(
    ("csfloat", "telegram"),
    [
        ("SKINEDGE_CSFLOAT_API_KEY", "SKINEDGE_TELEGRAM_BOT_TOKEN"),
        ("SKINEDGE_CSFLOAT_KEY", "TG_BOT_TOKEN"),
    ],
)
def test_key_names(
    csfloat: str, telegram: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # без настоящего .env
    monkeypatch.setenv(csfloat, "k1")
    monkeypatch.setenv(telegram, "t1")
    s = Settings()
    assert s.csfloat_api_key is not None and s.csfloat_api_key.get_secret_value() == "k1"
    assert s.telegram_bot_token is not None and s.telegram_bot_token.get_secret_value() == "t1"


def test_env_file_short_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SKINEDGE_CSFLOAT_KEY=k2\nTG_BOT_TOKEN=t2\n", encoding="utf-8")
    s = Settings()
    assert s.csfloat_api_key is not None and s.csfloat_api_key.get_secret_value() == "k2"
    assert s.telegram_bot_token is not None and s.telegram_bot_token.get_secret_value() == "t2"
