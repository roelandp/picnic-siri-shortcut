"""Instellingen uit .env en de omgeving."""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    country: str
    twofa_channel: str
    data_dir: Path
    raw_dump: bool
    min_interval: float
    max_per_hour: int
    log_level: str
    explore_gtin: str
    telegram_token: str
    telegram_chat_id: str
    api_key: str
    api_host: str
    api_port: int
    api_min_interval: float

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def explore_dir(self) -> Path:
        return self.data_dir / "explore"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "picnic.db"

    @property
    def ratelimit_file(self) -> Path:
        return self.data_dir / "ratelimit.json"


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    # .env in de werkmap; bestaande omgevingsvariabelen (systemd EnvironmentFile) winnen
    load_dotenv(Path.cwd() / ".env", override=False)
    data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = Path.cwd() / data_dir
    return Settings(
        username=os.getenv("PICNIC_USERNAME", ""),
        password=os.getenv("PICNIC_PASSWORD", ""),
        country=os.getenv("PICNIC_COUNTRY", "NL").upper(),
        twofa_channel=os.getenv("PICNIC_2FA_CHANNEL", "SMS").upper(),
        data_dir=data_dir,
        raw_dump=_flag(os.getenv("RAW_DUMP", "0")),
        min_interval=float(os.getenv("MIN_REQUEST_INTERVAL", "3.0")),
        max_per_hour=int(os.getenv("MAX_REQUESTS_PER_HOUR", "600")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        explore_gtin=os.getenv("EXPLORE_GTIN", "5449000000996"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        api_key=os.getenv("API_KEY", ""),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        api_port=int(os.getenv("API_PORT", "8085")),
        api_min_interval=float(os.getenv("API_MIN_REQUEST_INTERVAL", "1.0")),
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # urllib3 logt volledige URL's op DEBUG; dat is prima, maar geen headers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
