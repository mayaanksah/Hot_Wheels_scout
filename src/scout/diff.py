"""Per-address stock tracking with hysteresis.

Swiggy's search is noisy — consecutive calls return a drifting subset of
products, with items flickering in and out even when stock hasn't changed
(measured live 13 Jul 2026). Treating one absence as "out of stock" produces
phantom restock alerts every cycle. So each product carries a small state
machine per address:

- `avail_streak`  consecutive cycles seen available (present in search AND in
  stock). A confirmed in-stock transition needs `confirm_threshold` in a row.
- `misses`        consecutive cycles seen unavailable. A confirmed out-of-stock
  transition needs `miss_threshold` in a row.
- `in_stock`      the *confirmed* (de-noised) availability.
- `alerted_instock`  whether the current in-stock episode has been alerted
  (dedup); reset when the product is confirmed out of stock.
- `seen_instock_before`  distinguishes a first-ever arrival (New arrival) from a
  return to stock (Restock).
- `pending_kind`  the owed alert kind while `in_stock and not alerted_instock`.

New arrival = first confirmed in-stock of a product (FR-1, alerts regardless of
wishlist). Restock = a later confirmed return to stock of a *wishlist* product
(FR-2). A non-wishlist product returning to stock is tracked but not alerted.
"""

from __future__ import annotations

from .search import matches_wishlist

MISS_THRESHOLD = 3      # cycles unavailable before confirming out-of-stock
CONFIRM_THRESHOLD = 2   # cycles available before confirming in-stock


def _new_entry() -> dict:
    return {
        "in_stock": False,
        "alerted_instock": False,
        "avail_streak": 0,
        "misses": 0,
        "seen_instock_before": False,
        "pending_kind": None,
    }


def update_address(
    seen: dict,
    current: dict[str, dict],
    wishlist: list[str],
    now_utc: str,
    *,
    seeded: bool,
    miss_threshold: int = MISS_THRESHOLD,
    confirm_threshold: int = CONFIRM_THRESHOLD,
) -> list[tuple[dict, str]]:
    """Fold one address's search results into its `seen` dict in place, with
    hysteresis. Returns [(product, kind)] owed an alert this cycle — empty when
    seeding. Only products present in `current` can be returned (we need the
    full product dict to alert), so an alert owed while the item is flickered
    out is simply deferred until it reappears.

    Caller must skip this entirely on a fully empty/failed search (guards the
    mass false-OOS storm, PRD §12); per-product churn within a non-empty result
    is what the buffers absorb.
    """
    all_ids = set(seen) | set(current)
    for pid in all_ids:
        product = current.get(pid)
        available = product is not None and product["in_stock"]
        entry = seen.get(pid) or seen.setdefault(pid, _new_entry())

        if product is not None:
            entry["title"] = product["title"]
            entry["price"] = product["price"]
            entry["last_seen_utc"] = now_utc

        if available:
            entry["misses"] = 0
            entry["avail_streak"] = entry.get("avail_streak", 0) + 1
        else:
            entry["avail_streak"] = 0
            entry["misses"] = entry.get("misses", 0) + 1

        if seeded:
            # Trust the first observation as the baseline; never alert.
            entry["in_stock"] = available
            entry["alerted_instock"] = available
            entry["seen_instock_before"] = available
            entry["pending_kind"] = None
            continue

        if not entry["in_stock"] and available and entry["avail_streak"] >= confirm_threshold:
            entry["in_stock"] = True
            entry["alerted_instock"] = False
            first_time = not entry["seen_instock_before"]
            entry["seen_instock_before"] = True
            if first_time:
                entry["pending_kind"] = "New arrival"
            elif matches_wishlist(product["title"], wishlist):
                entry["pending_kind"] = "Restock"
            else:
                entry["pending_kind"] = None
                entry["alerted_instock"] = True  # tracked, but not worth alerting
        elif entry["in_stock"] and not available and entry["misses"] >= miss_threshold:
            entry["in_stock"] = False
            entry["alerted_instock"] = False
            entry["pending_kind"] = None

    if seeded:
        return []

    # Owed alerts: confirmed in stock, not yet alerted, and present this cycle
    # so we have the full product dict to send.
    hits = []
    for pid, product in current.items():
        entry = seen[pid]
        if entry["in_stock"] and not entry["alerted_instock"] and entry["pending_kind"]:
            hits.append((product, entry["pending_kind"]))
    return hits
