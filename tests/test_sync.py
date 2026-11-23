import json
from pathlib import Path

import pytest

pytest.importorskip("python_picnic_api2")

from python_picnic_api2 import Delivery, DeliverySummary  # noqa: E402

from picnic_service.client import LoginRequired  # noqa: E402
from picnic_service.config import load_settings  # noqa: E402
from picnic_service.notify import Notifier  # noqa: E402
from picnic_service.store import Store  # noqa: E402
from picnic_service.sync import DeliveryCache, run_sync  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "deliveries"


def delivery(name, delivery_id=None, start=None):
    raw = json.loads((FIX / f"{name}.json").read_text())
    if delivery_id:
        raw["delivery_id"] = raw["id"] = delivery_id
        for order in raw["orders"]:
            order["id"] = f"{delivery_id}-{order['id']}"
    if start:
        raw["slot"]["window_start"] = start
    return raw


class FakeClient:
    def __init__(self, raws, fail=None):
        self.raws = {r["delivery_id"]: r for r in raws}
        self.fail = fail
        self.fetched = []

    def call(self, method, *args, **kwargs):
        if self.fail:
            raise self.fail
        if method == "get_deliveries":
            return [DeliverySummary.from_api({k: v for k, v in r.items() if k != "decorators"}) for r in self.raws.values()]
        if method == "get_delivery":
            self.fetched.append(args[0])
            return Delivery.from_api(self.raws[args[0]])
        raise AssertionError(method)


class FakeNotifier(Notifier):
    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.sent = []

    @property
    def configured(self):
        return True

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = load_settings()
    store = Store(settings.db_path)
    yield settings, store
    store.close()


def history():
    raws = [delivery("completed", f"old{i:02d}", f"2025-0{1 + i % 9}-0{1 + i % 9}T17:00:00.000+02:00") for i in range(8)]
    raws.append(delivery("current", "new01", "2026-09-20T17:00:00.000+02:00"))
    return raws


def test_second_sync_adds_nothing_and_refetches_only_newest(env):
    settings, store = env
    client = FakeClient(history())
    first = run_sync(client, store, settings, FakeNotifier(settings, store))
    assert first.status == "ok"
    assert first.fetched == 9
    counts = store.counts()

    client.fetched.clear()
    second = run_sync(client, store, settings, FakeNotifier(settings, store))
    assert second.status == "ok"
    assert len(client.fetched) == 5
    assert store.counts() == counts


def test_first_sync_uses_explore_cache(env):
    settings, store = env
    raws = history()
    cache = DeliveryCache(settings)
    for raw in raws:
        if raw["status"] == "COMPLETED":
            cache.write(raw["delivery_id"], raw)
    client = FakeClient(raws)
    result = run_sync(client, store, settings, FakeNotifier(settings, store))
    assert result.from_cache == 4
    assert len(client.fetched) == 5
    assert store.counts()["deliveries"] == 9


def test_login_required_notifies_once_per_day(env):
    settings, store = env
    notifier = FakeNotifier(settings, store)
    client = FakeClient([], fail=LoginRequired("2FA nodig"))
    for _ in range(2):
        result = run_sync(client, store, settings, notifier)
        assert result.status == "login_required"
    assert len(notifier.sent) == 1
    assert "picnic login" in notifier.sent[0]
    status = store.conn.execute("SELECT status FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert status == "login_required"
