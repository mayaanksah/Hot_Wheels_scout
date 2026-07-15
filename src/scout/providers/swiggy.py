"""Swiggy Instamart provider — delegates to the existing (live-verified) search
and cart logic so behaviour is unchanged by the provider refactor."""

from __future__ import annotations

from .base import Provider
from ..cart import add_to_cart as _swiggy_add_to_cart
from ..search import product_link as _swiggy_product_link, search_brand


class SwiggyProvider(Provider):
    name = "swiggy"
    label = "Instamart"
    mcp_url = "https://mcp.swiggy.com/im"
    # Search/address/cart only. Excludes checkout, get_orders, track_order,
    # get_payment_options, check_payment_status, confirm_order, etc.
    tool_allowlist = frozenset({
        "search_products", "get_addresses", "get_cart", "update_cart",
    })

    async def search(self, client, address_id, keyword, max_pages):
        return await search_brand(client, address_id, keyword, max_pages)

    async def get_addresses(self, client):
        payload = await client.call("get_addresses", {})
        entries = payload.get("addresses", []) if isinstance(payload, dict) else []
        labels = {}
        for e in entries:
            aid = e.get("id")
            if aid:
                labels[str(aid)] = e.get("addressTag") or e.get("addressCategory") or str(aid)[:6]
        return labels

    async def add_to_cart(self, client, cart_address_id, product):
        await _swiggy_add_to_cart(client, cart_address_id, product)

    def product_link(self, product):
        return _swiggy_product_link(product)
