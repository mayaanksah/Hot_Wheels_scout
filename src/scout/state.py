"""state.json persistence. Missing or corrupt state triggers a silent seed
run (PRD §12): the first cycle after state loss records everything without
alerting, so a wiped host never blasts alerts for the whole catalog."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_state() -> dict:
    # seen_by_provider: {provider: {address_id: {product_id: {...}}}} — stock is
    # per-store within each app, so every provider+address gets its own slice
    # (prevents cross-address / cross-provider flip-flop and false restocks).
    return {"last_run_utc": None, "seen_by_provider": {}, "flags": {}}


def load_state(path: Path) -> tuple[dict, bool]:
    """Returns (state, seeded). seeded=True means the whole state was missing or
    corrupt and this run must reseed silently (PRD §12). The previous
    single-provider `seen_by_address` schema is migrated in place under the
    "swiggy" provider — NOT reseeded — so the live Swiggy baseline is preserved
    (no alert gap)."""
    if not path.exists():
        return empty_state(), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return empty_state(), True
        state.setdefault("flags", {})
        if "seen_by_provider" in state:
            return state, False
        if "seen_by_address" in state:  # migrate v2 -> v3 (Swiggy slice preserved)
            state["seen_by_provider"] = {"swiggy": state.pop("seen_by_address")}
            return state, False
        return empty_state(), True
    except (json.JSONDecodeError, OSError):
        return empty_state(), True


def save_state(path: Path, state: dict) -> None:
    state["last_run_utc"] = utc_now()
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
