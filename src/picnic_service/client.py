"""Dunne laag om python_picnic_api2.PicnicAPI.

Verzorgt login (met 2FA), token-cache in data/token.json, opnieuw inloggen bij
een auth-fout, throttling met een harde uurcap, optionele raw dumps van elke
response, en raw_get/raw_post voor paden die de wrapper niet kent.

Alle HTTP-verkeer van de wrapper loopt via session.request (requests.Session.get
en .post roepen die aan). Die methode wordt op de instantie ingepakt, zodat
throttle en dump ook gelden voor calls die we via de wrapper doen.
"""

import json
import logging
import os
import re
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from python_picnic_api2 import (
    Picnic2FAError,
    Picnic2FARequired,
    PicnicAPI,
    PicnicAuthError,
)
from python_picnic_api2.client import _HEADERS
from requests.exceptions import JSONDecodeError

from .config import Settings

log = logging.getLogger(__name__)

# Grenzen die ook via .env niet te versoepelen zijn; binnen deze grenzen is .env leidend
HARD_MIN_INTERVAL = 1.0
HARD_MAX_PER_HOUR = 1200
BLOCK_STATUSES = {403, 429}

# Requests naar deze paden worden nooit gedumpt (login-verkeer)
_NO_DUMP_PATHS = ("/user/login", "/user/2fa/")
_AUTH_ERROR_CODES = {"AUTH_ERROR", "AUTH_INVALID_CRED"}
_API_PREFIX = re.compile(r"^/api/\d+")


class RateLimitExceeded(RuntimeError):
    """De uurcap is bereikt; retry_after is het aantal seconden tot er ruimte is."""

    def __init__(self, retry_after: float):
        super().__init__(f"uurcap bereikt, weer ruimte over {retry_after:.0f} s")
        self.retry_after = retry_after


class LoginRequired(RuntimeError):
    """Inloggen lukt niet zonder tussenkomst (ontbrekende gegevens of 2FA)."""


class Unauthorized(RuntimeError):
    """HTTP 401 van Picnic buiten de login-flow."""


class PicnicHTTPError(RuntimeError):
    """Picnic blokkeert (429, HTML-403), heeft een storing (5xx) of gaf geen JSON terug."""

    def __init__(self, status: int | None, message: str, retry_after: str | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


@dataclass
class RawResponse:
    status: int
    path: str
    body: Any  # dict of list bij JSON, anders tekst

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_private(path: Path, text: str) -> None:
    """Schrijf een bestand dat alleen de eigenaar mag lezen (mode 600)."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def api_path(url: str) -> str:
    """Pad zonder host en zonder /api/<versie>-prefix."""
    return _API_PREFIX.sub("", urlsplit(url).path)


def slugify(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", path.split("?", 1)[0]).strip("_") or "root"


def parse_body(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def is_block(response) -> bool:
    """Picnic knijpt af: 429, of 403 met een HTML-pagina van de webserver.

    Een echte API-weigering komt als JSON; de kale nginx-403 is de firewall die op
    tempo reageert (gezien op 16 sept 2026 na ruim 150 artikelpagina's op 1 per s).
    """
    if response.status_code == 429:
        return True
    content_type = response.headers.get("Content-Type") or ""
    return response.status_code == 403 and "text/html" in content_type


def is_auth_error(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    return isinstance(error, dict) and error.get("code") in _AUTH_ERROR_CODES


class Throttle:
    """Minimale tussenpauze plus schuivend uurvenster, bewaard op schijf.

    Het venster staat in data/ratelimit.json zodat opeenvolgende runs (explore,
    later sync en api) samen onder de cap blijven.
    """

    def __init__(self, state_file: Path, min_interval: float, max_per_hour: int):
        self.state_file = state_file
        self.min_interval = max(HARD_MIN_INTERVAL, min_interval)
        self.max_per_hour = min(HARD_MAX_PER_HOUR, max_per_hour)
        if self.min_interval != min_interval or self.max_per_hour != max_per_hour:
            log.warning(
                "throttle uit .env begrensd tot %.1f s per call en %d per uur",
                self.min_interval, self.max_per_hour,
            )
        self.stamps: deque[float] = deque(self._load())

    def _load(self) -> list[float]:
        try:
            data = json.loads(self.state_file.read_text())
            return sorted(float(t) for t in data.get("stamps", []))
        except FileNotFoundError:
            return []
        except (ValueError, TypeError, AttributeError):
            log.warning("ratelimit-bestand onleesbaar, begin met leeg venster")
            return []

    def _save(self) -> None:
        write_private(self.state_file, json.dumps({"stamps": list(self.stamps)}))

    def _prune(self, now: float) -> None:
        while self.stamps and self.stamps[0] <= now - 3600:
            self.stamps.popleft()

    def acquire(self) -> None:
        now = time.time()
        self._prune(now)
        if len(self.stamps) >= self.max_per_hour:
            raise RateLimitExceeded(self.stamps[0] + 3600 - now)
        if self.stamps:
            wait = self.stamps[-1] + self.min_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
        self.stamps.append(now)
        self._save()

    @property
    def used_last_hour(self) -> int:
        self._prune(time.time())
        return len(self.stamps)


class PicnicClient:
    def __init__(self, settings: Settings, interactive: bool = False):
        self.settings = settings
        self.interactive = interactive
        ensure_private_dir(settings.data_dir)
        self.throttle = Throttle(
            settings.ratelimit_file, settings.min_interval, settings.max_per_hour
        )
        self.requests_made = 0
        self.last_response = None
        self._label: str | None = None
        self._saved_token = self._load_token()
        self.api = self._new_api(self._saved_token)

    # ---- sessie en instrumentatie ----

    def _new_api(self, token: str | None) -> PicnicAPI:
        # Zonder username/password logt de constructor niet zelf in
        api = PicnicAPI(country_code=self.settings.country, auth_token=token)
        self._instrument(api.session)
        return api

    def _instrument(self, session) -> None:
        original = session.request

        def request(method, url, *args, **kwargs):
            self.throttle.acquire()
            response = original(method, url, *args, **kwargs)
            self.requests_made += 1
            self.last_response = response
            path = api_path(url)
            log.debug("%s %s -> %s", method, path, response.status_code)
            login_traffic = any(p in path for p in _NO_DUMP_PATHS)
            if not login_traffic:
                self._dump(method, url, response)
                if response.status_code == 401:
                    raise Unauthorized(f"401 op {path}")
                if is_block(response) or response.status_code >= 500:
                    log.warning(
                        "HTTP %s op %s (Retry-After: %s)",
                        response.status_code, path, response.headers.get("Retry-After"),
                    )
                    raise PicnicHTTPError(
                        response.status_code, f"op {path}", response.headers.get("Retry-After")
                    )
            return response

        session.request = request

    @contextmanager
    def label(self, name: str):
        """Bepaalt de map onder data/raw waarin responses van dit blok landen."""
        previous = self._label
        self._label = name
        try:
            yield
        finally:
            self._label = previous

    def _dump(self, method: str, url: str, response) -> None:
        if not self.settings.raw_dump:
            return
        parts = urlsplit(url)
        label = self._label or slugify(api_path(url))
        now = datetime.now(timezone.utc)
        envelope = {
            "meta": {
                "label": label,
                "method": method,
                "host": parts.hostname,
                "path": api_path(url),
                "query": parts.query,
                "status": response.status_code,
                "content_type": response.headers.get("Content-Type"),
                "location": response.headers.get("Location"),
                "fetched_at": now.isoformat(),
            },
            "body": parse_body(response),
        }
        target = self.settings.raw_dir / label / f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        try:
            write_private(target, json.dumps(envelope, ensure_ascii=False, indent=2))
        except OSError as exc:
            log.warning("dump naar %s mislukt: %s", target, exc)

    # ---- token ----

    def _load_token(self) -> str | None:
        try:
            data = json.loads(self.settings.token_file.read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            log.warning("token-cache onleesbaar, opnieuw inloggen nodig")
            return None
        if data.get("country") != self.settings.country:
            return None
        return data.get("auth_token") or None

    def _save_token(self) -> None:
        token = self.api.session.auth_token
        if not token or token == self._saved_token:
            return
        payload = {
            "auth_token": token,
            "country": self.settings.country,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        write_private(self.settings.token_file, json.dumps(payload, indent=2))
        self._saved_token = token
        log.info("token-cache bijgewerkt")

    def _clear_token(self) -> None:
        self.settings.token_file.unlink(missing_ok=True)
        self._saved_token = None

    # ---- login ----

    def login(self) -> None:
        s = self.settings
        if not (s.username and s.password):
            raise LoginRequired("PICNIC_USERNAME of PICNIC_PASSWORD ontbreekt in .env")
        self.api = self._new_api(None)
        log.info("inloggen bij Picnic (%s)", s.country)
        try:
            self.api.login(s.username, s.password)
        except Picnic2FARequired:
            if not self.interactive:
                raise LoginRequired(
                    "Picnic vraagt een 2FA-code; draai `picnic login` in een terminal"
                ) from None
            self._complete_2fa()
        if not self.api.session.authenticated:
            raise LoginRequired("login gaf geen token terug")
        self._save_token()
        log.info("ingelogd")

    def _complete_2fa(self) -> None:
        channel = self.settings.twofa_channel
        self.api.generate_2fa_code(channel)
        print(f"Picnic heeft een code gestuurd via {channel}.")
        for _ in range(3):
            code = input("2FA-code: ").strip()
            try:
                self.api.verify_2fa_code(code)
                return
            except Picnic2FAError as exc:
                print(f"Code geweigerd ({exc.code}), probeer opnieuw.")
        raise LoginRequired("2FA mislukt na 3 pogingen")

    def ensure_login(self) -> None:
        if not self.api.session.authenticated:
            self.login()

    def _relogin(self, reason: str) -> None:
        log.warning("%s, opnieuw inloggen", reason)
        self._clear_token()
        self.login()

    # ---- calls ----

    def call(self, method: str, *args, label: str | None = None, **kwargs) -> Any:
        """Roep een wrapper-methode aan, met een nieuwe login bij een auth-fout."""
        self.ensure_login()
        for attempt in (1, 2):
            try:
                with self.label(label or method):
                    result = getattr(self.api, method)(*args, **kwargs)
                self._save_token()
                return result
            except (PicnicAuthError, Unauthorized) as exc:
                if attempt == 2:
                    raise
                self._relogin(f"auth-fout bij {method} ({type(exc).__name__})")
            except JSONDecodeError:
                # De wrapper parset blind; een lege of HTML-body betekent meestal een blokkade
                status = self.last_response.status_code if self.last_response is not None else None
                raise PicnicHTTPError(status, f"geen JSON in antwoord op {method}") from None
        raise AssertionError("onbereikbaar")

    def raw_get(self, path: str, *, label: str | None = None) -> RawResponse:
        return self._raw("GET", path, None, label)

    def raw_post(self, path: str, body: Any = None, *, label: str | None = None) -> RawResponse:
        return self._raw("POST", path, body, label)

    def _raw(self, method: str, path: str, body: Any, label: str | None) -> RawResponse:
        """Request op een pad onder de API-base-URL, met dezelfde headers als de wrapper."""
        self.ensure_login()
        for attempt in (1, 2):
            url = self.api._base_url + path
            session = self.api.session
            try:
                with self.label(label or "raw_" + slugify(path)):
                    if method == "GET":
                        response = session.get(url, headers=dict(_HEADERS))
                    else:
                        response = session.post(url, json=body, headers=dict(_HEADERS))
            except Unauthorized:
                if attempt == 2:
                    raise
                self._relogin(f"401 op {path}")
                continue
            parsed = parse_body(response)
            if is_auth_error(parsed) and attempt == 1:
                self._relogin(f"auth-fout op {path}")
                continue
            self._save_token()
            return RawResponse(status=response.status_code, path=path, body=parsed)
        raise AssertionError("onbereikbaar")
