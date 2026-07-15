# Hot Wheels Scout 🏎️

Watches Swiggy Instamart for new Hot Wheels arrivals and wishlist restocks,
alerts on Telegram, and auto-adds matches to the cart. **Never purchases** —
the checkout tool is excluded by a hard allowlist in
[src/scout/mcp_client.py](src/scout/mcp_client.py); ordering is impossible by
construction.

Runs free: GitHub Actions cron (~5 min, best-effort) + direct MCP calls
(no LLM in the loop) + Telegram Bot API.

## One-time setup

### 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2. Telegram bot

1. Message **@BotFather** → `/newbot` → copy the **bot token**.
2. Message your new bot once (any text), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your **chat id**.

### 3. Swiggy auth (repeats every ~5 days)

Swiggy MCP access tokens last ~5 days and there are no refresh tokens (v1.0).

```powershell
.\.venv\Scripts\python scripts\auth.py
```

Log in via the browser tab. The token is saved to `.secrets/token.json`
(gitignored) and printed for the GitHub secret. When the token expires, the
bot sends a Telegram "re-auth needed" alert — rerun this script and update
the `SWIGGY_TOKEN` secret.

### 4. Recon (M1 — do this before trusting anything)

```powershell
.\.venv\Scripts\python scripts\recon.py --cart
```

This dumps raw responses to `recon_output/` and prints what the field mapping
in [src/scout/search.py](src/scout/search.py) could parse. **If parsing looks
wrong, adjust the `*_KEYS` candidate lists at the top of search.py using
`recon_output/search_raw.json`.** Note the printed `addressId`.

### 5. Local trial run

```powershell
$env:PYTHONPATH="src"
$env:SWIGGY_ADDRESS_ID="<from recon>"
$env:TELEGRAM_BOT_TOKEN="<bot token>"
$env:TELEGRAM_CHAT_ID="<chat id>"
.\.venv\Scripts\python -m scout.main   # first run seeds state.json silently
.\.venv\Scripts\python -m scout.main   # second run should alert nothing (idempotent)
```

### 6. Deploy to GitHub (₹0 hosting)

The repo must be **public** for unlimited free Actions minutes (the 5-min
cadence needs ~8600 min/month; private repos cap at 2000). Nothing secret
lives in the repo — only config, code, and state.

1. Create a **public** repo on github.com, then:
   ```powershell
   git remote add origin https://github.com/<you>/hot-wheels-scout.git
   git push -u origin main
   ```
2. Repo → Settings → Secrets and variables → Actions → add:
   `SWIGGY_TOKEN`, `SWIGGY_ADDRESS_IDS`, `SWIGGY_CART_ADDRESS_ID`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Actions tab → enable workflows → run **scout** once via *Run workflow*
   (this button is also your manual force-check during a hot drop).

### Adding Zepto (second provider)

The bot is multi-provider. Zepto (official MCP) is added the same way as Swiggy:

1. `python scripts/auth.py zepto` — log in (mobile + OTP), saves
   `.secrets/zepto_token.json`.
2. `python scripts/recon.py zepto` — dumps Zepto's tools/addresses (read-only).
3. Add GitHub secrets `ZEPTO_TOKEN`, `ZEPTO_ADDRESS_IDS` (comma-separated Zepto
   address UUIDs from recon, cart address first), `ZEPTO_CART_ADDRESS_ID`.

A provider is active only when it has **both** a token and addresses, so Zepto
stays dormant until you set those. Each provider has its own ~5-day token ritual
and its own "re-auth needed" alert. Alerts are labelled with the app
(e.g. "New arrival — Hot Wheels (Zepto)").

### Addresses (multi-store monitoring)

Instamart stock is per-store, and each store is tied to a delivery address, so
monitoring several addresses widens the net for catching a rare car.

- `SWIGGY_ADDRESS_IDS` — comma-separated address ids to monitor, e.g.
  `d8m0…,d6db…,d900…`. Get ids from `scripts/recon.py`. **Addresses live in
  secrets, not `config.yaml`** (the repo is public; addresses are mildly
  private).
- `SWIGGY_CART_ADDRESS_ID` — the single address whose cart receives auto-adds
  (you can only check out one order from one address). Defaults to the first id
  in `SWIGGY_ADDRESS_IDS`. Cars in stock only at the *other* monitored
  addresses still alert you — they're just not auto-added.
- Legacy `SWIGGY_ADDRESS_ID` (single) is still read as a fallback.

Changing the address set reshapes `state.json`, so the next run reseeds
silently (no alerts that one cycle), then alerting resumes.

## Configuration

Edit [config.yaml](config.yaml): brand allow-list, wishlist keywords, and the
auto-add flags (`auto_add_wishlist`, `auto_add_new_arrivals` — both on).
Cadence lives in [.github/workflows/scout.yml](.github/workflows/scout.yml).

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover tests
```

## Known limits

- **~5-day re-auth ritual** (Swiggy token lifetime, no refresh tokens).
- GitHub cron is best-effort; delays of 10–30 min happen at peak.
- Scheduled workflows pause after 60 days without repo activity; the bot's
  own `state.json` commits normally keep it alive, but check after holidays.
- `update_cart` replaces the whole cart, so cart writes are add-only and are
  skipped entirely if existing cart lines can't be read back safely
  (the alert still arrives).
- Field mapping for search/cart responses is finalized from recon output
  (Swiggy doesn't publish schemas); schema drift triggers a loud Telegram
  warning rather than silent mis-parsing.
