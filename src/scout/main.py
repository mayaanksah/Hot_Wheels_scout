"""One polling cycle across all enabled providers.

For each provider (Swiggy, Zepto, …): open its MCP session, search each
monitored address, fold results into that provider+address's state slice with
hysteresis, and emit one aggregated alert per car (auto-adding at the provider's
cart address). Deterministic, no LLM. A failure in one provider never affects
another and never crashes the schedule.
"""

from __future__ import annotations

import asyncio
import logging

from .alerts import send_plain, send_product_alert
from .cart import CartSkipped
from .diff import update_address
from .mcp_client import AuthExpiredError, mcp_session
from .providers import PROVIDERS
from .search import SchemaDriftError, matches_brands, matches_wishlist
from .settings import (
    STATE_PATH,
    enabled_providers,
    load_address_ids,
    load_cart_address_id,
    load_config,
    load_telegram,
    load_token,
)
from .state import load_state, save_state, utc_now

log = logging.getLogger("scout")


def _reauth_message(app: str) -> str:
    return (f"🔑 Hot Wheels Scout: {app} token expired. Run "
            f"`python scripts/auth.py {app.lower()}` locally and update the "
            f"{app.upper()}_TOKEN secret.")


async def _search_all_brands(provider, client, config, address_id):
    """Union of brand searches at one address, brand-filtered. Returns
    ({product_id: product}, any_results)."""
    current: dict[str, dict] = {}
    any_results = False
    for brand in config["brands"]:
        products = await provider.search(client, address_id, brand, config["max_search_pages"])
        any_results = any_results or bool(products)
        for product in products:
            if matches_brands(product, config["brands"]):
                current[product["id"]] = product
    return current, any_results


def _record_hit(cycle_hits, product, kind, address_id, labels, seen, cart_address_id):
    hit = cycle_hits.get(product["id"])
    if hit is None:
        hit = cycle_hits[product["id"]] = {
            "product": product, "kind": kind, "labels": [],
            "entries": [], "at_cart_addr": False,
        }
    if kind == "Restock":
        hit["kind"] = "Restock"
    hit["labels"].append(labels.get(address_id, address_id[:6]))
    hit["entries"].append((seen, product["id"]))
    if address_id == cart_address_id and product["in_stock"]:
        hit["at_cart_addr"] = True
        hit["product"] = product  # the cart-address variant carries the right cart ids


async def _emit_hit(provider, client, telegram, config, cart_address_id, labels, hit):
    product = hit["product"]
    is_wishlisted = matches_wishlist(product["title"], config["wishlist"])
    want_add = config["auto_add_new_arrivals"] or (is_wishlisted and config["auto_add_wishlist"])
    added = False
    if want_add and hit["at_cart_addr"]:
        try:
            await provider.add_to_cart(client, cart_address_id, product)
            added = True
        except CartSkipped as exc:
            log.warning("[%s] cart write skipped for %s: %s", provider.name, product["title"], exc)
        except Exception:
            log.exception("[%s] cart add failed for %s", provider.name, product["title"])

    emitted = True
    if telegram:
        try:
            await send_product_alert(
                telegram, product, hit["kind"], added,
                app=provider.label, link=provider.product_link(product),
                address_labels=hit["labels"],
                cart_label=labels.get(cart_address_id) if added else None,
            )
        except Exception:
            log.exception("[%s] telegram alert failed for %s", provider.name, product["title"])
            emitted = False
    else:
        log.info("[%s] %s: %s at %s", provider.name, hit["kind"],
                 product["title"], ", ".join(hit["labels"]))

    if emitted:
        for seen, pid in hit["entries"]:
            if pid in seen:
                seen[pid]["alerted_instock"] = True


async def _run_provider(provider, config, telegram, seen_by_provider, seeded, flags) -> None:
    token = load_token(provider.name)
    address_ids = load_address_ids(provider.name)
    cart_address_id = load_cart_address_id(provider.name)
    prov_state = seen_by_provider.setdefault(provider.name, {})
    now = utc_now()

    try:
        async with mcp_session(provider.mcp_url, token, provider.tool_allowlist) as client:
            labels = await provider.get_addresses(client)
            for a in address_ids:
                labels.setdefault(a, a[:6])
            flags.pop(f"reauth_{provider.name}", None)
            flags.pop(f"schema_{provider.name}", None)

            fresh = {a for a in address_ids if not prov_state.get(a)}
            cycle_hits: dict[str, dict] = {}

            for address_id in address_ids:
                seen = prov_state.setdefault(address_id, {})
                seed_addr = seeded or address_id in fresh
                current, any_results = await _search_all_brands(provider, client, config, address_id)

                if not any_results and not seed_addr:
                    log.warning("[%s] no results for %s; skipping its state update",
                                provider.name, labels[address_id])
                    continue

                hits = update_address(
                    seen, current, config["wishlist"], now, seeded=seed_addr,
                    miss_threshold=config["miss_threshold"],
                    confirm_threshold=config["confirm_threshold"],
                )
                for prod, kind in hits:
                    _record_hit(cycle_hits, prod, kind, address_id, labels, seen, cart_address_id)

            if fresh and not seeded:
                log.info("[%s] silently seeded %d newly-added address(es)",
                         provider.name, len(fresh))

            if not seeded:
                for hit in cycle_hits.values():
                    await _emit_hit(provider, client, telegram, config,
                                    cart_address_id, labels, hit)
            log.info("[%s] done: %d addresses, %d cars", provider.name,
                     len(address_ids), len(cycle_hits))

    except AuthExpiredError:
        log.warning("[%s] auth expired", provider.name)
        if telegram and not flags.get(f"reauth_{provider.name}"):
            try:
                await send_plain(telegram, _reauth_message(provider.label))
                flags[f"reauth_{provider.name}"] = True
            except Exception:
                log.exception("[%s] failed to send re-auth alert", provider.name)
    except SchemaDriftError as exc:
        log.error("[%s] schema drift: %s", provider.name, exc)
        if telegram and not flags.get(f"schema_{provider.name}"):
            try:
                await send_plain(telegram, f"⚠️ Hot Wheels Scout {provider.label} schema drift:\n{exc}")
                flags[f"schema_{provider.name}"] = True
            except Exception:
                log.exception("[%s] failed to send schema alert", provider.name)
    except Exception:
        log.exception("[%s] cycle failed; other providers/next schedule continue", provider.name)


async def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    telegram = load_telegram()
    providers = enabled_providers()
    if not providers:
        log.error("No enabled providers. Set <PROVIDER>_TOKEN and "
                  "<PROVIDER>_ADDRESS_IDS (see scripts/auth.py, scripts/recon.py).")
        return 1

    state, seeded = load_state(STATE_PATH)
    flags = state["flags"]
    seen_by_provider = state["seen_by_provider"]

    for name in providers:
        await _run_provider(PROVIDERS[name], config, telegram, seen_by_provider, seeded, flags)

    save_state(STATE_PATH, state)
    if seeded:
        total = sum(len(a) for prov in seen_by_provider.values() for a in prov.values())
        log.info("seeded silently across %d provider(s) (%d product-slots); "
                 "alerts start next cycle", len(providers), total)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
