"""One polling cycle: search -> filter -> diff -> cart -> alert -> persist.

Deterministic, no LLM involved (PRD §8). Designed to never crash the
schedule: transient failures log and exit 0 so the next cron cycle retries;
only misconfiguration (missing secrets) exits non-zero.
"""

from __future__ import annotations

import asyncio
import logging

from .alerts import send_plain, send_product_alert
from .cart import CartSkipped, add_to_cart
from .diff import update_address
from .mcp_client import AuthExpiredError, instamart_client
from .search import SchemaDriftError, matches_brands, matches_wishlist, search_brand
from .settings import (
    STATE_PATH,
    load_address_ids,
    load_cart_address_id,
    load_config,
    load_swiggy_token,
    load_telegram,
)
from .state import load_state, save_state, utc_now

log = logging.getLogger("scout")

REAUTH_MESSAGE = (
    "🔑 Hot Wheels Scout: Swiggy token expired (they last ~5 days). "
    "Run scripts/auth.py locally and update the SWIGGY_TOKEN secret."
)


async def build_label_map(client, address_ids: list[str]) -> dict[str, str]:
    """Map address id -> human label (addressTag/Category) for alerts.
    Falls back to a short id on any failure so alerts still send."""
    labels = {aid: aid[:6] for aid in address_ids}
    try:
        payload = await client.call("get_addresses", {})
        entries = payload.get("addresses", []) if isinstance(payload, dict) else []
        for entry in entries:
            aid = str(entry.get("id"))
            if aid in labels:
                labels[aid] = entry.get("addressTag") or entry.get("addressCategory") or labels[aid]
    except Exception:
        log.warning("get_addresses failed; using short-id labels", exc_info=True)
    return labels


async def _search_address(client, config, address_id):
    """Return {product_id: product} for one address, brand-filtered."""
    current: dict[str, dict] = {}
    any_results = False
    for brand in config["brands"]:
        products = await search_brand(
            client, address_id, brand, config["max_search_pages"]
        )
        any_results = any_results or bool(products)
        for product in products:
            if matches_brands(product, config["brands"]):
                current[product["id"]] = product
    return current, any_results


def _record_hit(cycle_hits, product, kind, address_id, labels, seen, cart_address_id):
    """Accumulate a per-address detection into a single per-product hit so the
    same car isn't alerted once per address in a cycle."""
    hit = cycle_hits.get(product["id"])
    if hit is None:
        hit = cycle_hits[product["id"]] = {
            "product": product,
            "kind": kind,
            "labels": [],
            "entries": [],          # (seen_dict, product_id) to flag after emit
            "at_cart_addr": False,
        }
    # A wishlist restock anywhere is the more informative label.
    if kind == "Restock":
        hit["kind"] = "Restock"
    hit["labels"].append(labels[address_id])
    hit["entries"].append((seen, product["id"]))
    if address_id == cart_address_id and product["in_stock"]:
        hit["at_cart_addr"] = True
        hit["product"] = product  # ensure the cart-address variant (with spin/sku) wins


async def _emit_hit(client, telegram, config, cart_address_id, labels, hit):
    """Send one aggregated alert for a car and, if it's in stock at the cart
    address and flags allow, auto-add it to that single cart."""
    product = hit["product"]
    is_wishlisted = matches_wishlist(product["title"], config["wishlist"])
    want_add = config["auto_add_new_arrivals"] or (
        is_wishlisted and config["auto_add_wishlist"]
    )
    added = False
    if want_add and hit["at_cart_addr"]:
        try:
            await add_to_cart(client, cart_address_id, product)
            added = True
        except CartSkipped as exc:
            log.warning("cart write skipped for %s: %s", product["title"], exc)
        except Exception:
            log.exception("cart add failed for %s", product["title"])

    emitted = True
    if telegram:
        try:
            await send_product_alert(
                telegram, product, hit["kind"], added,
                address_labels=hit["labels"],
                cart_label=labels.get(cart_address_id) if added else None,
            )
        except Exception:
            log.exception("telegram alert failed for %s", product["title"])
            emitted = False
    else:
        log.info("%s: %s at %s (no telegram configured)",
                 hit["kind"], product["title"], ", ".join(hit["labels"]))

    if emitted:  # cross-address dedup: mark every contributing address entry
        for seen, pid in hit["entries"]:
            if pid in seen:
                seen[pid]["alerted_instock"] = True


async def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    token = load_swiggy_token()
    address_ids = load_address_ids()
    cart_address_id = load_cart_address_id()
    telegram = load_telegram()

    if not token:
        log.error("No Swiggy token. Set SWIGGY_TOKEN or run scripts/auth.py.")
        return 1
    if not address_ids:
        log.error("No addresses. Set SWIGGY_ADDRESS_IDS (see scripts/recon.py).")
        return 1

    state, seeded = load_state(STATE_PATH)
    flags = state["flags"]
    seen_by_address = state["seen_by_address"]

    try:
        async with instamart_client(token) as client:
            labels = await build_label_map(client, address_ids)
            flags.pop("reauth_alerted", None)
            flags.pop("schema_alerted", None)

            # product_id -> aggregated hit across all addresses this cycle
            cycle_hits: dict[str, dict] = {}
            now = utc_now()

            # An address with no prior state (freshly added to the monitor set)
            # must seed silently, exactly like a first install — otherwise its
            # whole catalog would alert at once as "new arrivals" on its second
            # cycle. Captured before the setdefault below populates the slice.
            fresh_addresses = {aid for aid in address_ids if not seen_by_address.get(aid)}

            for address_id in address_ids:
                seen = seen_by_address.setdefault(address_id, {})
                seed_addr = seeded or address_id in fresh_addresses
                current, any_results = await _search_address(client, config, address_id)

                if not any_results and not seed_addr:
                    # Fully empty/failed response for THIS address: skip so we
                    # don't mass-increment misses toward false OOS (PRD §12).
                    # Per-product churn within a non-empty result is absorbed by
                    # the hysteresis buffers in update_address.
                    log.warning("no results for %s; skipping its state update", labels[address_id])
                    continue

                hits = update_address(
                    seen, current, config["wishlist"], now, seeded=seed_addr,
                    miss_threshold=config["miss_threshold"],
                    confirm_threshold=config["confirm_threshold"],
                )
                for product, kind in hits:
                    _record_hit(cycle_hits, product, kind,
                                address_id, labels, seen, cart_address_id)

            if fresh_addresses and not seeded:
                log.info("silently seeded %d newly-added address(es): %s",
                         len(fresh_addresses),
                         ", ".join(labels[a] for a in fresh_addresses))

            if seeded:
                save_state(STATE_PATH, state)
                total = sum(len(s) for s in seen_by_address.values())
                log.info("seeded silently across %d addresses (%d product-slots); "
                         "alerts start next cycle", len(address_ids), total)
                return 0

            for hit in cycle_hits.values():
                await _emit_hit(client, telegram, config, cart_address_id, labels, hit)

            save_state(STATE_PATH, state)
            log.info("cycle done: %d addresses, %d cars alerted",
                     len(address_ids), len(cycle_hits))
            return 0

    except AuthExpiredError:
        log.warning("Swiggy auth expired")
        if telegram and not flags.get("reauth_alerted"):
            try:
                await send_plain(telegram, REAUTH_MESSAGE)
                flags["reauth_alerted"] = True
                save_state(STATE_PATH, state)
            except Exception:
                log.exception("failed to send re-auth alert")
        return 0
    except SchemaDriftError as exc:
        log.error("schema drift: %s", exc)
        if telegram and not flags.get("schema_alerted"):
            try:
                await send_plain(telegram, f"⚠️ Hot Wheels Scout schema drift — bot is blind until fixed:\n{exc}")
                flags["schema_alerted"] = True
                save_state(STATE_PATH, state)
            except Exception:
                log.exception("failed to send schema alert")
        return 0
    except Exception:
        # Transient network/API trouble: log, keep state untouched, let the
        # next cycle retry (PRD §12 — never crash the schedule).
        log.exception("cycle failed; will retry next schedule")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
