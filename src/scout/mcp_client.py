"""Generic MCP client shared by all providers.

A session is opened against a provider's MCP URL with its bearer token, and
every `call` is checked against that provider's allowlist. Order/checkout/
payment tools are never in any provider's allowlist — purchasing is outside
this bot's hard ceiling (PRD §2, Appendix B).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_AUTH_ERROR_MARKERS = ("401", "-32001", "unauthorized", "unauthenticated")


class ToolNotAllowedError(RuntimeError):
    """Raised on any attempt to call a tool outside the provider allowlist."""


class AuthExpiredError(RuntimeError):
    """Access token expired/revoked; re-auth needed (Swiggy ~5-day no-refresh)."""


class ToolCallError(RuntimeError):
    """The server returned an error result for an allowlisted tool call."""


def looks_like_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _result_payload(result):
    """Best-effort extraction: prefer structured content, then JSON text, then raw text."""
    structured = getattr(result, "structuredContent", None)
    if structured:  # observed {} on some responses — treat as absent
        return structured
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, ValueError):
        pass
    # Some tools (e.g. Swiggy get_cart) prefix the JSON body with LLM display
    # instructions ("...Data:\n{...}") — parse from the first brace.
    brace = joined.find("{")
    if brace >= 0:
        try:
            return json.loads(joined[brace:])
        except (json.JSONDecodeError, ValueError):
            pass
    return joined


class Client:
    """MCP session wrapper that enforces a per-provider tool allowlist."""

    def __init__(self, session: ClientSession, allowlist: frozenset[str]):
        self._session = session
        self._allowlist = allowlist

    async def call(self, tool: str, arguments: dict):
        if tool not in self._allowlist:
            raise ToolNotAllowedError(
                f"Tool '{tool}' is not allowlisted. This bot never places orders."
            )
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as exc:
            if looks_like_auth_error(exc):
                raise AuthExpiredError(str(exc)) from exc
            raise
        if getattr(result, "isError", False):
            # On errors the human-readable reason is in the text content while
            # structuredContent is often an empty {}, so prefer the text here
            # (the opposite of the success path).
            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
            if not text:
                payload = _result_payload(result)
                text = payload if isinstance(payload, str) else json.dumps(payload)
            if any(m in text.lower() for m in _AUTH_ERROR_MARKERS):
                raise AuthExpiredError(text)
            raise ToolCallError(f"{tool} failed: {text[:500]}")
        return _result_payload(result)


@asynccontextmanager
async def mcp_session(url: str, token: str, allowlist: frozenset[str]):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield Client(session, allowlist)
    except (AuthExpiredError, ToolNotAllowedError, ToolCallError):
        raise
    except Exception as exc:
        if looks_like_auth_error(exc):
            raise AuthExpiredError(str(exc)) from exc
        raise
