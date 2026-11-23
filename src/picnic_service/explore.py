"""Fase 0: alles wat de API teruggeeft één keer ophalen, dumpen en samenvatten.

Volgorde is gekozen op het request-budget (uurcap uit .env): eerst de goedkope
overzichtscalls, dan de bestelhistorie, dan kandidaat-paden, en als laatste het
artikeldetail (het grootste blok). Bestellingen en artikelen worden gecached in
data/cache, zodat een tweede run alleen nog ophaalt wat ontbreekt.
"""

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from python_picnic_api2 import Article, Delivery

from .client import BLOCK_STATUSES, PicnicClient, PicnicHTTPError, RateLimitExceeded, write_private
from .config import Settings
from .schema import build_schema

log = logging.getLogger(__name__)

# Statussen waarna een bestelling niet meer verandert; die hoeven niet opnieuw
FINAL_STATUSES = {"COMPLETED", "CANCELLED"}

# Zoveel mislukte artikel-calls op rij en we stoppen: Picnic knijpt dan af of blokkeert
MAX_CONSECUTIVE_FAILURES = 3

# Bij een blokkade (HTML-403 of 429): zo lang pauzeren en hetzelfde artikel opnieuw proberen.
# Op 16 sept 2026 hield een blokkade 75 tot 90 s aan. Blijft hij na zoveel pauzes: stoppen.
BLOCK_PAUSE_SECONDS = 300
MAX_BLOCK_PAUSES = 2

# Paden die de wrapper niet kent. Alleen GET, niets dat iets wijzigt of met
# checkout te maken heeft. {delivery_id} en {article_id} worden ingevuld.
# Bewust niet (meer) opgevraagd, alleen persoons- of betaalgegevens:
# /user-info, /profile-menu, /payment-profile (zie docs/API-BEVINDINGEN.md).
CANDIDATE_PATHS: list[tuple[str, str]] = [
    ("/my_store?depth=0", "oude categorieboom, opvolger van get_categories"),
    ("/my_store?depth=1", "idem, dieper"),
    ("/lists", "lijsten (favorieten, eerder gekocht)"),
    ("/lists?depth=1", "idem met inhoud"),
    ("/suggest?search_term=melk", "zoeksuggesties"),
    ("/messages", "berichten"),
    ("/reminders", "herinneringen"),
    ("/parcels", "pakketjes"),
    ("/mgm", "member-get-member"),
    ("/wallet/transactions?pageNumber=1&pageSize=20", "betaalhistorie, mogelijk bonnetjes"),
    ("/consent-settings", "toestemmingen"),
    ("/deliveries", "ongesummariseerde deliveries (oude API)"),
    ("/deliveries/{delivery_id}/scenario", "rit-scenario"),
    ("/deliveries/{delivery_id}/position", "positie bezorger"),
    ("/deliveries/{delivery_id}/rating", "beoordeling"),
    ("/deliveries/{delivery_id}/invoice", "factuur"),
    ("/deliveries/{delivery_id}/receipt", "bonnetje"),
    ("/articles/{article_id}", "artikel als domein-JSON (oude API)"),
    ("/articles/{article_id}/category", "categorie (wrapper noemt dit verwijderd)"),
    ("/pages/home-page-root", "startpagina (PML)"),
    ("/pages/search-page-root", "zoekpagina zonder term (PML)"),
    ("/pages/purchases-page-root", "eerder gekocht (PML)"),
    ("/pages/order-history-page-root", "bestelhistorie (PML)"),
    ("/pages/delivery-details-page-root?delivery_id={delivery_id}", "bestelling als pagina (PML)"),
    ("/pages/receipt-page-root?delivery_id={delivery_id}", "bonnetje als pagina (PML)"),
    ("/pages/L1-category-page-root", "categorieën als pagina (PML)"),
]


class StopExploration(Exception):
    """Uurcap bereikt en er mag niet gewacht worden."""


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Explorer:
    def __init__(
        self,
        client: PicnicClient,
        settings: Settings,
        wait: bool = True,
        max_articles: int | None = None,
        skip_articles: bool = False,
        skip_candidates: bool = False,
        verify_slot: bool = False,
    ):
        self.client = client
        self.settings = settings
        self.wait = wait
        self.max_articles = max_articles
        self.skip_articles = skip_articles
        self.skip_candidates = skip_candidates
        self.verify_slot = verify_slot
        self.steps: list[StepResult] = []
        self.candidates: list[dict] = []
        self.slot_check: dict | None = None
        self.summaries: list = []
        self.deliveries: dict[str, dict] = {}
        self.article_refs: dict[str, dict] = {}
        self.article_stats = Counter()

    # ---- hulpfuncties ----

    def _retry_on_cap(self, fn: Callable[[], Any], force_wait: bool = False) -> Any:
        while True:
            try:
                return fn()
            except RateLimitExceeded as exc:
                if not (self.wait or force_wait):
                    raise StopExploration(str(exc)) from None
                log.warning("uurcap bereikt, wacht %.0f s", exc.retry_after)
                time.sleep(exc.retry_after + 1)

    def _step(self, name: str, fn: Callable[[], Any], record: bool = True) -> Any:
        try:
            result = self._retry_on_cap(fn)
        except StopExploration:
            raise
        except Exception as exc:  # verkennen: elke fout noteren en doorgaan
            log.warning("%s faalde: %s: %s", name, type(exc).__name__, exc)
            if record:
                self.steps.append(StepResult(name, False, f"{type(exc).__name__}: {exc}"))
            return None
        if record:
            self.steps.append(StepResult(name, True))
        return result

    def _cache_path(self, kind: str, item_id: str):
        return self.settings.cache_dir / kind / f"{item_id}.json"

    def _read_cache(self, kind: str, item_id: str) -> dict | None:
        path = self._cache_path(kind, item_id)
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _write_cache(self, kind: str, item_id: str, payload: dict) -> None:
        write_private(self._cache_path(kind, item_id), json.dumps(payload, ensure_ascii=False))

    # ---- stappen ----

    def run(self) -> dict:
        started = _now_stamp()
        c = self.client
        self._retry_on_cap(c.ensure_login)

        user = self._step("get_user", lambda: c.call("get_user"))
        self._step("get_cart", lambda: c.call("get_cart"))
        self.summaries = self._step("get_deliveries", lambda: c.call("get_deliveries")) or []
        self._step("get_current_deliveries", lambda: c.call("get_current_deliveries"))
        self._step("get_delivery_slots", lambda: c.call("get_delivery_slots"))
        # De wrapper gooit NotImplementedError zonder request; noteren is genoeg
        self._step("get_categories", lambda: c.call("get_categories"))
        self._step("search_melk", lambda: c.call("search", "melk", label="search"))

        self._fetch_deliveries()
        self._step("get_article_by_gtin", lambda: c.call("get_article_by_gtin", self.settings.explore_gtin))
        if self.verify_slot:
            self._step("verify_slot", self._verify_slot)
        if not self.skip_candidates:
            self._probe_candidates()
        if not self.skip_articles:
            self._fetch_articles()

        summary = self._summary(user, started)
        self._write_outputs(summary)
        return summary

    def _fetch_deliveries(self) -> None:
        ids = [s.delivery_id for s in self.summaries if s.delivery_id]
        log.info("%d bestellingen in de historie", len(ids))
        fetched = cached = 0
        for i, delivery_id in enumerate(ids, 1):
            entry = self._read_cache("delivery", delivery_id)
            if entry and entry.get("raw", {}).get("status") in FINAL_STATUSES:
                self.deliveries[delivery_id] = entry["raw"]
                cached += 1
                continue
            delivery = self._step(
                f"get_delivery {delivery_id}", lambda: self.client.call("get_delivery", delivery_id), record=False
            )
            if delivery is None or delivery.raw is None:
                self.steps.append(StepResult(f"get_delivery {delivery_id}", False, "geen resultaat"))
                continue
            self.deliveries[delivery_id] = delivery.raw
            self._write_cache("delivery", delivery_id, {"fetched_at": _now_stamp(), "raw": delivery.raw})
            fetched += 1
            if i % 10 == 0:
                log.info("bestellingen: %d/%d", i, len(ids))
        self.steps.append(StepResult("get_delivery (alle)", True, f"{fetched} opgehaald, {cached} uit cache"))
        self._collect_articles()

    def _collect_articles(self) -> None:
        for delivery_id, raw in self.deliveries.items():
            delivery = Delivery.from_api(raw)
            when = (delivery.slot.window_start if delivery.slot else None) or delivery.creation_time
            for order in delivery.orders:
                for line in order.items:
                    # ORDER_ARTICLE.price is een vaste placeholder (432199); de echte prijs staat
                    # op de orderregel, voor alle stuks samen
                    line_units = sum(
                        next((d.quantity for d in a.decorators if d.type == "QUANTITY" and d.quantity), 1)
                        for a in line.items
                    )
                    for article in line.items:
                        if not article.id:
                            continue
                        qty = next((d.quantity for d in article.decorators if d.type == "QUANTITY" and d.quantity), 1)
                        ref = self.article_refs.setdefault(
                            article.id,
                            {"name": article.name, "unit_quantity": article.unit_quantity,
                             "deliveries": set(), "count": 0, "prices": [], "last_seen": None},
                        )
                        ref["deliveries"].add(delivery_id)
                        ref["count"] += qty
                        if line.price is not None and line_units and len(line.items) == 1:
                            ref["prices"].append((when, round(line.price / line_units)))
                        if when and (ref["last_seen"] is None or when > ref["last_seen"]):
                            ref["last_seen"] = when

    def _verify_slot(self) -> None:
        """Eén keer een ander slot zetten, controleren en direct terugzetten."""
        c = self.client
        check: dict[str, Any] = {"started_at": _now_stamp()}
        self.slot_check = check
        # Genoeg budget vooraf, zodat de cap niet tussen zetten en terugzetten valt
        if c.throttle.max_per_hour - c.throttle.used_last_hour < 15:
            check["skipped"] = "te weinig request-budget over dit uur"
            log.warning("slot-verificatie overgeslagen: %s", check["skipped"])
            return
        cart = c.call("get_cart", label="slot_check_cart_before")
        previous = cart.selected_slot.slot_id if cart.selected_slot else None
        check["previous_slot"] = previous
        check["previous_state"] = cart.selected_slot.state if cart.selected_slot else None
        slots = c.call("get_delivery_slots", label="slot_check_slots")
        candidates = [s.slot_id for s in slots.delivery_slots if s.is_available and s.slot_id and s.slot_id != previous]
        check["available_slots"] = len(candidates)
        if not candidates:
            check["skipped"] = "geen beschikbaar ander slot"
            return
        target = candidates[0]
        check["test_slot"] = target
        try:
            response = c.raw_post("/cart/set_delivery_slot", {"slot_id": target}, label="set_delivery_slot")
            check["set_status"] = response.status
            check["set_body_keys"] = sorted(response.body)[:30] if isinstance(response.body, dict) else type(response.body).__name__
            after = c.call("get_cart", label="slot_check_cart_after_set")
            check["selected_after_set"] = after.selected_slot.slot_id if after.selected_slot else None
            check["state_after_set"] = after.selected_slot.state if after.selected_slot else None
            check["set_works"] = check["selected_after_set"] == target
        finally:
            self._restore_slot(check, previous)
        log.info("slot-verificatie: %s", {k: v for k, v in check.items() if k != "set_body_keys"})

    def _restore_slot(self, check: dict, previous: str | None) -> None:
        c = self.client
        attempts: list[dict] = []
        check["restore_attempts"] = attempts
        bodies = [{"slot_id": previous}] if previous else [{"slot_id": None}, {}]
        for body in bodies:
            try:
                response = self._retry_on_cap(
                    lambda: c.raw_post("/cart/set_delivery_slot", body, label="set_delivery_slot_restore"),
                    force_wait=True,
                )
                cart = self._retry_on_cap(lambda: c.call("get_cart", label="slot_check_cart_after_restore"), force_wait=True)
                selected = cart.selected_slot.slot_id if cart.selected_slot else None
                attempts.append({"body": body, "status": response.status, "selected_after": selected,
                                 "state_after": cart.selected_slot.state if cart.selected_slot else None})
                if selected == previous:
                    check["restored"] = True
                    return
            except Exception as exc:
                attempts.append({"body": body, "error": f"{type(exc).__name__}: {exc}"})
        check["restored"] = False
        log.error(
            "SLOT NIET TERUGGEZET: huidige selectie wijkt af van de oorspronkelijke (%s). "
            "Controleer het mandje in de Picnic-app.", previous or "geen slot",
        )

    def _probe_candidates(self) -> None:
        delivery_id = next((s.delivery_id for s in self.summaries if s.delivery_id), None)
        article_id = next(iter(self.article_refs), None)
        values = {"delivery_id": delivery_id, "article_id": article_id}
        for template, note in CANDIDATE_PATHS:
            if ("{delivery_id}" in template and not delivery_id) or ("{article_id}" in template and not article_id):
                self.candidates.append({"path": template, "note": note, "status": None, "detail": "geen id beschikbaar"})
                continue
            path = template.format(**values)
            label = "raw_" + template.split("?", 1)[0].strip("/").replace("/", "_").replace("{", "").replace("}", "")
            response = self._step(f"raw_get {template}", lambda: self.client.raw_get(path, label=label), record=False)
            if response is None:
                self.candidates.append({"path": template, "note": note, "status": None, "detail": "exception"})
                continue
            self.candidates.append({"path": template, "note": note, "status": response.status,
                                    "detail": _describe(response.body)})
        ok = sum(1 for x in self.candidates if x["status"] == 200)
        self.steps.append(StepResult("kandidaat-paden", True, f"{ok}/{len(self.candidates)} gaven 200"))

    def _fetch_articles(self) -> None:
        ids = sorted(self.article_refs, key=lambda a: -len(self.article_refs[a]["deliveries"]))
        if self.max_articles is not None:
            ids = ids[: self.max_articles]
        log.info("%d unieke artikelen, detail ophalen (cache waar mogelijk)", len(ids))
        consecutive_failures = 0
        block_pauses = 0
        i = 0
        while i < len(ids):
            article_id = ids[i]
            if self._read_cache("article", article_id):
                self.article_stats["cached"] += 1
                i += 1
                continue
            try:
                article = self._retry_on_cap(lambda: self.client.call("get_article", article_id))
                error = None
            except StopExploration:
                raise
            except Exception as exc:  # verkennen: noteren, stroomonderbreker beslist
                article = None
                error = exc
            response = self.client.last_response
            raw = None
            if error is None and response is not None and f"id={article_id}" in (response.url or ""):
                try:
                    raw = response.json()
                except ValueError:
                    raw = None
            if raw is None:
                status = getattr(error, "status", None) or (response.status_code if response is not None else None)
                log.warning("get_article %s faalde (HTTP %s): %s", article_id, status, error)
                blocked = isinstance(error, PicnicHTTPError) and error.status in BLOCK_STATUSES
                if blocked and block_pauses < MAX_BLOCK_PAUSES:
                    # Blokkade duurde in de praktijk 1 tot 2 minuten; wachten en hetzelfde artikel opnieuw
                    block_pauses += 1
                    self.article_stats["block_pauses"] += 1
                    log.warning("Picnic blokkeert, pauze %d s (%d/%d)", BLOCK_PAUSE_SECONDS, block_pauses, MAX_BLOCK_PAUSES)
                    time.sleep(BLOCK_PAUSE_SECONDS)
                    continue
                self.article_stats["failed"] += 1
                consecutive_failures += 1
                if blocked or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    detail = f"gestopt, laatste HTTP {status}, {block_pauses} blokkadepauzes"
                    log.error("artikeldetail %s; draai later opnieuw, de cache blijft staan", detail)
                    self.article_stats["stopped_early"] = 1
                    self.steps.append(StepResult("get_article (alle)", False, f"{detail}; {dict(self.article_stats)}"))
                    return
                i += 1
                continue
            consecutive_failures = 0
            block_pauses = 0
            self.article_stats["parsed" if article else "unparsed"] += 1
            self._write_cache("article", article_id, {
                "fetched_at": _now_stamp(),
                "parsed": article.model_dump() if article else None,
                "category_ids": Article.category_ids_from_page(raw),
                "raw": raw,
            })
            i += 1
            if i % 25 == 0:
                log.info("artikelen: %d/%d", i, len(ids))
        self.steps.append(StepResult("get_article (alle)", True, repr(dict(self.article_stats))))

    # ---- uitvoer ----

    def _summary(self, user, started: str) -> dict:
        statuses = Counter(s.status for s in self.summaries)
        moments = sorted(
            t for s in self.summaries
            if (t := (s.slot.window_start if s.slot else None) or s.creation_time)
        )
        spent = sum(
            o.total_price or 0 for s in self.summaries if s.status == "COMPLETED" for o in s.orders
        )
        top = sorted(self.article_refs.items(), key=lambda kv: (-len(kv[1]["deliveries"]), -kv[1]["count"]))[:15]
        return {
            "started_at": started,
            "finished_at": _now_stamp(),
            "requests_made": self.client.requests_made,
            "user_counts": {
                "total_deliveries": getattr(user, "total_deliveries", None),
                "completed_deliveries": getattr(user, "completed_deliveries", None),
            },
            "deliveries": {
                "summaries": len(self.summaries),
                "details": len(self.deliveries),
                "statuses": dict(statuses),
                "first": moments[0] if moments else None,
                "last": moments[-1] if moments else None,
                "completed_total_cents": spent,
            },
            "articles": {
                "unique": len(self.article_refs),
                "units_total": sum(r["count"] for r in self.article_refs.values()),
                "detail": dict(self.article_stats),
                "top": [
                    {"id": aid, "name": r["name"], "unit_quantity": r["unit_quantity"],
                     "deliveries": len(r["deliveries"]), "units": r["count"],
                     "price_min": min((p for _, p in r["prices"]), default=None),
                     "price_max": max((p for _, p in r["prices"]), default=None)}
                    for aid, r in top
                ],
            },
            "slot_check": self.slot_check,
            "candidates": self.candidates,
            "steps": [asdict(s) for s in self.steps],
        }

    def _write_outputs(self, summary: dict) -> None:
        out_dir = self.settings.explore_dir
        today = date.today().isoformat()
        write_private(out_dir / f"summary-{today}.json", json.dumps(summary, ensure_ascii=False, indent=2))
        write_schema(self.settings, summary)


def _describe(body: Any) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            return f"error {body['error'].get('code')}"
        return "dict: " + ", ".join(list(body)[:12])
    if isinstance(body, list):
        return f"list[{len(body)}]"
    text = str(body).strip().replace("\n", " ")
    return f"tekst: {text[:60]}"


def candidate_section(candidates: list[dict]) -> str:
    lines = ["## Kandidaat-paden (raw_get)", "", "| pad | status | toelichting | inhoud |", "|---|---|---|---|"]
    for c in candidates:
        lines.append(f"| `{c['path']}` | {c['status']} | {c['note']} | {c['detail'].replace('|', '/')} |")
    return "\n".join(lines)


def write_schema(settings: Settings, summary: dict | None = None) -> str:
    """Schrijf data/explore/schema-<datum>.md en geef het pad terug."""
    extra = []
    if summary and summary.get("candidates"):
        extra.append(candidate_section(summary["candidates"]))
    text = build_schema(settings.raw_dir, extra)
    target = settings.explore_dir / f"schema-{date.today().isoformat()}.md"
    write_private(target, text)
    return str(target)


def format_report(summary: dict) -> str:
    d = summary["deliveries"]
    a = summary["articles"]
    lines = [
        "",
        "Samenvatting fase 0",
        "-------------------",
        f"Requests deze run:      {summary['requests_made']}",
        f"Bestellingen (summary): {d['summaries']}  (details: {d['details']})",
        f"Statussen:              {d['statuses']}",
        f"Datumbereik:            {d['first']}  t/m  {d['last']}",
        f"Totaal COMPLETED:       EUR {d['completed_total_cents'] / 100:.2f}",
        f"Unieke artikelen:       {a['unique']}  (stuks totaal {a['units_total']}, detail {a['detail']})",
        "",
        "Vaakst gekocht (aantal bestellingen / stuks):",
    ]
    for t in a["top"]:
        lines.append(f"  {t['deliveries']:>3} / {t['units']:>4}  {t['name']} ({t['unit_quantity']})")
    failed = [s for s in summary["steps"] if not s["ok"]]
    if failed:
        lines.append("")
        lines.append("Mislukte stappen:")
        lines.extend(f"  {s['name']}: {s['detail']}" for s in failed)
    if summary.get("slot_check"):
        lines.append("")
        lines.append(f"Slot-verificatie: {summary['slot_check']}")
    ok = [c["path"] for c in summary["candidates"] if c["status"] == 200]
    if summary["candidates"]:
        lines.append("")
        lines.append(f"Kandidaat-paden met 200: {ok}")
    return "\n".join(lines)
