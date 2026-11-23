from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("python_picnic_api2")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from helpers import make_delivery  # noqa: E402
from python_picnic_api2 import Cart, DeliverySlots, DeliverySummary, SearchResult  # noqa: E402

from picnic_service.api import create_app  # noqa: E402
from picnic_service.client import LoginRequired, RawResponse  # noqa: E402
from picnic_service.config import load_settings  # noqa: E402
from picnic_service.store import Store  # noqa: E402

KEY = "test-key-1234567890"
NOW = datetime.now(timezone.utc)
NAMES = {
    "s1": ("Picnic bio volle melk", "1 liter"),
    "s2": ("Tony's Chocolonely melk", "180 gram"),
    "s4": ("Picnic scharreleieren", "10 stuks M"),
    "s7": ("Picnic havermout", "500 gram"),
}


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


class FakeClient:
    def __init__(self):
        self.cart = {}
        self.calls = []
        self.current = []
        self.fail = None
        self.slots = [{"slot_id": "slotA", "window_start": ago(-2), "window_end": ago(-2), "cut_off_time": ago(-1), "is_available": True}]
        self.selected = {"slot_id": "slotA", "state": "IMPLICIT"}

    def _cart(self):
        items = [
            {"type": "ORDER_LINE", "id": aid, "price": 100 * n, "items": [
                {"type": "ORDER_ARTICLE", "id": aid, "name": NAMES[aid][0], "unit_quantity": NAMES[aid][1],
                 "decorators": [{"type": "QUANTITY", "quantity": n}]}]}
            for aid, n in self.cart.items()
        ]
        return {"type": "ORDER", "items": items, "total_count": sum(self.cart.values()),
                "total_price": 100 * sum(self.cart.values()), "selected_slot": self.selected}

    def call(self, method, *args, **kwargs):
        self.calls.append(method)
        if self.fail:
            raise self.fail
        if method == "get_cart":
            return Cart.from_api(self._cart())
        if method == "add_product":
            aid, n = args
            if aid not in NAMES:
                return Cart.from_api({"error": {"code": "NOT_FOUND", "message": "onbekend"}})
            self.cart[aid] = self.cart.get(aid, 0) + n
            return Cart.from_api(self._cart())
        if method == "remove_product":
            aid, n = args
            self.cart[aid] = self.cart.get(aid, 0) - n
            if self.cart[aid] <= 0:
                del self.cart[aid]
            return Cart.from_api(self._cart())
        if method == "get_current_deliveries":
            return [DeliverySummary.from_api(d) for d in self.current]
        if method == "search":
            page = {"body": {"child": {"children": [
                {"type": "SELLING_UNIT_TILE", "sellingUnit": {"id": "s7", "name": "Picnic havermout", "unit_quantity": "500 gram", "display_price": 129}}
            ]}}}
            return SearchResult.from_page(page)
        if method == "get_delivery_slots":
            return DeliverySlots.from_api({"delivery_slots": self.slots, "selected_slot": self.selected})
        raise AssertionError(method)

    def raw_get(self, path, label=None):
        self.calls.append("raw_get " + path)
        return RawResponse(status=404, path=path, body={"error": {"code": "NOT_FOUND"}})

    def raw_post(self, path, body=None, label=None):
        self.calls.append("raw_post " + path)
        assert path == "/cart/set_delivery_slot"
        self.selected = {"slot_id": body["slot_id"], "state": "EXPLICIT"}
        return RawResponse(status=200, path=path, body=self._cart())


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", KEY)
    settings = load_settings()
    store = Store(settings.db_path)
    melk = [("s1", *NAMES["s1"], 1, 149), ("s2", *NAMES["s2"], 1, 459)]
    for i in range(4):
        store.upsert_delivery(make_delivery(f"m{i}", ago(20 + i * 10), melk))
    for i in range(6):
        store.upsert_delivery(make_delivery(f"e{i}", ago(15 + i * 10), [("s4", *NAMES["s4"], 1, 313)]))
    store.recompute_derived()
    client = FakeClient()
    app = create_app(settings, client=client, store=store)
    http = TestClient(app, headers={"X-Api-Key": KEY})
    return http, client, store


def test_key_required(api):
    http, _, _ = api
    assert http.post("/add", json={"q": "eieren"}, headers={"X-Api-Key": "fout"}).status_code == 401
    assert TestClient(http.app).get("/health").status_code == 200


def test_no_checkout_endpoint(api):
    http, _, _ = api
    for path in ("/checkout", "/order", "/confirm", "/pay"):
        assert http.post(path, json={}).status_code == 404


def test_clear_winner_is_added_and_spoken(api):
    http, client, _ = api
    body = http.post("/add", json={"q": "eieren"}).json()
    assert body["status"] == "added"
    assert body["article"]["article_id"] == "s4"
    assert body["speech"] == "Picnic scharreleieren, 10 stuks M, toegevoegd."
    en = http.post("/add", json={"q": "eggs", "lang": "en"}).json()
    assert en["speech"].endswith("added.")
    assert client.cart["s4"] == 2


def test_ambiguous_asks_then_remembers(api):
    http, client, store = api
    first = http.post("/add", json={"q": "melk"}).json()
    assert first["status"] == "needs_choice"
    assert first["speech"] in ("Welke melk: Picnic bio volle of Tony's Chocolonely?", "Welke melk: Tony's Chocolonely of Picnic bio volle?")
    assert first["offer_remember"] is False
    assert "add_product" not in client.calls
    label = next(o["label"] for o in first["options"] if o["article_id"] == "s1")

    chosen = http.post("/add", json={"q": "melk", "choice": label, "remember": True}).json()
    assert chosen["status"] == "added"
    assert chosen["remembered_as"] == "melk"
    assert store.get_alias("melk")["article_id"] == "s1"

    again = http.post("/add", json={"q": "melk"}).json()
    assert again["status"] == "added"
    assert again["decision"]["kind"] == "alias"


def test_unknown_term_offers_search_results(api):
    http, client, _ = api
    body = http.post("/add", json={"q": "havermout"}).json()
    assert body["status"] == "needs_choice"
    assert body["speech"].startswith("Nog nooit havermout gekocht")
    assert body["options"][0]["article_id"] == "s7"
    assert "add_product" not in client.calls


def test_undo_removes_last_add(api):
    http, client, _ = api
    http.post("/add", json={"q": "eieren"})
    body = http.post("/undo", json={"lang": "nl"}).json()
    assert body["status"] == "undone"
    assert "s4" not in client.cart
    assert http.post("/undo", json={}).json()["status"] == "nothing_to_undo"


def test_top_up_hint_when_order_is_open(api):
    http, client, _ = api
    start = (NOW + timedelta(days=2)).replace(hour=17, minute=0)
    client.current = [make_delivery("open1", start.isoformat(), [], status="CURRENT", cut_off=(NOW + timedelta(days=1)).isoformat())]
    body = http.post("/add", json={"q": "eieren"}).json()
    assert body["order_context"]["can_top_up"] is True
    assert "kan nog aangevuld worden" in body["speech"]


def test_login_required_gives_spoken_error(api):
    http, client, _ = api
    client.fail = LoginRequired("2FA nodig")
    response = http.post("/add", json={"q": "eieren", "lang": "en"})
    assert response.status_code == 503
    assert response.json()["speech"].startswith("The Picnic login has expired")


def test_slot_only_set_on_explicit_request(api):
    http, client, _ = api
    http.post("/add", json={"q": "eieren"})
    http.get("/cart")
    assert not any(c.startswith("raw_post") for c in client.calls)
    assert http.post("/slot", json={"slot_id": "nope"}).status_code == 409
    body = http.post("/slot", json={"slot_id": "slotA"}).json()
    assert body["status"] == "slot_set"


def test_remember_is_only_offered_after_repeated_same_choice(api):
    http, client, store = api
    full = None
    for i in range(3):
        ask = http.post("/add", json={"q": "melk"}).json()
        assert ask["offer_remember"] is False
        full = next(o["full_label"] for o in ask["options"] if o["article_id"] == "s2")
        assert http.post("/add", json={"q": "melk", "choice": full}).json()["status"] == "added"
    ask = http.post("/add", json={"q": "melk"}).json()
    assert ask["offer_remember"] is True
    assert ask["remember_prompt"]


def test_alternating_choices_never_offer_remember(api):
    http, _, _ = api
    for article in ("s1", "s2", "s1", "s2"):
        ask = http.post("/add", json={"q": "melk"}).json()
        assert ask["offer_remember"] is False
        label = next(o["label"] for o in ask["options"] if o["article_id"] == article)
        http.post("/add", json={"q": "melk", "choice": label})
