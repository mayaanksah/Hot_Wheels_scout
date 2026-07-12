"""Thin MCP client for the Swiggy Instamart server.

Only allowlisted tools are callable. The server also exposes checkout /
order tools (COD-only, non-cancellable); they are deliberately excluded —
purchasing is outside this bot's hard ceiling (PRD §2, Appendix B).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

INSTAMART_MCP_URL = "https://mcp.swiggy.com/im"

TOOL_ALLOWLIST = frozenset({
    "search_products",
    "get_addresses",
    "get_cart",
    "update_cart",
})

_AUTH_ERROR_MARKERS = ("401", "-32001", "unauthorized", "unauthenticated")


class ToolNotAllowedError(RuntimeError):
    """Raised on any attempt to call a tool outside TOOL_ALLOWLIST."""


class AuthExpiredError(RuntimeError):
    """Swiggy access tokens live ~5 days with no refresh; re-auth is manual."""


class ToolCallError(RuntimeError):
    """The server returned an error result for an allowlisted tool call."""


def looks_like_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _result_payload(result):
    """Best-effort extraction: prefer structured content, then JSON text, then raw text."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, ValueError):
        return joined


class InstamartClient:
    def __init__(self, session: ClientSession):
        self._session = session

    async def call(self, tool: str, arguments: dict):
        if tool not in TOOL_ALLOWLIST:
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
            # On errors Swiggy puts the human-readable reason in the text
            # content while structuredContent is often an empty {}, so prefer
            # the text here (the opposite of the success path).
            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
            if not text:
                payload = _result_payload(result)
                text = payload if isinstance(payload, str) else json.dumps(payload)
            if any(m in text.lower() for m in _AUTH_ERROR_MARKERS):
                raise AuthExpiredError(text)
            raise ToolCallError(f"{tool} failed: {text[:500]}")
        return _result_payload(result)


@asynccontextmanager
async def instamart_client(token: str):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(INSTAMART_MCP_URL, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield InstamartClient(session)
    except (AuthExpiredError, ToolNotAllowedError, ToolCallError):
        raise
    except Exception as exc:
        if looks_like_auth_error(exc):
            raise AuthExpiredError(str(exc)) from exc
        raise
