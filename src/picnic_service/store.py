"""SQLite-opslag van de bestelhistorie (fase 1) en de API-gegevens (fase 2).

Werkt op de ruwe delivery-JSON van `get_delivery`, niet op de wrapper-modellen,
zodat velden die de wrapper niet modelleert (delivery_time, decorators op
orderregels, issues) ook meegaan. Zie docs/ARCHITECTUUR.md 3.3.

Upserts zijn idempotent. Deliveries en orders worden nooit verwijderd; de
regels en issues van een delivery worden bij elke upsert vervangen door de
actuele inhoud, want vóór bezorging kan een bestelling nog wijzigen.
"""

import copy
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2"
FINAL_STATUSES = ("COMPLETED", "CANCELLED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    created_at       TEXT,
    slot_start       TEXT,
    slot_end         TEXT,
    eta_start        TEXT,
    eta_end          TEXT,
    delivered_start  TEXT,
    delivered_end    TEXT,
    total_cents      INTEGER,
    savings_cents    INTEGER,
    deposit_cents    INTEGER,
    raw_json         TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_slot_start ON deliveries (slot_start);

CREATE TABLE IF NOT EXISTS orders (
    id                    TEXT PRIMARY KEY,
    delivery_id           TEXT NOT NULL REFERENCES deliveries (id),
    status                TEXT,
    created_at            TEXT,
    cancelled_at          TEXT,
    total_cents           INTEGER,
    checkout_total_cents  INTEGER,
    savings_cents         INTEGER,
    deposit_cents         INTEGER
);
CREATE INDEX IF NOT EXISTS orders_delivery ON orders (delivery_id);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id        TEXT NOT NULL REFERENCES orders (id),
    line_id         TEXT NOT NULL,
    article_id      TEXT NOT NULL,
    delivery_id     TEXT NOT NULL REFERENCES deliveries (id),
    name            TEXT,
    unit_quantity   TEXT,
    image_id        TEXT,
    quantity        INTEGER NOT NULL,
    line_cents      INTEGER,
    display_cents   INTEGER,
    discount_cents  INTEGER,
    unit_cents      INTEGER,
    discount_text   TEXT,
    PRIMARY KEY (order_id, line_id, article_id)
);
CREATE INDEX IF NOT EXISTS order_lines_article ON order_lines (article_id);
CREATE INDEX IF NOT EXISTS order_lines_delivery ON order_lines (delivery_id);

CREATE TABLE IF NOT EXISTS articles (
    id                 TEXT PRIMARY KEY,
    name               TEXT,
    unit_quantity      TEXT,
    image_id           TEXT,
    first_bought       TEXT,
    last_bought        TEXT,
    deliveries_count   INTEGER,
    units_count        INTEGER,
    last_unit_cents    INTEGER,
    available          INTEGER,
    detail_json        TEXT,
    detail_fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS price_log (
    article_id  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    source      TEXT NOT NULL,
    unit_cents  INTEGER NOT NULL,
    PRIMARY KEY (article_id, seen_at, source)
);

CREATE TABLE IF NOT EXISTS delivery_issues (
    delivery_id  TEXT NOT NULL REFERENCES deliveries (id),
    article_id   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    quantity     INTEGER,
    resolution   TEXT,
    price_cents  INTEGER,
    PRIMARY KEY (delivery_id, article_id, reason)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,
    deliveries_seen     INTEGER,
    deliveries_fetched  INTEGER,
    lines_upserted      INTEGER,
    message             TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- fase 2: woordenboek van het huishouden, term -> artikel
CREATE TABLE IF NOT EXISTS aliases (
    term           TEXT PRIMARY KEY,
    article_id     TEXT NOT NULL,
    name           TEXT,
    unit_quantity  TEXT,
    created_at     TEXT NOT NULL
);

-- fase 2: Favorieten-pagina, platgeslagen en gecachet
CREATE TABLE IF NOT EXISTS favorites (
    article_id     TEXT PRIMARY KEY,
    name           TEXT,
    unit_quantity  TEXT,
    price_cents    INTEGER,
    fetched_at     TEXT NOT NULL
);

-- fase 2: toegevoegde artikelen, voor /undo
CREATE TABLE IF NOT EXISTS add_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    added_at       TEXT NOT NULL,
    article_id     TEXT NOT NULL,
    name           TEXT,
    unit_quantity  TEXT,
    count          INTEGER NOT NULL,
    term           TEXT,
    undone_at      TEXT
);

-- fase 2: gemaakte keuzes per term, om pas na herhaling "onthouden?" te vragen
CREATE TABLE IF NOT EXISTS choice_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    term        TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    chosen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS choice_log_term ON choice_log (term, id);

-- fase 2: openstaande keuzelijst per term, zodat de Shortcut met een label kan antwoorden
CREATE TABLE IF NOT EXISTS pending_choices (
    term        TEXT NOT NULL,
    label       TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (term, label)
);
"""

_RECOMPUTE_ARTICLES = """
WITH lines AS (
    SELECT l.*, d.slot_start,
           ROW_NUMBER() OVER (PARTITION BY l.article_id ORDER BY d.slot_start DESC, l.order_id DESC) AS rn
    FROM order_lines l
    JOIN deliveries d ON d.id = l.delivery_id
    WHERE d.status = 'COMPLETED'
),
agg AS (
    SELECT article_id,
           MIN(slot_start) AS first_bought,
           MAX(slot_start) AS last_bought,
           COUNT(DISTINCT delivery_id) AS deliveries_count,
           SUM(quantity) AS units_count
    FROM lines
    GROUP BY article_id
)
INSERT INTO articles (id, name, unit_quantity, image_id, first_bought, last_bought,
                      deliveries_count, units_count, last_unit_cents)
SELECT a.article_id, l.name, l.unit_quantity, l.image_id, a.first_bought, a.last_bought,
       a.deliveries_count, a.units_count, l.unit_cents
FROM agg a
JOIN lines l ON l.article_id = a.article_id AND l.rn = 1
WHERE true
ON CONFLICT (id) DO UPDATE SET
    name = excluded.name,
    unit_quantity = excluded.unit_quantity,
    image_id = excluded.image_id,
    first_bought = excluded.first_bought,
    last_bought = excluded.last_bought,
    deliveries_count = excluded.deliveries_count,
    units_count = excluded.units_count,
    last_unit_cents = excluded.last_unit_cents
"""

_REFRESH_PRICE_LOG = """
INSERT OR IGNORE INTO price_log (article_id, seen_at, source, unit_cents)
SELECT l.article_id, d.slot_start, 'delivery', l.unit_cents
FROM order_lines l
JOIN deliveries d ON d.id = l.delivery_id
WHERE d.status = 'COMPLETED' AND l.unit_cents IS NOT NULL AND d.slot_start IS NOT NULL
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_delivery(raw: dict) -> dict:
    """Kopie zonder betaalgegevens (orders[].transaction_info)."""
    clean = copy.deepcopy(raw)
    for order in clean.get("orders") or []:
        order.pop("transaction_info", None)
    return clean


def _quantity(article: dict) -> int:
    for decorator in article.get("decorators") or []:
        if decorator.get("type") == "QUANTITY" and decorator.get("quantity"):
            return int(decorator["quantity"])
    return 1


def _line_discount(line: dict) -> tuple[int | None, str | None]:
    cents = None
    texts = []
    for decorator in line.get("decorators") or []:
        if decorator.get("type") == "PRICE" and decorator.get("display_price") is not None:
            cents = decorator["display_price"]
        if decorator.get("text"):
            texts.append(decorator["text"])
    return cents, ", ".join(texts) or None


def _sum(orders: list[dict], key: str) -> int | None:
    values = [o.get(key) for o in orders if o.get(key) is not None]
    return sum(values) if values else None


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(path, 0o600)
        # De API gebruikt de verbinding vanuit meerdere threads, altijd achter Shop.lock
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self.conn.close()

    # ---- meta ----

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- deliveries ----

    def delivery_statuses(self) -> dict[str, str]:
        return {r["id"]: r["status"] for r in self.conn.execute("SELECT id, status FROM deliveries")}

    def upsert_delivery(self, raw: dict) -> int:
        """Schrijf één delivery met orders, regels en issues. Geeft het aantal regels terug."""
        delivery_id = raw.get("delivery_id") or raw.get("id")
        if not delivery_id:
            raise ValueError("delivery zonder id")
        clean = sanitize_delivery(raw)
        orders = clean.get("orders") or []
        slot = clean.get("slot") or {}
        eta = clean.get("eta2") or {}
        delivered = clean.get("delivery_time") or {}
        now = now_iso()
        lines = 0
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO deliveries (id, status, created_at, slot_start, slot_end, eta_start, eta_end,
                                        delivered_start, delivered_end, total_cents, savings_cents,
                                        deposit_cents, raw_json, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status, created_at = excluded.created_at,
                    slot_start = excluded.slot_start, slot_end = excluded.slot_end,
                    eta_start = excluded.eta_start, eta_end = excluded.eta_end,
                    delivered_start = excluded.delivered_start, delivered_end = excluded.delivered_end,
                    total_cents = excluded.total_cents, savings_cents = excluded.savings_cents,
                    deposit_cents = excluded.deposit_cents, raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    delivery_id, clean.get("status") or "UNKNOWN", clean.get("creation_time"),
                    slot.get("window_start"), slot.get("window_end"), eta.get("start"), eta.get("end"),
                    delivered.get("start"), delivered.get("end"),
                    _sum(orders, "total_price"), _sum(orders, "total_savings"), _sum(orders, "total_deposit"),
                    json.dumps(clean, ensure_ascii=False, separators=(",", ":")), now, now,
                ),
            )
            self.conn.execute("DELETE FROM order_lines WHERE delivery_id = ?", (delivery_id,))
            self.conn.execute("DELETE FROM delivery_issues WHERE delivery_id = ?", (delivery_id,))
            for order in orders:
                order_id = order.get("id")
                if not order_id:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO orders (id, delivery_id, status, created_at, cancelled_at, total_cents,
                                        checkout_total_cents, savings_cents, deposit_cents)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        delivery_id = excluded.delivery_id, status = excluded.status,
                        created_at = excluded.created_at, cancelled_at = excluded.cancelled_at,
                        total_cents = excluded.total_cents, checkout_total_cents = excluded.checkout_total_cents,
                        savings_cents = excluded.savings_cents, deposit_cents = excluded.deposit_cents
                    """,
                    (
                        order_id, delivery_id, order.get("status"), order.get("creation_time"),
                        order.get("cancellation_time"), order.get("total_price"), order.get("checkout_total_price"),
                        order.get("total_savings"), order.get("total_deposit"),
                    ),
                )
                for line in order.get("items") or []:
                    articles = [a for a in line.get("items") or [] if a.get("id")]
                    discount_cents, discount_text = _line_discount(line)
                    total_units = sum(_quantity(a) for a in articles)
                    for article in articles:
                        quantity = _quantity(article)
                        # ORDER_ARTICLE.price is een placeholder; de prijs staat op de regel
                        unit_cents = None
                        if len(articles) == 1 and line.get("price") is not None and total_units:
                            unit_cents = round(line["price"] / total_units)
                        image_ids = article.get("image_ids") or []
                        self.conn.execute(
                            """
                            INSERT INTO order_lines (order_id, line_id, article_id, delivery_id, name,
                                                     unit_quantity, image_id, quantity, line_cents,
                                                     display_cents, discount_cents, unit_cents, discount_text)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (order_id, line_id, article_id) DO UPDATE SET
                                quantity = order_lines.quantity + excluded.quantity
                            """,
                            (
                                order_id, line.get("id") or "", article["id"], delivery_id, article.get("name"),
                                article.get("unit_quantity"), image_ids[0] if image_ids else None, quantity,
                                line.get("price"), line.get("display_price"), discount_cents, unit_cents,
                                discount_text,
                            ),
                        )
                        lines += 1
            for decorator in clean.get("decorators") or []:
                if decorator.get("type") != "ARTICLE_DELIVERY_ISSUES":
                    continue
                for issue in decorator.get("issues") or []:
                    if not issue.get("article_id"):
                        continue
                    self.conn.execute(
                        """
                        INSERT INTO delivery_issues (delivery_id, article_id, reason, quantity, resolution, price_cents)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (delivery_id, article_id, reason) DO UPDATE SET
                            quantity = delivery_issues.quantity + excluded.quantity,
                            price_cents = COALESCE(delivery_issues.price_cents, 0) + COALESCE(excluded.price_cents, 0)
                        """,
                        (
                            delivery_id, issue["article_id"], (issue.get("reason") or {}).get("type") or "UNKNOWN",
                            issue.get("quantity"), (issue.get("resolution") or {}).get("type"), issue.get("price"),
                        ),
                    )
        return lines

    def recompute_derived(self) -> None:
        """Artikelen en prijslog opnieuw afleiden uit de orderregels."""
        with self.conn:
            self.conn.execute(_RECOMPUTE_ARTICLES)
            self.conn.execute(_REFRESH_PRICE_LOG)

    # ---- sync_log ----

    def start_sync(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sync_log (started_at, status) VALUES (?, 'running')", (now_iso(),)
            )
        return int(cur.lastrowid)

    def finish_sync(self, run_id: int, status: str, seen: int, fetched: int, lines: int, message: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE sync_log SET finished_at = ?, status = ?, deliveries_seen = ?, deliveries_fetched = ?,
                                    lines_upserted = ?, message = ?
                WHERE id = ?
                """,
                (now_iso(), status, seen, fetched, lines, message, run_id),
            )

    # ---- fase 2: woordenboek ----

    def get_alias(self, term: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM aliases WHERE term = ?", (term,)).fetchone()

    def list_aliases(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM aliases ORDER BY term").fetchall()

    def set_alias(self, term: str, article_id: str, name: str | None, unit_quantity: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO aliases (term, article_id, name, unit_quantity, created_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (term) DO UPDATE SET article_id = excluded.article_id, name = excluded.name,
                    unit_quantity = excluded.unit_quantity, created_at = excluded.created_at
                """,
                (term, article_id, name, unit_quantity, now_iso()),
            )

    def delete_alias(self, term: str) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM aliases WHERE term = ?", (term,)).rowcount > 0

    # ---- fase 2: favorieten ----

    def favorites_fetched_at(self) -> str | None:
        row = self.conn.execute("SELECT MAX(fetched_at) FROM favorites").fetchone()
        return row[0]

    def replace_favorites(self, items: list[dict]) -> None:
        now = now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM favorites")
            self.conn.executemany(
                "INSERT OR REPLACE INTO favorites (article_id, name, unit_quantity, price_cents, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(i["article_id"], i.get("name"), i.get("unit_quantity"), i.get("price_cents"), now) for i in items],
            )

    def list_favorites(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM favorites").fetchall()

    # ---- fase 2: historie voor de keuzeregel ----

    def purchase_stats(self, since: str) -> list[sqlite3.Row]:
        """Per artikel: naam, eenheid, laatste prijs, laatst gekocht, aantal bezorgingen sinds `since`."""
        return self.conn.execute(
            """
            SELECT a.id AS article_id, a.name, a.unit_quantity, a.last_unit_cents AS price_cents, a.last_bought,
                   a.deliveries_count,
                   (SELECT COUNT(DISTINCT l.delivery_id) FROM order_lines l JOIN deliveries d ON d.id = l.delivery_id
                    WHERE l.article_id = a.id AND d.status = 'COMPLETED' AND d.slot_start >= ?) AS recent_count
            FROM articles a
            """,
            (since,),
        ).fetchall()

    def article(self, article_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()

    def recent_orders(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT d.id, d.status, d.slot_start, d.slot_end, d.delivered_start, d.total_cents, d.savings_cents,
                   (SELECT COALESCE(SUM(quantity), 0) FROM order_lines l WHERE l.delivery_id = d.id) AS units
            FROM deliveries d ORDER BY d.slot_start DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def last_sync(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()

    # ---- fase 2: add_log en keuzes ----

    def log_add(self, article_id: str, name: str | None, unit_quantity: str | None, count: int, term: str | None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO add_log (added_at, article_id, name, unit_quantity, count, term) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), article_id, name, unit_quantity, count, term),
            )
        return int(cur.lastrowid)

    def last_add(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM add_log WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    def mark_undone(self, add_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE add_log SET undone_at = ? WHERE id = ?", (now_iso(), add_id))

    def save_choices(self, term: str, options: list[dict]) -> None:
        """Bewaar korte en volledige labels, zodat beide terugleiden naar het artikel."""
        now = now_iso()
        rows = []
        for o in options:
            for label in {o["label"], o.get("full_label") or o["label"]}:
                rows.append((term, label, o["article_id"], now))
        with self.conn:
            self.conn.execute("DELETE FROM pending_choices WHERE term = ?", (term,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO pending_choices (term, label, article_id, created_at) VALUES (?, ?, ?, ?)",
                rows,
            )

    def log_choice(self, term: str, article_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO choice_log (term, article_id, chosen_at) VALUES (?, ?, ?)", (term, article_id, now_iso())
            )

    def repeated_choice(self, term: str, times: int) -> str | None:
        """Artikel dat de laatste `times` keuzes voor deze term steeds gekozen is, anders None."""
        rows = self.conn.execute(
            "SELECT article_id FROM choice_log WHERE term = ? ORDER BY id DESC LIMIT ?", (term, times)
        ).fetchall()
        ids = {r["article_id"] for r in rows}
        return rows[0]["article_id"] if len(rows) == times and len(ids) == 1 else None

    def resolve_choice(self, term: str, label: str) -> str | None:
        row = self.conn.execute(
            "SELECT article_id FROM pending_choices WHERE term = ? AND label = ?", (term, label)
        ).fetchone()
        return row["article_id"] if row else None

    # ---- tellingen ----

    def counts(self) -> dict[str, Any]:
        tables = ("deliveries", "orders", "order_lines", "articles", "price_log", "delivery_issues")
        result: dict[str, Any] = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        row = self.conn.execute(
            "SELECT MIN(slot_start), MAX(slot_start), SUM(total_cents) FROM deliveries WHERE status = 'COMPLETED'"
        ).fetchone()
        result.update(first=row[0], last=row[1], completed_total_cents=row[2])
        return result
