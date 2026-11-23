"""Telegram-meldingen (fase 1).

Token en chat-id komen uit .env; zonder die waarden wordt er niets verstuurd. Het token zit
in de URL van de Bot API; daarom worden foutmeldingen van requests nooit
volledig gelogd, alleen het type.
"""

import logging
import socket
from datetime import date

import requests

from .config import Settings

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings, store=None, session: requests.Session | None = None):
        self.token = settings.telegram_token
        self.chat_id = settings.telegram_chat_id
        self.store = store
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        message = f"[picnic@{socket.gethostname()}] {text}"
        if not self.configured:
            log.warning("Telegram niet ingesteld, melding niet verstuurd: %s", text)
            return False
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Telegram-melding mislukt (%s)", type(exc).__name__)
            return False
        if response.status_code != 200:
            log.warning("Telegram-melding mislukt (HTTP %s)", response.status_code)
            return False
        return True

    def once_per_day(self, key: str, text: str) -> bool:
        """Verstuur hooguit één melding per sleutel per kalenderdag."""
        today = date.today().isoformat()
        meta_key = f"notified:{key}"
        if self.store is not None and self.store.get_meta(meta_key) == today:
            log.info("melding '%s' is vandaag al verstuurd", key)
            return False
        sent = self.send(text)
        if sent and self.store is not None:
            self.store.set_meta(meta_key, today)
        return sent
