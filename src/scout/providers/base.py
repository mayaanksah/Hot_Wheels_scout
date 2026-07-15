"""Provider interface. Concrete providers subclass this."""

from __future__ import annotations


class Provider:
    name: str = ""                 # state key + secret prefix, e.g. "swiggy"
    label: str = ""                # human app name for alerts, e.g. "Instamart"
    mcp_url: str = ""
    tool_allowlist: frozenset[str] = frozenset()  # search/address/cart tools ONLY

    async def search(self, client, address_id: str, keyword: str, max_pages: int) -> list[dict]:
        """Return normalized products in stock/known at `address_id` for `keyword`."""
        raise NotImplementedError

    async def get_addresses(self, client) -> dict[str, str]:
        """Return {address_id: human_label} for building alert labels."""
        raise NotImplementedError

    async def add_to_cart(self, client, cart_address_id: str, product: dict) -> None:
        """Add `product` to the cart at `cart_address_id` (add-only; raise
        cart.CartSkipped when a safe add is impossible)."""
        raise NotImplementedError

    def product_link(self, product: dict) -> str:
        raise NotImplementedError
