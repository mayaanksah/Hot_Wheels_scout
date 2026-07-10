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

RESOURCE_URL = "https://mcp.swiggy.com/im"
REDIRECT_PORT = 8976  # localhost redirects are whitelisted by Swiggy's manifest
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "token.json"


def discover_auth_server(http: httpx.Client) -> dict:
    """RFC 9728 protected-resource metadata -> RFC 8414 auth-server metadata."""
    base = "https://mcp.swiggy.com"
    resource_candidates = [
        f"{base}/.well-known/oauth-protected-resource/im",
        f"{RESOURCE_URL}/.well-known/oauth-protected-resource",
        f"{base}/.well-known/oauth-protected-resource",
    ]
    auth_server = None
    for url in resource_candidates:
        response = http.get(url)
        if response.status_code == 200:
            servers = response.json().get("authorization_servers") or []
            if servers:
                auth_server = servers[0].rstrip("/")
                break
    if auth_server is None:
        auth_server = base  # fall back to the MCP host itself

    metadata_candidates = [
        f"{auth_server}/.well-known/oauth-authorization-server",
        f"{auth_server}/.well-known/openid-configuration",
    ]
    for url in metadata_candidates:
        response = http.get(url)
        if response.status_code == 200:
            return response.json()
    sys.exit(f"Could not discover OAuth metadata from {auth_server}. "
             "Check https://mcp.swiggy.com/builders docs for changes.")


def register_client(http: httpx.Client, metadata: dict) -> str:
    """RFC 7591 dynamic client registration (Swiggy has no static API keys)."""
    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        sys.exit("Auth server offers no dynamic registration endpoint; "
                 "a pre-registered client_id is required — check Swiggy docs.")
    response = http.post(registration_endpoint, json={
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
        metadata = discover_auth_server(http)
        client_id = register_client(http, metadata)

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
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
            "resource": RESOURCE_URL,  # RFC 8707, required by the MCP auth spec
        }
        if metadata.get("scopes_supported"):
            auth_params["scope"] = " ".join(metadata["scopes_supported"])

        auth_url = f"{metadata['authorization_endpoint']}?{urlencode(auth_params)}"
        print("Opening browser for Swiggy login...")
        print(f"(If nothing opens, visit:\n{auth_url}\n)")
        webbrowser.open(auth_url)

        code = wait_for_code()
        response = http.post(metadata["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE_URL,
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
