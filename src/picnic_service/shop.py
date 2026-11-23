"""Handelingen richting Picnic voor de API (fase 2): mandje, zoeken, Favorieten, slots.

Alle calls lopen via één lock: de API draait requests in een threadpool, en de
throttle, de token-cache en de SQLite-verbinding zijn niet voor gelijktijdig
gebruik gemaakt.

Hoe Picnic werkt, en dus deze module:

- Er is altijd één mandje. `add` en `remove` wijzigen alleen het mandje.
- Afrekenen maakt van het mandje een order binnen een bezorging (slot). Dat doet
  deze dienst nooit.
- Tot de cut-off van dat slot kan een geplaatste bestelling worden aangevuld: je
  vult het mandje opnieuw en rondt in de app af, en Picnic hangt het als extra
  order aan dezelfde bezorging (daarom heeft een delivery 1 tot 5 orders).
  `order_context` meldt of dat nu kan, zodat het antwoord het kan zeggen.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from python_picnic_api2 import SearchResult

from .client import PicnicClient
from .store import Store

log = logging.getLogger(__name__)

FAVORITES_MAX_AGE = timedelta(hours=24)
CONTEXT_MAX_AGE_SECONDS = 600


class NotAdded(RuntimeError):
    """Picnic accepteerde het artikel niet (niet leverbaar of onbekend)."""


def flatten_cart(raw: dict | None) -> dict:
    raw = raw or {}
    lines = []
    for line in raw.get("items") or []:
        for article in line.get("items") or []:
            if not article.get("id"):
                continue
            count = 1
            for decorator in article.get("decorators") or []:
                if decorator.get("type") == "QUANTITY" and decorator.get("quantity"):
                    count = int(decorator["quantity"])
            lines.append({
                "article_id": article["id"],
                "name": article.get("name"),
                "unit_quantity": article.get("unit_quantity"),
                "count": count,
                "line_cents": line.get("price"),
            })
    selected = raw.get("selected_slot") or {}
    return {
        "items": lines,
        "total_count": raw.get("total_count"),
        "total_cents": raw.get("total_price"),
        "selected_slot_id": selected.get("slot_id"),
        "selected_slot_state": selected.get("state"),
    }


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


class Shop:
    def __init__(self, client: PicnicClient, store: Store):
        self.client = client
        self.store = store
        self.lock = threading.RLock()
        self._context: tuple[float, dict] | None = None

    # ---- mandje ----

    def cart(self) -> dict:
        with self.lock:
            return flatten_cart(self.client.call("get_cart").raw)

    def add(self, article_id: str, count: int, term: str | None) -> dict:
        with self.lock:
            cart = self.client.call("add_product", article_id, count)
            raw = cart.raw or {}
            flat = flatten_cart(raw)
            line = next((i for i in flat["items"] if i["article_id"] == article_id), None)
            if line is None:
                error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
                raise NotAdded(error.get("message") or "artikel niet in het mandje na toevoegen")
            self.store.log_add(article_id, line["name"], line["unit_quantity"], count, term)
            return {"article": line, "cart": flat}

    def remove(self, article_id: str, count: int) -> dict:
        with self.lock:
            return flatten_cart(self.client.call("remove_product", article_id, count).raw)

    def undo(self) -> dict | None:
        with self.lock:
            last = self.store.last_add()
            if last is None:
                return None
            cart = flatten_cart(self.client.call("remove_product", last["article_id"], last["count"]).raw)
            self.store.mark_undone(last["id"])
            return {"article": dict(last), "cart": cart}

    # ---- zoeken en Favorieten ----

    def search(self, q: str) -> list[dict]:
        with self.lock:
            result = self.client.call("search", q)
        items = []
        for item in result.items:
            known = self.store.article(item.id)
            items.append({
                "article_id": item.id,
                "name": item.name,
                "unit_quantity": item.unit_quantity,
                "price_cents": item.display_price,
                "times_bought": (known["deliveries_count"] or 0) if known else 0,
            })
        return items

    def refresh_favorites(self, force: bool = False) -> None:
        """Favorieten-pagina hooguit één keer per dag ophalen; bij een fout de oude houden."""
        fetched = _parse(self.store.favorites_fetched_at())
        if not force and fetched and datetime.now(timezone.utc) - fetched < FAVORITES_MAX_AGE:
            return
        with self.lock:
            try:
                response = self.client.raw_get("/pages/purchases-page-root", label="favorites")
            except Exception as exc:  # Favorieten zijn een hulpbron, geen harde eis
                log.warning("Favorieten ophalen mislukt: %s", type(exc).__name__)
                return
        if not response.ok or not isinstance(response.body, dict):
            log.warning("Favorieten gaven HTTP %s", response.status)
            return
        items = [
            {"article_id": i.id, "name": i.name, "unit_quantity": i.unit_quantity, "price_cents": i.display_price}
            for i in SearchResult.from_page(response.body).items
        ]
        if items:
            self.store.replace_favorites(items)
            log.info("%d Favorieten bijgewerkt", len(items))

    # ---- bestelling en slots ----

    def order_context(self) -> dict:
        """Loopt er een geplaatste bestelling die nog aangevuld kan worden? 10 minuten gecachet."""
        if self._context and time.monotonic() - self._context[0] < CONTEXT_MAX_AGE_SECONDS:
            return self._context[1]
        with self.lock:
            current = self.client.call("get_current_deliveries")
        now = datetime.now(timezone.utc)
        open_delivery = None
        for delivery in current:
            slot = delivery.slot
            cut_off = _parse(slot.cut_off_time) if slot else None
            if slot and cut_off and cut_off > now:
                if open_delivery is None or slot.window_start < open_delivery["slot_start"]:
                    open_delivery = {
                        "delivery_id": delivery.delivery_id,
                        "slot_start": slot.window_start,
                        "slot_end": slot.window_end,
                        "cut_off": slot.cut_off_time,
                    }
        context = {"can_top_up": open_delivery is not None, "open_delivery": open_delivery}
        self._context = (time.monotonic(), context)
        return context

    def slots(self) -> dict:
        with self.lock:
            data = self.client.call("get_delivery_slots")
        selected = data.selected_slot
        return {
            "selected_slot_id": selected.slot_id if selected else None,
            "selected_slot_state": selected.state if selected else None,
            "slots": [
                {"slot_id": s.slot_id, "start": s.window_start, "end": s.window_end, "cut_off": s.cut_off_time}
                for s in data.delivery_slots
                if s.is_available
            ],
        }

    def set_slot(self, slot_id: str) -> dict:
        """Zet een bezorgslot. Alleen aanroepen op expliciet verzoek van de gebruiker."""
        with self.lock:
            available = {s["slot_id"]: s for s in self.slots()["slots"]}
            if slot_id not in available:
                raise ValueError("slot is niet (meer) beschikbaar")
            response = self.client.raw_post("/cart/set_delivery_slot", {"slot_id": slot_id}, label="set_delivery_slot")
        body = response.body if isinstance(response.body, dict) else {}
        selected = (body.get("selected_slot") or {}).get("slot_id")
        if not response.ok or selected != slot_id:
            raise NotAdded(f"slot niet gezet (HTTP {response.status})")
        return {"slot": available[slot_id], "selected_slot_state": (body.get("selected_slot") or {}).get("state")}
