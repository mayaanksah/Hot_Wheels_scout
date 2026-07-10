"""Diff current search results against persisted state.

New arrival  = product id never seen before (FR-1).
Restock      = wishlist-matching product now in stock whose stored state was
               out of stock or absent-from-results (FR-2).
"""

from __future__ import annotations

from .search import matches_wishlist


def find_new_arrivals(current: dict[str, dict], seen_products: dict) -> list[dict]:
    return [p for pid, p in current.items() if pid not in seen_products and p["in_stock"]]


def find_wishlist_restocks(
    current: dict[str, dict], seen_products: dict, wishlist: list[str]
) -> list[dict]:
    restocks = []
    for pid, product in current.items():
        if not product["in_stock"]:
            continue
        previous = seen_products.get(pid)
        if previous is None or previous.get("in_stock", False):
            continue  # never seen (that's a new arrival) or was already in stock
        if matches_wishlist(product["title"], wishlist):
            restocks.append(product)
    return restocks


def apply_run_to_state(
    state: dict, current: dict[str, dict], now_utc: str, search_ok: bool
) -> None:
    """Fold this run's results into state.seen_products in place.

    Products absent from a *successful* search are marked out of stock and
    their alert flag reset, so a later reappearance counts as a restock. On a
    failed/empty search (search_ok=False) nothing is flipped — guards against
    false restock storms from one bad response (PRD §12).
    """
    seen = state["seen_products"]
    for pid, product in current.items():
        entry = seen.setdefault(pid, {})
        entry.update(
            title=product["title"],
            price=product["price"],
            in_stock=product["in_stock"],
            last_seen_utc=now_utc,
        )
        if not product["in_stock"]:
            entry["alerted_instock"] = False
    if search_ok:
        for pid, entry in seen.items():
            if pid not in current and entry.get("in_stock"):
                entry["in_stock"] = False
                entry["alerted_instock"] = False
