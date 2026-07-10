"""M1 recon: dump raw Instamart MCP responses so the field mapping in
src/scout/search.py can be finalized against reality (PRD A3/A4).

Usage:
  python scripts/recon.py                    # list tools + addresses, search "hot wheels"
  python scripts/recon.py --address-id X     # search at a specific address
  python scripts/recon.py --query "matchbox" --cart   # custom query, also dump get_cart

Requires a token from scripts/auth.py (or SWIGGY_TOKEN env var).
Raw JSON lands in recon_output/ (gitignored). Read-only except nothing —
this script never writes to the cart.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scout.mcp_client import instamart_client  # noqa: E402
from scout.search import extract_products, normalize_product  # noqa: E402
from scout.settings import load_swiggy_token  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "recon_output"


def dump(name: str, payload) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    print(f"  wrote {path}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address-id", default=None)
    parser.add_argument("--query", default="hot wheels")
    parser.add_argument("--cart", action="store_true", help="also dump get_cart")
    args = parser.parse_args()

    token = load_swiggy_token()
    if not token:
        sys.exit("No token. Run scripts/auth.py first (or set SWIGGY_TOKEN).")

    async with instamart_client(token) as client:
        print("== get_addresses ==")
        addresses = await client.call("get_addresses", {})
        dump("addresses", addresses)
        print(json.dumps(addresses, indent=2, default=str)[:2000])

        address_id = args.address_id
        if not address_id:
            found = extract_products(addresses)  # generic list-finder works here too
            if found:
                for key in ("address_id", "addressId", "id"):
                    if key in found[0]:
                        address_id = str(found[0][key])
                        break
        if not address_id:
            sys.exit("Could not auto-pick an addressId — rerun with --address-id "
                     "using the dump above.")
        print(f"\nUsing addressId={address_id}  (set SWIGGY_ADDRESS_ID to this)")

        print(f"\n== search_products('{args.query}') ==")
        payload = await client.call(
            "search_products", {"addressId": address_id, "query": args.query, "offset": 0}
        )
        dump("search_raw", payload)

        raw = extract_products(payload)
        print(f"extract_products found {len(raw)} raw items")
        normalized = [p for p in (normalize_product(r) for r in raw) if p]
        print(f"normalize_product parsed {len(normalized)} of them:")
        for p in normalized[:10]:
            stock = "IN STOCK" if p["in_stock"] else "out of stock"
            print(f"  [{p['id']}] {p['title']} | Rs.{p['price']} | {stock} | image={'yes' if p['image'] else 'no'}")
        if raw and not normalized:
            print("!! Raw items exist but none normalized — update field name")
            print("   candidates at the top of src/scout/search.py using search_raw.json")

        if args.cart:
            print("\n== get_cart ==")
            cart = await client.call("get_cart", {"addressId": address_id})
            dump("cart_raw", cart)


if __name__ == "__main__":
    asyncio.run(main())
