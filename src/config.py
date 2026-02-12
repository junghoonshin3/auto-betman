from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = PROJECT_ROOT / "storage"


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    discord_channel_id: int
    betman_user_id: str = ""
    betman_user_pw: str = ""
    headless: bool = True
    polling_interval_minutes: int = 30
    base_url: str = "https://www.betman.co.kr"
    session_state_path: Path = field(default_factory=lambda: STORAGE_DIR / "session_state.json")
    last_notified_path: Path = field(default_factory=lambda: STORAGE_DIR / "last_notified.json")
    db_path: Path = field(default_factory=lambda: STORAGE_DIR / "betman.db")

    @classmethod
    def from_env(cls, env_path: Path | str | None = None) -> Config:
        load_dotenv(dotenv_path=env_path or PROJECT_ROOT / ".env")

        required = {
            "DISCORD_BOT_TOKEN": os.getenv("DISCORD_BOT_TOKEN"),
            "DISCORD_CHANNEL_ID": os.getenv("DISCORD_CHANNEL_ID"),
        }

        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            discord_bot_token=required["DISCORD_BOT_TOKEN"],
            discord_channel_id=int(required["DISCORD_CHANNEL_ID"]),
            betman_user_id=os.getenv("BETMAN_USER_ID", ""),
            betman_user_pw=os.getenv("BETMAN_USER_PW", ""),
            headless=os.getenv("HEADLESS", "true").lower() == "true",
            polling_interval_minutes=int(os.getenv("POLLING_INTERVAL_MINUTES", "30")),
        )
