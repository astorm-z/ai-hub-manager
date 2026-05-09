from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


@dataclass(frozen=True)
class Settings:
    app_name: str = "AI 中转渠道管理"
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "3670"))
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'ai_hub_manager.db'}")
    session_secret: str = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "1") != "0"
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))


settings = Settings()
