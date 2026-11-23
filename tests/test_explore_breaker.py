import pytest

pytest.importorskip("python_picnic_api2")

from picnic_service.client import PicnicHTTPError  # noqa: E402
from picnic_service.config import load_settings  # noqa: E402
from picnic_service import explore as explore_mod  # noqa: E402
from picnic_service.explore import MAX_BLOCK_PAUSES, MAX_CONSECUTIVE_FAILURES, Explorer  # noqa: E402


class FakeResponse:
    status_code = 403
    url = ""

    def json(self):
        raise ValueError("geen JSON")


class FailingClient:
    def __init__(self, status):
        self.status = status
        self.calls = 0
        self.last_response = FakeResponse()

    def call(self, method, *args, **kwargs):
        self.calls += 1
        raise PicnicHTTPError(self.status, "mislukt")


def test_article_fetch_pauses_then_stops_on_block(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sleeps = []
    monkeypatch.setattr(explore_mod.time, "sleep", sleeps.append)
    settings = load_settings()
    client = FailingClient(403)
    explorer = Explorer(client, settings)
    explorer.article_refs = {f"s{i}": {"deliveries": {"d"}, "count": 1} for i in range(50)}
    explorer._fetch_articles()
    # blokkade: pauzeren en hetzelfde artikel opnieuw, daarna stoppen zonder door te hameren
    assert client.calls == MAX_BLOCK_PAUSES + 1
    assert sleeps == [explore_mod.BLOCK_PAUSE_SECONDS] * MAX_BLOCK_PAUSES
    assert explorer.article_stats["stopped_early"] == 1
    assert explorer.steps[-1].ok is False


def test_other_errors_stop_after_consecutive_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = FailingClient(502)
    explorer = Explorer(client, load_settings())
    explorer.article_refs = {f"s{i}": {"deliveries": {"d"}, "count": 1} for i in range(50)}
    explorer._fetch_articles()
    assert client.calls == MAX_CONSECUTIVE_FAILURES


class RecoveringClient:
    """Eén blokkade, daarna weer normale artikelpagina's."""

    def __init__(self):
        self.calls = 0
        self.last_response = None

    def call(self, method, article_id, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise PicnicHTTPError(403, "blokkade")

        class Ok:
            url = f"https://x/pages/product-details-page-root?id={article_id}"
            status_code = 200

            def json(self):
                return {"id": "product-details-page-root"}

        self.last_response = Ok()
        return None


def test_article_fetch_resumes_after_block(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(explore_mod.time, "sleep", lambda s: None)
    client = RecoveringClient()
    explorer = Explorer(client, load_settings())
    explorer.article_refs = {f"s{i}": {"deliveries": {"d"}, "count": 1} for i in range(3)}
    explorer._fetch_articles()
    assert client.calls == 4
    assert explorer.article_stats["unparsed"] == 3
    assert explorer.article_stats["block_pauses"] == 1
    assert explorer.steps[-1].ok is True


def test_article_price_comes_from_order_line(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    explorer = Explorer(FailingClient(403), load_settings())
    explorer.deliveries = {"d1": {
        "delivery_id": "d1", "status": "COMPLETED",
        "slot": {"window_start": "2026-09-09T08:00:00.000+02:00"},
        "orders": [{"items": [{
            "type": "ORDER_LINE", "price": 780, "display_price": 780,
            "items": [{"type": "ORDER_ARTICLE", "id": "s1", "name": "Sojadrink", "price": 432199,
                       "decorators": [{"type": "QUANTITY", "quantity": 4}]}],
        }]}],
    }}
    explorer._collect_articles()
    ref = explorer.article_refs["s1"]
    assert ref["count"] == 4
    assert ref["prices"] == [("2026-09-09T08:00:00.000+02:00", 195)]
