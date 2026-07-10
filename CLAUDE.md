# Hot Wheels Scout

Single-user bot: polls Swiggy Instamart's MCP server (`https://mcp.swiggy.com/im`,
streamable HTTP + OAuth 2.1 bearer) for Hot Wheels stock, Telegram-alerts Maya,
auto-adds matches to her cart. PRD assumptions were verified 10 Jul 2026; the
approved plan lives at `~/.claude/plans/prd-spec-swirling-harbor.md`.

## Hard invariants — do not relax

1. **Never purchase.** Only `TOOL_ALLOWLIST` in `src/scout/mcp_client.py`
   (search_products, get_addresses, get_cart, update_cart) may be called.
   Never add checkout/order tools, anywhere, including tests against the live server.
2. **Cart writes are add-only.** `update_cart` REPLACES the whole cart; the
   merge in `src/scout/cart.py` must pass existing lines through untouched and
   skip the write when they can't be reconstructed.
3. **No LLM in the polling loop** (keeps recurring cost ₹0).
4. **Secrets never in the repo** (public repo!). Env vars / GitHub Actions
   secrets / gitignored `.secrets/` only.
5. A failed or empty search must never mass-flip state to out-of-stock
   (false restock storms) and must never crash the schedule.

## Layout

- `src/scout/` — bot modules; run as `python -m scout.main` with `PYTHONPATH=src`
- `scripts/auth.py` — manual OAuth (tokens last ~5 days, no refresh)
- `scripts/recon.py` — dumps raw MCP responses to `recon_output/`
- `tests/` — stdlib unittest: `python -m unittest discover tests`
- Field-name candidates for Swiggy's unpublished response schemas live at the
  top of `src/scout/search.py`; tighten them from recon output, don't guess.

## State

`state.json` is machine-written and committed back by the workflow. Missing or
corrupt state = silent seed cycle (no alerts).
