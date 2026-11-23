"""Kleine bouwstenen voor tests: synthetische deliveries in het formaat van get_delivery."""


def make_delivery(delivery_id, window_start, lines, status="COMPLETED", cut_off=None):
    """lines: lijst van (article_id, name, unit_quantity, quantity, line_cents)."""
    items = []
    for i, (article_id, name, unit, qty, cents) in enumerate(lines):
        items.append({
            "type": "ORDER_LINE",
            "id": f"{delivery_id}-{i}",
            "price": cents,
            "display_price": cents,
            "items": [{
                "type": "ORDER_ARTICLE",
                "id": article_id,
                "name": name,
                "unit_quantity": unit,
                "price": 432199,
                "decorators": [{"type": "QUANTITY", "quantity": qty}],
            }],
        })
    return {
        "type": "DELIVERY",
        "delivery_id": delivery_id,
        "id": delivery_id,
        "status": status,
        "creation_time": window_start,
        "slot": {"slot_id": f"slot-{delivery_id}", "window_start": window_start,
                 "window_end": window_start, "cut_off_time": cut_off or window_start},
        "orders": [{"type": "ORDER", "id": f"order-{delivery_id}", "status": status,
                    "total_price": sum(line[4] for line in lines), "items": items}],
    }
