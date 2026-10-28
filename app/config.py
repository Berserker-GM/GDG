"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///data/leaderboard.db"
    leaderboard_mode: str = "auto"
    leaderboard_size: int = 50
    min_success_ratio: float = 0.6
    max_other_badge_ratio: float = 0.5
    scoring_config: str = "scoring.yaml"
    run_lock_timeout_hours: float = 2.0

    contact_email: str = "organizers@example.edu"
    user_agent: str = ""
    fetch_concurrency: int = 3
    fetch_delay_seconds: float = 1.5
    fetch_timeout_seconds: float = 20.0
    fetch_max_retries: int = 3
    fetch_backoff_base: float = 2.0

    admin_token: str = ""
    cors_origins: list[str] = field(default_factory=list)

    enable_scheduler: bool = True
    refresh_cron: str = "0 2 * * *"
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        contact = _env("CONTACT_EMAIL", cls.contact_email)
        mode = _env("LEADERBOARD_MODE", "auto").lower()
        if mode not in {"auto", "team", "solo"}:
            raise ValueError(f"LEADERBOARD_MODE must be auto, team or solo, got {mode!r}")
        return cls(
            database_url=_env("DATABASE_URL", cls.database_url),
            leaderboard_mode=mode,
            leaderboard_size=int(_env("LEADERBOARD_SIZE", "50")),
            min_success_ratio=float(_env("MIN_SUCCESS_RATIO", "0.6")),
            max_other_badge_ratio=float(_env("MAX_OTHER_BADGE_RATIO", "0.5")),
            scoring_config=_env("SCORING_CONFIG", cls.scoring_config),
            run_lock_timeout_hours=float(_env("RUN_LOCK_TIMEOUT_HOURS", "2")),
            contact_email=contact,
            user_agent=_env("USER_AGENT", f"SkillLabLeaderboard/1.0 (+mailto:{contact})"),
            fetch_concurrency=max(1, int(_env("FETCH_CONCURRENCY", "3"))),
            fetch_delay_seconds=float(_env("FETCH_DELAY_SECONDS", "1.5")),
            fetch_timeout_seconds=float(_env("FETCH_TIMEOUT_SECONDS", "20")),
            fetch_max_retries=int(_env("FETCH_MAX_RETRIES", "3")),
            fetch_backoff_base=float(_env("FETCH_BACKOFF_BASE", "2.0")),
            admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
            cors_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
            enable_scheduler=_bool(os.getenv("ENABLE_SCHEDULER"), True),
            refresh_cron=_env("REFRESH_CRON", cls.refresh_cron),
            timezone=_env("TIMEZONE", cls.timezone),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
