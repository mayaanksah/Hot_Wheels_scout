"""Local OAuth 2.1 + PKCE helper for a provider's MCP server.

Usage:  python scripts/auth.py [swiggy|zepto]   (default: swiggy)

Opens a browser to log in (phone/mobile + OTP), then saves the token to
.secrets/<provider>_token.json (gitignored) and prints the access token for
pasting into the GitHub Actions <PROVIDER>_TOKEN secret. Re-run when a token
expires (Swiggy ~5-day, no refresh; Zepto issues refresh tokens).
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

REDIRECT_PORT = 8976  # http://localhost redirect is allowed for local dev
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SECRETS_DIR = Path(__file__).resolve().parents[1] / ".secrets"

# Endpoints verified live 14 Jul 2026 (Swiggy: mcp.swiggy.com/auth/*, Zepto:
# discovered via auth.zepto.co.in/.well-known/oauth-authorization-server).
PROVIDERS = {
    "swiggy": {
        "authorize": "https://mcp.swiggy.com/auth/authorize",
        "token": "https://mcp.swiggy.com/auth/token",
        "register": "https://mcp.swiggy.com/auth/register",
        "scope": "mcp:tools mcp:resources mcp:prompts",
        "resource": None,
        "token_body": "json",       # Swiggy's token endpoint takes a JSON body
        "grant_types": ["authorization_code"],
        "token_file": "token.json",  # legacy path kept for the live Swiggy setup
        "secret_name": "SWIGGY_TOKEN",
        "login": "phone + OTP",
    },
    "zepto": {
        "authorize": "https://auth.zepto.co.in/authorize",
        "token": "https://auth.zepto.co.in/token",
        "register": "https://auth.zepto.co.in/register",
        "scope": "tools:read tools:write",
        "resource": "https://mcp.zepto.co.in",   # RFC 8707 resource indicator
        "token_body": "form",       # standard OAuth form-encoded token exchange
        "grant_types": ["authorization_code", "refresh_token"],
        "token_file": "zepto_token.json",
        "secret_name": "ZEPTO_TOKEN",
        "login": "mobile + OTP",
    },
}


def register_client(http: httpx.Client, cfg: dict) -> str:
    response = http.post(cfg["register"], json={
        "client_name": "hot-wheels-scout (personal)",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": cfg["grant_types"],
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
    provider = (sys.argv[1] if len(sys.argv) > 1 else "swiggy").lower()
    if provider not in PROVIDERS:
        sys.exit(f"Unknown provider '{provider}'. Choose: {', '.join(PROVIDERS)}")
    cfg = PROVIDERS[provider]

    with httpx.Client(timeout=30, follow_redirects=True) as http:
        client_id = register_client(http, cfg)

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
            "scope": cfg["scope"],
        }
        if cfg["resource"]:
            auth_params["resource"] = cfg["resource"]

        auth_url = f"{cfg['authorize']}?{urlencode(auth_params)}"
        print(f"Opening browser for {provider.title()} login ({cfg['login']})...")
        print(f"(If nothing opens, visit:\n{auth_url}\n)")
        webbrowser.open(auth_url)

        code = wait_for_code()
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        if cfg["resource"]:
            body["resource"] = cfg["resource"]
        kwargs = {"json": body} if cfg["token_body"] == "json" else {"data": body}
        response = http.post(cfg["token"], **kwargs)
        response.raise_for_status()
        tokens = response.json()

    SECRETS_DIR.mkdir(exist_ok=True)
    token_path = SECRETS_DIR / cfg["token_file"]
    token_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    expires_days = tokens.get("expires_in", 0) / 86400
    has_refresh = "yes" if tokens.get("refresh_token") else "no"
    print(f"\nToken saved to {token_path} (expires in ~{expires_days:.1f} days; "
          f"refresh token: {has_refresh}).")
    print(f"\nFor the cloud deployment, set the GitHub secret {cfg['secret_name']} "
          "to the access token below:\n")
    print(tokens["access_token"])


if __name__ == "__main__":
    main()
