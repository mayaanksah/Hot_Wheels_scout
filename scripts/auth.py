"""Local OAuth 2.1 + PKCE helper for the Swiggy Instamart MCP server.

Swiggy issues ~5-day access tokens and (as of v1.0) no refresh tokens, so
this script is the manual re-auth step: run it, log in via the browser tab it
opens, and it saves the token to .secrets/token.json (gitignored) and prints
it for pasting into the GitHub Actions SWIGGY_TOKEN secret.

Usage:  python scripts/auth.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

API_BASE = "https://mcp.swiggy.com"
AUTHORIZE_ENDPOINT = f"{API_BASE}/auth/authorize"
TOKEN_ENDPOINT = f"{API_BASE}/auth/token"
REGISTER_ENDPOINT = f"{API_BASE}/auth/register"
SCOPE = "mcp:tools mcp:resources mcp:prompts"
REDIRECT_PORT = 8976  # http://localhost is allowed for local dev per Swiggy's docs
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "token.json"


def register_client(http: httpx.Client) -> str:
    """RFC 7591 dynamic client registration at POST /auth/register (documented
    at mcp.swiggy.com/builders/docs/start/authenticate/ — no static API key)."""
    response = http.post(REGISTER_ENDPOINT, json={
        "client_name": "hot-wheels-scout (personal)",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    response.raise_for_status()
    return response.json()["client_id"]


def wait_for_code() -> str:
    holder: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                holder["code"] = params["code"][0]
                body = b"<h2>Hot Wheels Scout authorized. You can close this tab.</h2>"
            else:
                body = b"<h2>Authorization failed (no code in callback).</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not done.wait(timeout=300):
        server.shutdown()
        sys.exit("Timed out waiting for the browser callback (5 min).")
    server.shutdown()
    if "code" not in holder:
        sys.exit("Callback arrived without an authorization code.")
    return holder["code"]


def main() -> None:
    with httpx.Client(timeout=30, follow_redirects=True) as http:
        client_id = register_client(http)

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()

        auth_params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(16),
            "scope": SCOPE,
        }

        auth_url = f"{AUTHORIZE_ENDPOINT}?{urlencode(auth_params)}"
        print("Opening browser for Swiggy login (phone + OTP)...")
        print(f"(If nothing opens, visit:\n{auth_url}\n)")
        webbrowser.open(auth_url)

        code = wait_for_code()
        response = http.post(TOKEN_ENDPOINT, json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        })
        response.raise_for_status()
        tokens = response.json()

    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    expires_days = tokens.get("expires_in", 0) / 86400
    print(f"\nToken saved to {TOKEN_PATH} (expires in ~{expires_days:.1f} days).")
    print("\nFor the cloud deployment, update the GitHub secret:")
    print("  gh secret set SWIGGY_TOKEN   (paste the token below)")
    print("  — or GitHub repo -> Settings -> Secrets and variables -> Actions\n")
    print(tokens["access_token"])


if __name__ == "__main__":
    main()
