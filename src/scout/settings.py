"""Config and secret loading. Secrets come from env vars (GitHub Actions) or
.secrets/<provider>_token.json written by scripts/auth.py for local runs.

Per-provider secret conventions (PROVIDER = SWIGGY, ZEPTO, ...):
  <PROVIDER>_TOKEN            access token
  <PROVIDER>_ADDRESS_IDS      comma-separated monitored addresses (cart addr first)
  <PROVIDER>_CART_ADDRESS_ID  the single address that receives auto-adds
A provider is "enabled" when it has a token and at least one address.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
STATE_PATH = PROJECT_ROOT / "state.json"
SECRETS_DIR = PROJECT_ROOT / ".secrets"

DEFAULTS = {
    "brands": ["hot wheels"],
    "wishlist": [],
    "auto_add_wishlist": True,
    "auto_add_new_arrivals": True,
    "max_search_pages": 3,
    # Hysteresis to absorb providers' noisy search (see diff.py).
    "miss_threshold": 3,       # cycles absent before confirming out-of-stock
    "confirm_threshold": 2,    # cycles present before confirming in-stock
}


def load_config() -> dict:
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        config.update(loaded)
    return config


def _token_files(provider: str):
    # swiggy keeps the legacy .secrets/token.json for its live local setup.
    files = [SECRETS_DIR / f"{provider}_token.json"]
    if provider == "swiggy":
        files.append(SECRETS_DIR / "token.json")
    return files


def load_token(provider: str) -> str | None:
    token = os.environ.get(f"{provider.upper()}_TOKEN")
    if token:
        return token.strip()
    for path in _token_files(provider):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))["access_token"]
            except (json.JSONDecodeError, KeyError):
                continue
    return None


def load_address_ids(provider: str) -> list[str]:
    """Monitored addresses for a provider, order preserved. Prefers
    <PROVIDER>_ADDRESS_IDS; falls back to a single <PROVIDER>_ADDRESS_ID."""
    raw = os.environ.get(f"{provider.upper()}_ADDRESS_IDS")
    if raw:
        ids = [a.strip() for a in raw.split(",") if a.strip()]
        if ids:
            return ids
    single = os.environ.get(f"{provider.upper()}_ADDRESS_ID")
    return [single.strip()] if single else []


def load_cart_address_id(provider: str) -> str | None:
    """The one address that receives auto-adds; defaults to the first
    monitored address when <PROVIDER>_CART_ADDRESS_ID is unset."""
    explicit = os.environ.get(f"{provider.upper()}_CART_ADDRESS_ID")
    if explicit:
        return explicit.strip()
    ids = load_address_ids(provider)
    return ids[0] if ids else None


def enabled_providers() -> list[str]:
    """Provider names that have both a token and at least one address."""
    from .providers import PROVIDERS
    return [name for name in PROVIDERS
            if load_token(name) and load_address_ids(name)]


def load_telegram() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None
