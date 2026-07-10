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
from .diff import apply_run_to_state, find_new_arrivals, find_wishlist_restocks
from .mcp_client import AuthExpiredError, instamart_client
from .search import SchemaDriftError, matches_brands, matches_wishlist, search_brand
from .settings import (
    STATE_PATH,
    load_address_id,
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


async def _alert_and_add(client, telegram, config, address_id, product, kind, auto_add):
    added = False
    if auto_add:
        try:
            await add_to_cart(client, address_id, product)
            added = True
        except CartSkipped as exc:
            log.warning("cart write skipped for %s: %s", product["title"], exc)
        except Exception:
            log.exception("cart add failed for %s", product["title"])
    if telegram:
        try:
            await send_product_alert(telegram, product, kind, added)
            return True
        except Exception:
            log.exception("telegram alert failed for %s", product["title"])
            return False
    log.info("%s: %s (no telegram configured)", kind, product["title"])
    return True


async def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    token = load_swiggy_token()
    address_id = load_address_id()
    telegram = load_telegram()

    if not token:
        log.error("No Swiggy token. Set SWIGGY_TOKEN or run scripts/auth.py.")
        return 1
    if not address_id:
        log.error("No address id. Set SWIGGY_ADDRESS_ID (see scripts/recon.py).")
        return 1

    state, seeded = load_state(STATE_PATH)
    flags = state["flags"]
    seen = state["seen_products"]

    try:
        async with instamart_client(token) as client:
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

            flags.pop("reauth_alerted", None)
            flags.pop("schema_alerted", None)

            if not any_results and not seeded:
                # Suspect/empty response: do not flip everything to
                # out-of-stock off one bad cycle (PRD §12).
                log.warning("search returned nothing; skipping state update this cycle")
                return 0

            if seeded:
                apply_run_to_state(state, current, utc_now(), search_ok=True)
                for entry in seen.values():
                    entry["alerted_instock"] = entry.get("in_stock", False)
                save_state(STATE_PATH, state)
                log.info("state seeded silently with %d products; alerts start next cycle",
                         len(current))
                return 0

            new_arrivals = find_new_arrivals(current, seen)
            restocks = find_wishlist_restocks(current, seen, config["wishlist"])

            apply_run_to_state(state, current, utc_now(), search_ok=True)

            for product in new_arrivals:
                is_wishlisted = matches_wishlist(product["title"], config["wishlist"])
                auto_add = config["auto_add_new_arrivals"] or (
                    is_wishlisted and config["auto_add_wishlist"]
                )
                if await _alert_and_add(client, telegram, config, address_id,
                                        product, "New arrival", auto_add):
                    seen[product["id"]]["alerted_instock"] = True

            for product in restocks:
                entry = seen[product["id"]]
                if entry.get("alerted_instock"):
                    continue  # dedup (FR-3)
                if await _alert_and_add(client, telegram, config, address_id,
                                        product, "Restock", config["auto_add_wishlist"]):
                    entry["alerted_instock"] = True

            save_state(STATE_PATH, state)
            log.info("cycle done: %d tracked, %d new, %d restocked",
                     len(current), len(new_arrivals), len(restocks))
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
