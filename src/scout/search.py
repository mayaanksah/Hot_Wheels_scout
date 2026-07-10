"""Search + normalization.

The search_products response schema is not published by Swiggy (PRD A3/A4),
so normalization probes candidate field names and the raw shape is dumped by
scripts/recon.py. After M1 recon, tighten the candidate lists below to the
real field names. If no product id/name can be found at all, SchemaDriftError
is raised so main.py can fail loudly to Telegram instead of mis-parsing.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

ID_KEYS = ("product_id", "productId", "id", "item_id", "itemId", "spin", "sku")
NAME_KEYS = ("display_name", "displayName", "name", "title", "product_name", "productName")
BRAND_KEYS = ("brand", "brand_name", "brandName")
PRICE_KEYS = ("offer_price", "offerPrice", "price", "selling_price", "sellingPrice",
              "final_price", "store_price", "mrp")
IMAGE_KEYS = ("image_url", "imageUrl", "image", "images", "image_id", "imageId", "thumbnail")
STOCK_TRUE_KEYS = ("in_stock", "inStock", "available", "is_available", "isAvailable")
STOCK_FALSE_KEYS = ("out_of_stock", "outOfStock", "sold_out", "soldOut")
VARIANT_KEYS = ("variations", "variants")
LIST_CONTAINER_KEYS = ("products", "items", "results", "data", "catalog", "content", "widgets")

# Instamart serves images by id from this CDN; recon confirms whether search
# returns full URLs or bare image ids.
SWIGGY_IMAGE_CDN = "https://media-assets.swiggy.com/swiggy/image/upload/"


class SchemaDriftError(RuntimeError):
    """Response shape changed / unrecognizable — alert loudly, never mis-parse."""


def _first_key(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def extract_products(payload) -> list[dict]:
    """BFS through an unknown response shape for the first list of dicts that
    look like products (i.e. have a name-ish field)."""
    queue = [payload]
    depth = 0
    while queue and depth < 6:
        next_queue = []
        for node in queue:
            if isinstance(node, list):
                dicts = [x for x in node if isinstance(x, dict)]
                if dicts and any(_first_key(d, NAME_KEYS) for d in dicts):
                    return dicts
                next_queue.extend(dicts)
            elif isinstance(node, dict):
                for key in LIST_CONTAINER_KEYS:
                    if key in node:
                        next_queue.append(node[key])
                next_queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        queue = next_queue
        depth += 1
    return []


def _extract_price(d: dict):
    value = _first_key(d, PRICE_KEYS)
    if isinstance(value, dict):
        value = _first_key(value, PRICE_KEYS)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        match = re.search(r"[\d.]+", value.replace(",", ""))
        return float(match.group()) if match else None
    return None


def _extract_image(d: dict):
    value = _first_key(d, IMAGE_KEYS)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = _first_key(value, IMAGE_KEYS + ("url",))
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith("http") else SWIGGY_IMAGE_CDN + value


def _extract_in_stock(d: dict) -> bool:
    for k in STOCK_FALSE_KEYS:
        if isinstance(d.get(k), bool):
            return not d[k]
    for k in STOCK_TRUE_KEYS:
        value = d.get(k)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "yes", "in_stock", "available")
    # Being returned by search at this address is itself an availability
    # signal; absence from results is treated as out of stock (PRD FR-2).
    return True


def normalize_product(raw: dict) -> dict | None:
    """Flatten one raw product (merging its first variant, where prices/stock
    often live) into the fields the bot uses. Returns None if unusable."""
    merged = dict(raw)
    variants = _first_key(raw, VARIANT_KEYS)
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        in_stock_variants = [v for v in variants if _extract_in_stock(v)]
        chosen = in_stock_variants[0] if in_stock_variants else variants[0]
        merged = {**raw, **chosen}
        merged["_any_variant_in_stock"] = bool(in_stock_variants)

    product_id = _first_key(merged, ID_KEYS)
    name = _first_key(merged, NAME_KEYS)
    if product_id is None or name is None:
        return None

    if "_any_variant_in_stock" in merged:
        in_stock = merged["_any_variant_in_stock"]
    else:
        in_stock = _extract_in_stock(merged)

    return {
        "id": str(product_id),
        "title": str(name),
        "brand": str(_first_key(merged, BRAND_KEYS) or ""),
        "price": _extract_price(merged),
        "image": _extract_image(merged),
        "in_stock": in_stock,
        "raw_id_key": next((k for k in ID_KEYS if k in merged), None),
    }


def matches_brands(product: dict, brands: list[str]) -> bool:
    haystack = f"{product['title']} {product['brand']}".lower()
    return any(b.lower() in haystack for b in brands)


def _tokens(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def matches_wishlist(title: str, wishlist: list[str]) -> str | None:
    """Returns the matching wishlist entry (every token of the entry appears
    in the title), or None."""
    title_tokens = _tokens(title)
    for entry in wishlist:
        entry_tokens = _tokens(entry)
        if entry_tokens and entry_tokens <= title_tokens:
            return entry
    return None


def product_link(product: dict) -> str:
    # No documented product/cart deep-link (PRD A4) — a search link is the
    # reliable fallback that opens Instamart on the right query.
    return "https://www.swiggy.com/instamart/search?custom_back=true&query=" + quote_plus(product["title"])


async def search_brand(client, address_id: str, keyword: str, max_pages: int = 3) -> list[dict]:
    """Paginated search_products for one brand keyword; returns normalized,
    deduped products. Raises SchemaDriftError if results exist but none are
    parseable."""
    found: dict[str, dict] = {}
    raw_count = 0
    for page in range(max_pages):
        payload = await client.call(
            "search_products",
            {"addressId": address_id, "query": keyword, "offset": page * 20},
        )
        raw_products = extract_products(payload)
        if not raw_products:
            break
        raw_count += len(raw_products)
        normalized = [p for p in (normalize_product(r) for r in raw_products) if p]
        new_ids = {p["id"] for p in normalized} - found.keys()
        for p in normalized:
            found.setdefault(p["id"], p)
        if not new_ids:  # page repeated itself — stop paginating
            break
    if raw_count and not found:
        raise SchemaDriftError(
            f"search_products returned {raw_count} items for '{keyword}' but none "
            "were parseable — field mapping in search.py needs updating."
        )
    return list(found.values())
