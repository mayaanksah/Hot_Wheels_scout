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
    return {"last_run_utc": None, "seen_products": {}, "flags": {}}


def load_state(path: Path) -> tuple[dict, bool]:
    """Returns (state, seeded). seeded=True means state was missing/corrupt
    and this run must not alert."""
    if not path.exists():
        return empty_state(), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or "seen_products" not in state:
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
