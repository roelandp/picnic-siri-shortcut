import json
from pathlib import Path

from picnic_service.store import Store

FIX = Path(__file__).parent / "fixtures" / "deliveries"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def rows(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_upsert_twice_gives_no_duplicates(tmp_path):
    store = Store(tmp_path / "picnic.db")
    for _ in range(2):
        store.upsert_delivery(load("completed"))
        store.upsert_delivery(load("current"))
        store.recompute_derived()
    assert rows(store, "deliveries") == 2
    assert rows(store, "orders") == 3
    assert rows(store, "order_lines") == 9
    assert rows(store, "delivery_issues") == 1
    first = store.counts()
    store.recompute_derived()
    assert store.counts() == first


def test_database_file_is_private(tmp_path):
    Store(tmp_path / "picnic.db")
    assert oct((tmp_path / "picnic.db").stat().st_mode & 0o777) == "0o600"


def test_unit_price_comes_from_order_line(tmp_path):
    store = Store(tmp_path / "picnic.db")
    store.upsert_delivery(load("completed"))
    row = store.conn.execute(
        "SELECT quantity, line_cents, unit_cents, discount_cents, discount_text FROM order_lines WHERE name = 'Picnic kokosyoghurt'"
    ).fetchone()
    assert row["quantity"] == 2
    assert row["line_cents"] == 370
    assert row["unit_cents"] == 185
    assert row["discount_cents"] == 366
    assert row["discount_text"] == "BundelBonus"
    assert store.conn.execute("SELECT COUNT(*) FROM order_lines WHERE unit_cents = 432199").fetchone()[0] == 0


def test_payment_details_are_not_stored(tmp_path):
    store = Store(tmp_path / "picnic.db")
    store.upsert_delivery(load("completed"))
    raw = store.conn.execute("SELECT raw_json FROM deliveries").fetchone()[0]
    assert "transaction_info" not in raw
    assert "iban" not in raw.lower()


def test_status_change_updates_delivery_and_replaces_lines(tmp_path):
    store = Store(tmp_path / "picnic.db")
    current = load("current")
    store.upsert_delivery(current)
    assert store.delivery_statuses()["testdel002"] == "CURRENT"
    current["status"] = "COMPLETED"
    current["orders"][0]["items"] = current["orders"][0]["items"][:1]
    store.upsert_delivery(current)
    assert store.delivery_statuses()["testdel002"] == "COMPLETED"
    assert store.conn.execute("SELECT COUNT(*) FROM order_lines WHERE delivery_id = 'testdel002'").fetchone()[0] == 1


def test_articles_only_count_completed_deliveries(tmp_path):
    store = Store(tmp_path / "picnic.db")
    store.upsert_delivery(load("completed"))
    store.upsert_delivery(load("current"))
    store.recompute_derived()
    melk = store.conn.execute("SELECT * FROM articles WHERE name = 'Picnic bio volle melk'").fetchone()
    assert melk["deliveries_count"] == 1
    assert melk["last_bought"] == "2026-06-16T07:50:00.000+02:00"
    assert rows(store, "price_log") > 0
