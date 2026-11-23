"""Bestelhistorie synchroniseren naar SQLite (fase 1). Zie docs/ARCHITECTUUR.md 3.3."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import LoginRequired, PicnicClient, write_private
from .config import Settings
from .notify import Notifier
from .store import FINAL_STATUSES, Store

log = logging.getLogger(__name__)

# De nieuwste deliveries altijd opnieuw ophalen: status en inhoud kunnen nog wijzigen
REFRESH_NEWEST = 5


@dataclass
class SyncResult:
    status: str  # ok | login_required | error
    seen: int = 0
    fetched: int = 0
    from_cache: int = 0
    lines: int = 0
    message: str | None = None


class DeliveryCache:
    """Leest en schrijft data/cache/delivery/<id>.json, hetzelfde formaat als explore."""

    def __init__(self, settings: Settings):
        self.dir = settings.cache_dir / "delivery"

    def read(self, delivery_id: str) -> dict | None:
        try:
            return json.loads((self.dir / f"{delivery_id}.json").read_text())["raw"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def write(self, delivery_id: str, raw: dict) -> None:
        payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "raw": raw}
        write_private(self.dir / f"{delivery_id}.json", json.dumps(payload, ensure_ascii=False))


def _slot_start(summary) -> str:
    return (summary.slot.window_start if summary.slot else None) or summary.creation_time or ""


def run_sync(client: PicnicClient, store: Store, settings: Settings, notifier: Notifier) -> SyncResult:
    run_id = store.start_sync()
    result = SyncResult(status="ok")
    cache = DeliveryCache(settings)
    try:
        summaries = [s for s in client.call("get_deliveries") if s.delivery_id]
        result.seen = len(summaries)
        newest = {s.delivery_id for s in sorted(summaries, key=_slot_start, reverse=True)[:REFRESH_NEWEST]}
        known = store.delivery_statuses()

        for summary in summaries:
            delivery_id = summary.delivery_id
            stored_status = known.get(delivery_id)
            if stored_status in FINAL_STATUSES and delivery_id not in newest:
                continue
            raw = None
            if delivery_id not in newest and summary.status in FINAL_STATUSES:
                cached = cache.read(delivery_id)
                if cached and cached.get("status") in FINAL_STATUSES:
                    raw = cached
                    result.from_cache += 1
            if raw is None:
                delivery = client.call("get_delivery", delivery_id)
                raw = delivery.raw
                result.fetched += 1
                if raw.get("status") in FINAL_STATUSES:
                    cache.write(delivery_id, raw)
            result.lines += store.upsert_delivery(raw)

        store.recompute_derived()
        result.message = f"{result.seen} gezien, {result.fetched} opgehaald, {result.from_cache} uit cache"
        log.info("sync klaar: %s, %d regels", result.message, result.lines)
    except LoginRequired as exc:
        result.status = "login_required"
        result.message = str(exc)
        log.error("sync gestopt, login nodig: %s", exc)
        notifier.once_per_day(
            "login_required",
            "Picnic-login is verlopen, de nachtelijke sync is overgeslagen. Draai `picnic login` op de server.",
        )
    except Exception as exc:  # nachtelijke run: noteren, melden, morgen opnieuw
        result.status = "error"
        result.message = f"{type(exc).__name__}: {exc}"
        log.exception("sync mislukt")
        notifier.once_per_day("sync_error", f"Picnic-sync mislukt: {type(exc).__name__}. Zie journalctl -u picnic-sync.")
    finally:
        store.finish_sync(run_id, result.status, result.seen, result.fetched, result.lines, result.message)
    return result
