"""HTTP-API voor de Shortcuts (fase 2). Zie docs/ARCHITECTUUR.md 3.4.

Elk antwoord op /add en /undo heeft `status` en `speech`: `speech` is een korte
zin in de taal van de Shortcut die Siri kan uitspreken. Er is geen endpoint voor
afrekenen, bevestigen of betalen, en alleen POST /slot zet een bezorgslot.
"""

import logging
import secrets
from datetime import datetime
from importlib.metadata import version
from typing import Literal

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .choose import TERMS, decide, normalize, options_from_search, spoken_options
from .client import LoginRequired, PicnicClient, PicnicHTTPError, RateLimitExceeded
from .config import Settings
from .notify import Notifier
from .shop import NotAdded, Shop
from .store import Store

log = logging.getLogger(__name__)

Lang = Literal["nl", "en"]

_DAYS = {"nl": ["ma", "di", "wo", "do", "vr", "za", "zo"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}

TEXT = {
    "added": {"nl": "{name}, {unit}, toegevoegd.", "en": "{name}, {unit}, added."},
    "added_many": {"nl": "{count} keer {name}, {unit}, toegevoegd.", "en": "{count} times {name}, {unit}, added."},
    "top_up": {
        "nl": " De bestelling voor {slot} kan nog aangevuld worden tot {cut_off}, rond af in de app.",
        "en": " The order for {slot} can still be topped up until {cut_off}, finish it in the app.",
    },
    "choose": {"nl": "Welke {term}: {options}?", "en": "Which {term}: {options}?"},
    "choose_new": {"nl": "Nog nooit {term} gekocht. Kies: {options}.", "en": "You've never bought {term}. Pick: {options}."},
    "remember_prompt": {
        "nl": "Je kiest bij {term} steeds hetzelfde. Voortaan meteen dit nemen?",
        "en": "You keep picking the same {term}. Use this from now on?",
    },
    "not_found": {"nl": "Ik kan geen {term} vinden.", "en": "I can't find {term}."},
    "not_added": {
        "nl": "{name} kon niet worden toegevoegd, misschien is het niet leverbaar.",
        "en": "{name} couldn't be added, it may be unavailable.",
    },
    "choice_expired": {"nl": "Die keuze is verlopen, probeer het opnieuw.", "en": "That choice has expired, please try again."},
    "undone": {"nl": "{name} is weer uit het mandje.", "en": "{name} is out of the basket again."},
    "nothing_to_undo": {"nl": "Er is niets om terug te draaien.", "en": "There is nothing to undo."},
    "login_required": {
        "nl": "De Picnic-login is verlopen. Log opnieuw in op de server met picnic login.",
        "en": "The Picnic login has expired. Run picnic login on the server again.",
    },
    "picnic_error": {"nl": "Picnic reageert niet goed, probeer het straks nog eens.", "en": "Picnic isn't responding, try again later."},
}


def say(key: str, lang: str, **values) -> str:
    return TEXT[key].get(lang, TEXT[key]["nl"]).format(**values)


def when(ts: str | None, lang: str) -> str:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        dt = None
    if dt is None:
        return ts or ""
    return f"{_DAYS.get(lang, _DAYS['nl'])[dt.weekday()]} {dt:%H:%M}"


class AddBody(BaseModel):
    q: str | None = Field(None, max_length=120, description="gesproken of getypte term")
    article_id: str | None = Field(None, max_length=40)
    gtin: str | None = Field(None, max_length=20)
    choice: str | None = Field(None, max_length=300, description="label uit een eerdere keuzelijst voor dezelfde q")
    remember: bool = Field(False, description="keuze onthouden als woordenboek-term voor q")
    count: int = Field(1, ge=1, le=24)
    lang: Lang = "nl"


class RemoveBody(BaseModel):
    article_id: str = Field(..., max_length=40)
    count: int = Field(1, ge=1, le=24)


class UndoBody(BaseModel):
    lang: Lang = "nl"


class AliasBody(BaseModel):
    article_id: str = Field(..., max_length=40)


class SlotBody(BaseModel):
    slot_id: str = Field(..., max_length=60)


def create_app(settings: Settings, client: PicnicClient | None = None, store: Store | None = None) -> FastAPI:
    if not settings.api_key or len(settings.api_key) < 16:
        raise RuntimeError("API_KEY ontbreekt of is korter dan 16 tekens")
    store = store or Store(settings.db_path)
    client = client or PicnicClient(settings, interactive=False)
    shop = Shop(client, store)
    notifier = Notifier(settings, store)

    app = FastAPI(title="picnic-service", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shop = shop

    def require_key(x_api_key: str | None = Header(None)) -> None:
        if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(status_code=401, detail="ongeldige of ontbrekende X-Api-Key")

    def lang_of(request: Request) -> str:
        return getattr(request.state, "lang", "nl")

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        notifier.once_per_day("login_required", "Picnic-login is verlopen; de API kan niets toevoegen. Draai `picnic login`.")
        lang = lang_of(request)
        return JSONResponse(status_code=503, content={"status": "login_required", "speech": say("login_required", lang)})

    async def picnic_failed(request: Request, exc: Exception):
        log.warning("Picnic-call mislukt: %s: %s", type(exc).__name__, exc)
        lang = lang_of(request)
        return JSONResponse(
            status_code=502,
            content={"status": "picnic_error", "speech": say("picnic_error", lang), "detail": type(exc).__name__},
        )

    for exc_type in (PicnicHTTPError, RateLimitExceeded, requests.RequestException):
        app.add_exception_handler(exc_type, picnic_failed)

    # ---- zonder sleutel ----

    @app.get("/health")
    def health():
        with shop.lock:
            last = store.last_sync()
        return {
            "status": "ok",
            "token_cached": settings.token_file.exists(),
            "wrapper_version": version("python-picnic-api2"),
            "last_sync": dict(last) if last else None,
        }

    # ---- met sleutel ----

    @app.get("/cart", dependencies=[Depends(require_key)])
    def get_cart():
        with shop.lock:
            cart = shop.cart()
            return {**cart, "order_context": shop.order_context()}

    @app.get("/search", dependencies=[Depends(require_key)])
    def search(q: str):
        with shop.lock:
            return {"items": shop.search(q)}

    @app.post("/add", dependencies=[Depends(require_key)])
    def add(body: AddBody, request: Request):
        request.state.lang = body.lang
        lang = body.lang
        term = normalize(body.q) if body.q else None

        def _add(article_id: str, add_term: str | None, remember: bool, fallback_name: str | None = None):
            try:
                result = shop.add(article_id, body.count, add_term)
            except NotAdded as exc:
                name = fallback_name or article_id
                return {"status": "not_added", "article_id": article_id, "detail": str(exc), "speech": say("not_added", lang, name=name)}
            article = result["article"]
            if remember and add_term:
                store.set_alias(add_term, article_id, article["name"], article["unit_quantity"])
            key = "added" if body.count == 1 else "added_many"
            speech = say(key, lang, name=article["name"], unit=article["unit_quantity"] or "", count=body.count)
            try:
                context = shop.order_context()
            except Exception as exc:  # toevoegen is gelukt; zonder context alleen geen aanvulzin
                log.warning("order_context mislukt: %s", type(exc).__name__)
                context = {"can_top_up": False, "open_delivery": None}
            if context["can_top_up"]:
                delivery = context["open_delivery"]
                speech += say("top_up", lang, slot=when(delivery["slot_start"], lang), cut_off=when(delivery["cut_off"], lang))
            return {
                "status": "added",
                "article": article,
                "count": body.count,
                "remembered_as": add_term if remember and add_term else None,
                "cart": {"total_count": result["cart"]["total_count"], "total_cents": result["cart"]["total_cents"]},
                "order_context": context,
                "speech": speech,
            }

        with shop.lock:
            if body.choice:
                if not term:
                    raise HTTPException(status_code=422, detail="choice vereist q")
                article_id = store.resolve_choice(term, body.choice)
                if not article_id:
                    return {"status": "choice_expired", "speech": say("choice_expired", lang)}
                result = _add(article_id, term, remember=body.remember)
                if result["status"] == "added":
                    store.log_choice(term, article_id)
                return result
            if body.article_id:
                return _add(body.article_id, term, remember=body.remember and bool(term))
            if body.gtin:
                article = shop.client.call("get_article_by_gtin", body.gtin)
                if article is None:
                    return {"status": "not_found", "speech": say("not_found", lang, term=body.gtin)}
                return _add(article.id, None, remember=False, fallback_name=article.name)
            if not term:
                raise HTTPException(status_code=422, detail="geef q, article_id, gtin of choice")

            shop.refresh_favorites()
            decision = decide(term, lang, store)
            if decision.kind in ("alias", "winner"):
                result = _add(decision.article.article_id, term, remember=False, fallback_name=decision.article.name)
                result["decision"] = {"kind": decision.kind, "reason": decision.reason}
                return result
            speech_key = "choose"
            if decision.kind == "none":
                search_term = _search_term(term, lang)
                decision = options_from_search(term, shop.search(search_term), store)
                speech_key = "choose_new"
                if decision.kind == "none":
                    return {"status": "not_found", "term": term, "speech": say("not_found", lang, term=body.q)}
            options = [o.as_dict() for o in decision.options]
            store.save_choices(term, options)
            # "Onthouden?" alleen als deze term de laatste 3 keer steeds hetzelfde opleverde
            offer_remember = store.repeated_choice(term, 3) is not None
            return {
                "status": "needs_choice",
                "term": term,
                "reason": decision.reason,
                "labels": [o["label"] for o in options],
                "options": options,
                "offer_remember": offer_remember,
                "remember_prompt": say("remember_prompt", lang, term=body.q) if offer_remember else None,
                "speech": say(speech_key, lang, term=body.q, options=spoken_options(decision.options, lang)),
            }

    @app.post("/undo", dependencies=[Depends(require_key)])
    def undo(request: Request, body: UndoBody | None = None):
        lang = body.lang if body else "nl"
        request.state.lang = lang
        with shop.lock:
            result = shop.undo()
        if result is None:
            return {"status": "nothing_to_undo", "speech": say("nothing_to_undo", lang)}
        return {"status": "undone", "article": result["article"], "cart": result["cart"],
                "speech": say("undone", lang, name=result["article"]["name"])}

    @app.post("/remove", dependencies=[Depends(require_key)])
    def remove(body: RemoveBody):
        with shop.lock:
            return {"status": "removed", "cart": shop.remove(body.article_id, body.count)}

    @app.get("/aliases", dependencies=[Depends(require_key)])
    def aliases():
        with shop.lock:
            return {"aliases": [dict(r) for r in store.list_aliases()]}

    @app.put("/aliases/{term}", dependencies=[Depends(require_key)])
    def put_alias(term: str, body: AliasBody):
        key = normalize(term)
        with shop.lock:
            known = store.article(body.article_id)
            store.set_alias(key, body.article_id, known["name"] if known else None, known["unit_quantity"] if known else None)
            return {"status": "saved", "term": key, "article_id": body.article_id}

    @app.delete("/aliases/{term}", dependencies=[Depends(require_key)])
    def delete_alias(term: str):
        with shop.lock:
            if not store.delete_alias(normalize(term)):
                raise HTTPException(status_code=404, detail="term staat niet in het woordenboek")
        return {"status": "deleted"}

    @app.get("/orders", dependencies=[Depends(require_key)])
    def orders(limit: int = 20):
        with shop.lock:
            return {"orders": [dict(r) for r in store.recent_orders(max(1, min(limit, 100)))]}

    @app.get("/slots", dependencies=[Depends(require_key)])
    def slots():
        with shop.lock:
            return shop.slots()

    @app.post("/slot", dependencies=[Depends(require_key)])
    def set_slot(body: SlotBody):
        with shop.lock:
            try:
                return {"status": "slot_set", **shop.set_slot(body.slot_id)}
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except NotAdded as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None

    return app


def _search_term(term: str, lang: str) -> str:
    """Bij Engels zoeken we met de Nederlandse vertaling als die bekend is."""
    if lang == "en":
        translated = TERMS.get("en", {}).get(term)
        if translated:
            return translated[0]
    return term

