"""Provider-generic MCP recon: dump a provider's tools + sample responses so
field mappings can be finalized against reality (like Swiggy's original M1).

Usage:
  python scripts/recon.py [swiggy|zepto] [--query "hot wheels"]

Read-only: lists tools, calls the discovered address + search tools, and dumps
the CART tool's schema WITHOUT calling it (no writes). Needs a token from
scripts/auth.py <provider> (or the <PROVIDER>_TOKEN env var). Raw JSON lands in
recon_output/<provider>_*.json (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "recon_output"

PROVIDERS = {
    "swiggy": {"url": "https://mcp.swiggy.com/im", "token_file": "token.json",
               "env": "SWIGGY_TOKEN"},
    "zepto": {"url": "https://mcp.zepto.co.in/mcp", "token_file": "zepto_token.json",
              "env": "ZEPTO_TOKEN"},
}


def load_token(cfg: dict) -> str:
    token = os.environ.get(cfg["env"])
    if token:
        return token.strip()
    path = ROOT / ".secrets" / cfg["token_file"]
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["access_token"]
    sys.exit(f"No token. Run scripts/auth.py for this provider first "
             f"(or set {cfg['env']}).")


def dump(provider: str, name: str, payload) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{provider}_{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}")


def _text(result):
    parts = [c.text for c in result.content if getattr(c, "text", None)]
    joined = "\n".join(parts)
    for candidate in (joined, joined[joined.find("{"):] if "{" in joined else ""):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return getattr(result, "structuredContent", None) or joined


import re

_MUTATING = {"create", "update", "delete", "remove", "add", "clear", "checkout",
             "order", "pay", "confirm", "set", "upsert", "place", "shop"}


def _tokens(name: str) -> set[str]:
    # split snake_case and camelCase so "list_saved_addresses" -> {list, saved,
    # addresses} (no "add" token) but "add_saved_address" -> {add, ...}.
    return set(re.split(r"[^a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()))


def _is_mutating(name: str) -> bool:
    return bool(_tokens(name) & _MUTATING)


def _pick(tools, *needles, readonly=False):
    """First tool whose name contains all needles. With readonly=True, skip any
    tool whose name hints at a mutation (recon must never write)."""
    for t in tools:
        name = t.name.lower()
        if all(n in name for n in needles):
            if readonly and _is_mutating(t.name):
                continue
            return t
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", nargs="?", default="swiggy", choices=list(PROVIDERS))
    parser.add_argument("--query", default="hot wheels")
    args = parser.parse_args()
    cfg = PROVIDERS[args.provider]
    token = load_token(cfg)

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(cfg["url"], headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            print(f"== {args.provider} list_tools ==")
            tools = (await session.list_tools()).tools
            schema_dump = [{"name": t.name, "description": t.description,
                            "inputSchema": t.inputSchema} for t in tools]
            dump(args.provider, "tools", schema_dump)
            for t in tools:
                req = t.inputSchema.get("required", []) if t.inputSchema else []
                props = list((t.inputSchema or {}).get("properties", {}).keys())
                print(f"  {t.name:28} required={req} props={props}")

            # read-only picks only — recon never CALLS a mutating tool
            addr_tool = _pick(tools, "address", readonly=True)
            search_tool = _pick(tools, "search", readonly=True)
            cart_tools = [t for t in tools if "cart" in t.name.lower()]

            addresses = None
            if addr_tool:
                print(f"\n== {addr_tool.name} ==")
                try:
                    addresses = _text(await session.call_tool(addr_tool.name, {}))
                    dump(args.provider, "addresses", addresses)
                except Exception as exc:
                    print(f"  {addr_tool.name} failed: {exc}")

            # Session-model providers (Zepto) require selecting a store/address
            # before search returns results. select_saved_address only sets the
            # active delivery context — no cart/order side effects.
            select_tool = _pick(tools, "select", "address")
            aid = _first_address_id(addresses) if addresses else None
            if select_tool and aid:
                id_prop = next((k for k in (select_tool.inputSchema or {}).get("properties", {})
                                if "address" in k.lower() and "id" in k.lower()), "addressId")
                print(f"\n== {select_tool.name}({{{id_prop}: {aid}}}) ==")
                try:
                    print("  ", str(_text(await session.call_tool(select_tool.name, {id_prop: aid})))[:200])
                except Exception as exc:
                    print(f"  {select_tool.name} failed: {exc}")

            if search_tool:
                print(f"\n== {search_tool.name}('{args.query}') ==")
                schema_props = (search_tool.inputSchema or {}).get("properties", {})
                call_args = {}
                for key in schema_props:
                    kl = key.lower()
                    if "query" in kl or "search" in kl or kl in ("q", "keyword", "term"):
                        call_args[key] = args.query
                    elif "offset" in kl or "page" in kl:
                        call_args[key] = 0
                # some providers require an address id on search
                if addresses and any("address" in k.lower() for k in schema_props):
                    aid = _first_address_id(addresses)
                    for key in schema_props:
                        if "address" in key.lower() and aid:
                            call_args[key] = aid
                print(f"  args: {call_args}")
                try:
                    payload = _text(await session.call_tool(search_tool.name, call_args))
                    dump(args.provider, "search_raw", payload)
                except Exception as exc:
                    print(f"  {search_tool.name} failed: {exc}")

            if cart_tools:
                print(f"\n== cart tools (schemas only, NOT called) ==")
                for t in cart_tools:
                    print(f"  {t.name}: {json.dumps(t.inputSchema)[:400]}")


def _first_address_id(addresses):
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in ("id", "address_id", "addressid") and isinstance(v, (str, int)):
                    return str(v)
            for v in o.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(o, list):
            for v in o:
                found = walk(v)
                if found:
                    return found
        return None
    return walk(addresses)


if __name__ == "__main__":
    asyncio.run(main())
