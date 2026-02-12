from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import Config


class TestConfigFromEnv:
    def test_missing_required_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
        monkeypatch.delenv("BETMAN_USER_ID", raising=False)
        monkeypatch.delenv("BETMAN_USER_PW", raising=False)

        with pytest.raises(ValueError, match="Missing required"):
            Config.from_env(env_file)

    def test_partial_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env"
        env_file.write_text("DISCORD_BOT_TOKEN=tok\n")
        monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("BETMAN_USER_ID", raising=False)
        monkeypatch.delenv("BETMAN_USER_PW", raising=False)

        with pytest.raises(ValueError, match="DISCORD_CHANNEL_ID"):
            Config.from_env(env_file)

    def test_valid_config_without_betman_creds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "DISCORD_BOT_TOKEN=tok\n"
            "DISCORD_CHANNEL_ID=12345\n"
        )
        for key in ("BETMAN_USER_ID", "BETMAN_USER_PW", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
            monkeypatch.delenv(key, raising=False)

        cfg = Config.from_env(env_file)
        assert cfg.discord_bot_token == "tok"
        assert cfg.discord_channel_id == 12345
        assert cfg.betman_user_id == ""
        assert cfg.betman_user_pw == ""

    def test_valid_config_with_betman_creds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "BETMAN_USER_ID=user\n"
            "BETMAN_USER_PW=pass\n"
            "DISCORD_BOT_TOKEN=tok\n"
            "DISCORD_CHANNEL_ID=12345\n"
        )
        for key in ("BETMAN_USER_ID", "BETMAN_USER_PW", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
            monkeypatch.delenv(key, raising=False)

        cfg = Config.from_env(env_file)
        assert cfg.betman_user_id == "user"
        assert cfg.betman_user_pw == "pass"
        assert cfg.discord_channel_id == 12345

    def test_defaults(self, mock_config: Config):
        assert mock_config.headless is True
        assert mock_config.polling_interval_minutes == 30
        assert mock_config.base_url == "https://www.betman.co.kr"

    def test_frozen(self, mock_config: Config):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            mock_config.headless = False  # type: ignore[misc]
