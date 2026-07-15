"""Provider abstraction: each quick-commerce app (Swiggy, Zepto) implements the
same interface so the orchestration in main.py is provider-agnostic.

A provider owns everything app-specific: its MCP URL, its tool allowlist (only
search/address/cart tools — NEVER order/checkout/payment), how it searches, how
it lists addresses, and how it adds to cart. Normalized products share one
shape (`id`, `title`, `price`, `image`, `in_stock`, plus provider cart ids).
"""

from __future__ import annotations

from .swiggy import SwiggyProvider
from .zepto import ZeptoProvider

PROVIDERS = {p.name: p for p in (SwiggyProvider(), ZeptoProvider())}


def get_provider(name: str):
    return PROVIDERS[name]
