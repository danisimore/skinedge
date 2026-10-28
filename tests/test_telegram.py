import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import skinedge.cli as cli
from skinedge.cli import app
from skinedge.notify.message import format_digest
from skinedge.notify.telegram import TelegramClient, TelegramError, split_message
from skinedge.storage import Storage

TOKEN = "123456:SECRET-token"
UPDATES = {
    "ok": True,
    "result": [
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 777, "type": "private", "first_name": "Danil", "username": "dn"},
                "text": "/start",
            },
        },
        {"update_id": 2, "edited_message": {}},
    ],
}


def make(handler: Any) -> tuple[TelegramClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return TelegramClient(TOKEN, client, sleep=lambda s: None), seen


def ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/getUpdates"):
        return httpx.Response(200, json=UPDATES)
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})


def test_rejects_bad_token() -> None:
    with pytest.raises(ValueError, match="токена"):
        TelegramClient("nonsense")


def test_send_payload() -> None:
    bot, seen = make(ok)
    bot.send(777, "<b>hi</b>")
    req = seen[0]
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == 777 and body["text"] == "<b>hi</b>"
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}


def test_long_message_is_split() -> None:
    bot, seen = make(ok)
    bot.send(1, "\n\n".join("x" * 3000 for _ in range(3)))
    assert len(seen) == 3


def test_error_hides_token() -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"ok": False, "error_code": 401, "description": f"Unauthorized {TOKEN}"}
        )

    bot, seen = make(bad)
    with pytest.raises(TelegramError) as info:
        bot.send(1, "x")
    assert TOKEN not in str(info.value) and "SECRET" not in str(info.value)
    assert "401" in str(info.value)
    assert len(seen) == 1  # 401 не повторяется


def test_network_error_hides_token() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    bot, _ = make(broken)
    with pytest.raises(TelegramError) as info:
        bot.send(1, "x")
    assert "SECRET" not in str(info.value)


def test_rate_limit_retry() -> None:
    calls = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "description": "Too Many", "parameters": {"retry_after": 3}}
            )
        return ok(request)

    bot, _ = make(limited)
    bot.send(1, "x")
    assert len(calls) == 2


def test_chats() -> None:
    bot, _ = make(ok)
    chats = bot.chats()
    assert len(chats) == 1
    assert chats[0].id == 777 and chats[0].title == "Danil (@dn)" and chats[0].last_text == "/start"


def test_split_message() -> None:
    assert split_message("short") == ["short"]
    parts = split_message("a" * 10 + "\n\n" + "b" * 10, limit=12)
    assert parts == ["a" * 10, "b" * 10]
    long_line = split_message("c" * 25, limit=10)
    assert all(len(p) <= 10 for p in long_line) and "".join(long_line) == "c" * 25


# ---------- CLI ----------


@pytest.fixture
def mock_bot(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    def factory(token: str) -> TelegramClient:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return TelegramClient(token, client, sleep=lambda s: None)

    monkeypatch.setattr(cli, "TelegramClient", factory)
    return seen


def test_cli_setup_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_bot: list[httpx.Request]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"TG_BOT_TOKEN={TOKEN}", encoding="utf-8")
    result = CliRunner().invoke(app, ["telegram", "setup", "--save"])
    assert result.exit_code == 0, result.output
    assert "777" in result.output and TOKEN not in result.output
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        f"TG_BOT_TOKEN={TOKEN}\nSKINEDGE_TELEGRAM_CHAT_ID=777\n"
    )
    again = CliRunner().invoke(app, ["telegram", "setup", "--save"])
    assert "уже задан" in again.output
    assert (tmp_path / ".env").read_text(encoding="utf-8").count("CHAT_ID") == 1


def test_cli_test_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_bot: list[httpx.Request]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TG_CHAT_ID", "777")
    result = CliRunner().invoke(app, ["telegram", "test"])
    assert result.exit_code == 0, result.output
    assert json.loads(mock_bot[0].content)["chat_id"] == "777"


def test_cli_scan_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_bot: list[httpx.Request]
) -> None:
    from test_scanner import q, stats

    db = tmp_path / "x.db"
    now = datetime.now(UTC)
    s = Storage(db)
    s.save_quotes([q("skinport", "14.01", at=now), q("dmarket", "10.00", at=now)])
    s.save_sales([stats("13.00", 10, at=now)])
    s.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKINEDGE_DB_PATH", str(db))
    monkeypatch.setenv("TG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TG_CHAT_ID", "777")
    args = ["scan", "--no-fetch", "--usd-per-eur", "1.10", "--rub-per-eur", "90", "--skinport"]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "Отправлено в Telegram: 1" in first.output
    text = json.loads(mock_bot[0].content)["text"]
    assert "AK-47 | Redline (Field-Tested)" in text and "2.77 EUR" in text
    second = CliRunner().invoke(app, args)
    assert "Новых возможностей для уведомления нет" in second.output
    assert len(mock_bot) == 1


def test_digest_escapes_html() -> None:
    from test_scanner import NOW, q, scanner, stats

    name = "Sticker | <b>x</b> & co"
    found = scanner().scan(
        "dmarket",
        "skinport",
        {name: q("dmarket", "10.00", name=name)},
        {name: q("skinport", "14.01", name=name)},
        {name: stats("13.00", 10, name=name)},
        NOW,
    )
    text = format_digest(found, 3)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; co" in text
    assert "новых возможностей 1 (всего в скане 3)" in text
    assert "ничего не покупает" in text
