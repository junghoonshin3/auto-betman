from __future__ import annotations

import logging

from src.main import _parse_sync_guild_id


def test_parse_sync_guild_id_valid() -> None:
    assert _parse_sync_guild_id("123456789") == 123456789


def test_parse_sync_guild_id_missing_or_blank() -> None:
    assert _parse_sync_guild_id(None) is None
    assert _parse_sync_guild_id("") is None
    assert _parse_sync_guild_id("   ") is None


def test_parse_sync_guild_id_invalid_logs_warning(caplog) -> None:
    caplog.set_level(logging.WARNING)

    assert _parse_sync_guild_id("abc") is None
    assert _parse_sync_guild_id("0") is None
    assert _parse_sync_guild_id("-7") is None

    text = caplog.text
    assert "Invalid DISCORD_GUILD_ID value" in text
