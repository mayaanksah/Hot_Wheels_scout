"""Zepto provider. Field mapping + tool names from live recon (14 Jul 2026,
see memory zepto-mcp-facts). Session model: select an address before search /
cart. Cart is add-only via update_cart(replaceCart=false) which merges — no
whole-cart reconstruction needed (safer than Swiggy)."""

from __future__ import annotations

import os

from .base import Provider
from ..cart import CartSkipped
from ..mcp_client import ToolCallError
from ..search import SchemaDriftError, _first_key, extract_products

# A stable per-bot cart key (Zepto uses deviceId as a fallback cart key when
# there's no app session). Overridable so a real device id can be supplied.
DEFAULT_DEVICE_ID = "hot-wheels-scout-bot"

_STORE_CLOSED_MARKERS = ("store is currently unavailable", "store not selected",
                         "not serviceable", "out of stock", "unavailable")


class ZeptoProvider(Provider):
    name = "zepto"
    label = "Zepto"
    mcp_url = "https://mcp.zepto.co.in/mcp"
    # Alert-only: Zepto's MCP cart is a separate "agent cart" from the app cart
    # (verified live 14 Jul 2026 — item added via MCP never appears in the app,
    # even account-tied and store-matched), and we never checkout, so an
    # auto-add can't help Maya. Alert with a link; she adds in-app herself.
    supports_cart = False
    # Alert on the FIRST sighting. GitHub's scheduler runs ~hourly (throttled),
    # so requiring 2 consecutive polls can miss a car that sells out between
    # runs (observed 15 Jul 2026: "HW 17 Audi" seen once, sold out before the
    # next poll). No cart-write here, so a rare false positive is cheap.
    confirm_threshold = 1
    # Read + cart only. Excludes zepto_shop (intent buy!), create_order,
    # create_online_payment_order, create_wallet_order,
    # create_upi_reserve_pay_order, check_payment_status, get_payment_methods,
    # add_saved_address, update_* — nothing that can place or pay for an order.
    tool_allowlist = frozenset({
        "search_products", "list_saved_addresses", "select_saved_address",
        "view_cart", "update_cart",
    })

    async def _select(self, client, address_id):
        await client.call("select_saved_address", {"addressId": address_id})

    async def search(self, client, address_id, keyword, max_pages):
        await self._select(client, address_id)  # session: pick store first
        found: dict[str, dict] = {}
        raw_count = 0
        for page in range(max_pages):
            payload = await client.call(
                "search_products", {"query": keyword, "pageNumber": page})
            raw = payload.get("products") if isinstance(payload, dict) else None
            if raw is None:
                raw = extract_products(payload)
            if not raw:
                break
            raw_count += len(raw)
            normalized = [p for p in (self._normalize(r) for r in raw) if p]
            new_ids = {p["id"] for p in normalized} - found.keys()
            for p in normalized:
                found.setdefault(p["id"], p)
            if not new_ids:
                break
        if raw_count and not found:
            raise SchemaDriftError(
                f"Zepto search returned {raw_count} items for '{keyword}' but none "
                "parsed — update field mapping in providers/zepto.py.")
        return list(found.values())

    def _normalize(self, raw: dict) -> dict | None:
        pvid = _first_key(raw, ("productVariantId", "variantId", "id"))
        name = _first_key(raw, ("name", "displayName", "title"))
        if not pvid or not name:
            return None
        price = _first_key(raw, ("price",))          # in paise
        avail = raw.get("availableQuantity")
        return {
            "id": str(pvid),
            "spin_id": str(pvid),                     # cart: productVariantId
            "sku_id": str(_first_key(raw, ("storeProductId", "storeProductID")) or ""),  # cart: storeProductId
            "title": str(name),
            "brand": "",                              # Zepto results carry no brand field
            "price": (price / 100) if isinstance(price, (int, float)) else None,
            "image": _first_key(raw, ("imageUrl", "image_url", "image")),
            "in_stock": bool(avail) and avail > 0 if isinstance(avail, (int, float)) else True,
            "raw_id_key": "productVariantId",
        }

    async def get_addresses(self, client):
        payload = await client.call("list_saved_addresses", {})
        entries = payload.get("addresses", []) if isinstance(payload, dict) else []
        labels = {}
        for e in entries:
            aid = e.get("id")
            if aid:
                labels[str(aid)] = e.get("label") or e.get("type") or str(aid)[:6]
        return labels

    def _cart_pvids(self, cart_payload) -> set[str]:
        items = []
        if isinstance(cart_payload, dict):
            for key in ("cartItems", "items", "products"):
                if isinstance(cart_payload.get(key), list):
                    items = cart_payload[key]
                    break
            else:
                items = extract_products(cart_payload)
        return {str(_first_key(i, ("productVariantId", "variantId", "id")))
                for i in items if isinstance(i, dict)
                and _first_key(i, ("productVariantId", "variantId", "id"))}

    async def add_to_cart(self, client, cart_address_id, product):
        if not product.get("spin_id") or not product.get("sku_id"):
            raise CartSkipped(f"{product['title']} missing pvid/spid; cannot add")
        await self._select(client, cart_address_id)

        # Dedup only — replaceCart=false means we never need the whole cart to
        # add safely, so a failed read is non-fatal (still safe to add).
        try:
            existing = self._cart_pvids(await client.call("view_cart", {}))
            if product["spin_id"] in existing:
                return  # already in cart; don't reset its quantity
        except ToolCallError:
            pass

        device_id = os.environ.get("ZEPTO_DEVICE_ID", DEFAULT_DEVICE_ID)
        try:
            await client.call("update_cart", {
                "deviceId": device_id,
                "replaceCart": False,   # merge/upsert — leaves other items intact
                "cartItems": [{
                    "productVariantId": product["spin_id"],
                    "storeProductId": product["sku_id"],
                    "quantity": 1,
                }],
            })
        except ToolCallError as exc:
            if any(m in str(exc).lower() for m in _STORE_CLOSED_MARKERS):
                raise CartSkipped(f"store closed / item rejected: {exc}") from exc
            raise

    def product_link(self, product):
        from urllib.parse import quote_plus
        return "https://www.zeptonow.com/search?query=" + quote_plus(product["title"])
