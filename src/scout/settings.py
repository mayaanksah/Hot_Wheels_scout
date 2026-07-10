"""Config and secret loading. Secrets come from env vars (GitHub Actions)
or .secrets/token.json written by scripts/auth.py for local runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
STATE_PATH = PROJECT_ROOT / "state.json"
LOCAL_TOKEN_PATH = PROJECT_ROOT / ".secrets" / "token.json"

DEFAULTS = {
    "brands": ["hot wheels"],
    "wishlist": [],
    "auto_add_wishlist": True,
    "auto_add_new_arrivals": True,
    "max_search_pages": 3,
}


def load_config() -> dict:
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        config.update(loaded)
    return config


def load_swiggy_token() -> str | None:
    token = os.environ.get("SWIGGY_TOKEN")
    if token:
        return token.strip()
    if LOCAL_TOKEN_PATH.exists():
        try:
            return json.loads(LOCAL_TOKEN_PATH.read_text(encoding="utf-8"))["access_token"]
        except (json.JSONDecodeError, KeyError):
            return None
    return None


def load_address_id() -> str | None:
    return os.environ.get("SWIGGY_ADDRESS_ID") or None


def load_telegram() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None
