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
    # seen_by_address: {address_id: {product_id: {...}}} — stock is per-store,
    # so each monitored address gets its own slice (prevents cross-address
    # flip-flop / false restock storms).
    return {"last_run_utc": None, "seen_by_address": {}, "flags": {}}


def load_state(path: Path) -> tuple[dict, bool]:
    """Returns (state, seeded). seeded=True means state was missing/corrupt
    OR in the old flat single-address schema — in every case this run must
    reseed silently (no alert storm), consistent with PRD §12."""
    if not path.exists():
        return empty_state(), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or "seen_by_address" not in state:
            return empty_state(), True
        state.setdefault("flags", {})
        return state, False
    except (json.JSONDecodeError, OSError):
        return empty_state(), True


def save_state(path: Path, state: dict) -> None:
    state["last_run_utc"] = utc_now()
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
