"""Add-to-cart via get_cart -> merge -> update_cart.

update_cart REPLACES the whole cart (there is no incremental add tool), so
the invariant here is add-only: existing cart lines are passed through
untouched, and if they cannot be faithfully reconstructed the write is
SKIPPED entirely (the alert still goes out). Losing a cart add is
recoverable; clobbering Maya's cart is not.
"""

from __future__ import annotations

from .mcp_client import ToolCallError
from .search import ID_KEYS, SKU_KEYS, SPIN_KEYS, _first_key

QUANTITY_KEYS = ("quantity", "qty", "count")

# update_cart schema confirmed against the LIVE server 13 Jul 2026:
#   selectedAddressId + items:[{"spinId": ..., "skuId": ..., "quantity": N}]
# (docs show only spinId, but the server rejects items without skuId;
#  get_cart, by contrast, takes addressId — Swiggy's naming is inconsistent.)

# Swiggy's get_cart returns an ERROR (not an empty list) when the cart is
# empty — confirmed live 13 Jul 2026: "Cart not found or session expired...
# Please add items to your cart again using update_cart". We treat this
# specific message as a genuinely empty cart (safe to write the first item).
_EMPTY_CART_MARKERS = ("cart not found", "add items to your cart")

# Observed live 13 Jul 2026 around midnight IST: when the dark store is
# closed, update_cart rejects everything ("store is currently unavailable or
# closed", and sometimes "No valid items in cart"). Not a bug in our payload —
# treat as a skip so the alert still fires and the next cycle retries.
_STORE_CLOSED_MARKERS = ("store is currently unavailable", "store is closed",
                         "no valid items in cart")


class CartSkipped(RuntimeError):
    """Cart write was skipped to protect existing cart contents."""


def _is_empty_cart_error(exc: ToolCallError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _EMPTY_CART_MARKERS)


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


def _existing_to_update_items(existing: list[dict]) -> list[dict]:
    """Reduce existing cart lines to update_cart's {spinId, skuId, quantity}
    shape. Raises CartSkipped if any line lacks either id — better to skip
    the add than send a write that would drop a line Maya already had."""
    items = []
    for line in existing:
        spin = _first_key(line, SPIN_KEYS)
        sku = _first_key(line, SKU_KEYS)
        if spin is None or sku is None:
            raise CartSkipped(
                "an existing cart line is missing spinId/skuId; skipping write "
                "to avoid dropping it"
            )
        qty = _first_key(line, QUANTITY_KEYS)
        items.append({"spinId": str(spin), "skuId": str(sku),
                      "quantity": int(qty) if qty else 1})
    return items


async def add_to_cart(client, address_id: str, product: dict) -> None:
    """Raises CartSkipped when a safe merge is impossible; other exceptions
    bubble to main.py's per-item handler."""
    if not product.get("spin_id") or not product.get("sku_id"):
        raise CartSkipped(f"{product['title']} has no spinId/skuId; cannot add to cart")

    try:
        # get_cart's live schema takes no arguments (the cart is session-scoped)
        cart_payload = await client.call("get_cart", {})
    except ToolCallError as exc:
        if not _is_empty_cart_error(exc):
            raise  # a real cart error — don't risk a blind replace-write
        cart_payload = {"items": []}  # empty cart: safe to write the first item

    existing = _extract_cart_items(cart_payload)

    if not existing and isinstance(cart_payload, str):
        # Unparseable non-JSON cart response: cannot prove the cart is empty,
        # so a replace-write is not safe.
        raise CartSkipped("get_cart response unparseable; skipping write to protect cart")

    existing_items = _existing_to_update_items(existing)
    if any(item["spinId"] == product["spin_id"] for item in existing_items):
        return  # already in cart

    new_items = existing_items + [
        {"spinId": product["spin_id"], "skuId": product["sku_id"], "quantity": 1}
    ]
    try:
        await client.call(
            "update_cart", {"selectedAddressId": address_id, "items": new_items}
        )
    except ToolCallError as exc:
        text = str(exc).lower()
        if any(marker in text for marker in _STORE_CLOSED_MARKERS):
            raise CartSkipped(f"store closed / item rejected: {exc}") from exc
        raise
