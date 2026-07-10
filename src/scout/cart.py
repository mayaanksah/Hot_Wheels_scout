"""Add-to-cart via get_cart -> merge -> update_cart.

update_cart REPLACES the whole cart (there is no incremental add tool), so
the invariant here is add-only: existing cart lines are passed through
untouched, and if they cannot be faithfully reconstructed the write is
SKIPPED entirely (the alert still goes out). Losing a cart add is
recoverable; clobbering Maya's cart is not.
"""

from __future__ import annotations

from .search import ID_KEYS, _first_key

QUANTITY_KEYS = ("quantity", "qty", "count")


class CartSkipped(RuntimeError):
    """Cart write was skipped to protect existing cart contents."""


def _extract_cart_items(cart_payload) -> list[dict]:
    from .search import extract_products  # same shape-probing logic

    if isinstance(cart_payload, dict):
        for key in ("items", "cart_items", "cartItems", "products"):
            value = cart_payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        cart = cart_payload.get("cart")
        if isinstance(cart, dict):
            return _extract_cart_items(cart)
        return extract_products(cart_payload)
    return []


def build_cart_line(product: dict, template: dict | None) -> dict:
    """Build the new cart line, mirroring the key naming of existing lines
    (template) when available so update_cart accepts a homogeneous list."""
    id_key = "product_id"
    qty_key = "quantity"
    if template:
        id_key = next((k for k in ID_KEYS if k in template), id_key)
        qty_key = next((k for k in QUANTITY_KEYS if k in template), qty_key)
    return {id_key: product["id"], qty_key: 1}


async def add_to_cart(client, address_id: str, product: dict) -> None:
    """Raises CartSkipped when a safe merge is impossible; other exceptions
    bubble to main.py's per-item handler."""
    cart_payload = await client.call("get_cart", {"addressId": address_id})
    existing = _extract_cart_items(cart_payload)

    cart_seems_nonempty = bool(existing)
    if not existing and isinstance(cart_payload, str):
        # Unparseable non-JSON cart response: cannot prove the cart is empty,
        # so a replace-write is not safe.
        raise CartSkipped("get_cart response unparseable; skipping write to protect cart")

    already_there = any(
        str(_first_key(line, ID_KEYS)) == product["id"] for line in existing
    )
    if already_there:
        return

    template = existing[0] if cart_seems_nonempty else None
    new_items = existing + [build_cart_line(product, template)]
    await client.call("update_cart", {"addressId": address_id, "items": new_items})
