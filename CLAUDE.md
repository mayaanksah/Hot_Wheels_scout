# Hot Wheels Scout

Single-user bot: polls quick-commerce MCP servers for Hot Wheels stock,
Telegram-alerts Maya, auto-adds matches to her cart. Never purchases.
**Multi-provider**: Swiggy Instamart (`https://mcp.swiggy.com/im`) and Zepto
(`https://mcp.zepto.co.in/mcp`), both streamable HTTP + OAuth 2.1 bearer. Each
is a `Provider` in `src/scout/providers/`. The approved plan and per-provider
recon facts live at `~/.claude/plans/prd-spec-swirling-harbor.md`.

## Hard invariants — do not relax

1. **Never purchase.** A provider's `tool_allowlist` (in its
   `src/scout/providers/<name>.py`) lists ONLY search/address/cart tools.
   Never add order/checkout/**payment** tools — anywhere, any provider,
   including tests against a live server. (Zepto exposes autonomous-payment and
   `zepto_shop` intent tools; the allowlist is the guard.)
2. **Cart writes are add-only.** Swiggy `update_cart` replaces the whole cart —
   the merge in `src/scout/cart.py` passes existing lines through untouched and
   skips when they can't be reconstructed. Zepto `update_cart(replaceCart=false)`
   merges natively; skip if the item is already present (don't reset quantity).
3. **No LLM in the polling loop** (keeps recurring cost ₹0).
4. **Secrets never in the repo** (public repo!). Env vars / GitHub Actions
   secrets / gitignored `.secrets/` only. Per provider: `<PROVIDER>_TOKEN`,
   `<PROVIDER>_ADDRESS_IDS`, `<PROVIDER>_CART_ADDRESS_ID`.
5. A failed or empty search must never mass-flip state to out-of-stock; a
   provider's failure must never crash the cycle or affect another provider.
6. Providers' search is **non-deterministic** (drifting result set) — the
   hysteresis in `src/scout/diff.py` (miss/confirm thresholds) is what keeps
   alerts honest. Don't remove it.

## Layout

- `src/scout/providers/` — one module per app (`swiggy.py`, `zepto.py`) behind
  `base.Provider`; `PROVIDERS` registry in `__init__.py`
- `src/scout/mcp_client.py` — generic `mcp_session(url, token, allowlist)` + `Client`
- `src/scout/{search,diff,cart,alerts,state,settings,main}.py` — provider-agnostic
- `scripts/auth.py <provider>` — manual OAuth (Swiggy ~5-day no-refresh; Zepto has refresh)
- `scripts/recon.py <provider>` — provider-generic tool/schema dumps to `recon_output/`
- `tests/` — stdlib unittest: `python -m unittest discover tests` (PYTHONPATH=src)

## State

`state.json` (machine-written, committed by the workflow) is nested
`seen_by_provider[provider][address_id][product_id]`. Missing/corrupt = silent
seed; a newly-added provider or address seeds silently (no first-run alert
burst). Old `seen_by_address` migrates in place under `swiggy`.
