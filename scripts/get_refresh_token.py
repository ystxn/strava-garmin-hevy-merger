#!/usr/bin/env python3
"""Mint a Strava refresh token via the OAuth2 authorization-code flow.

This is a one-time bootstrap helper. It opens the Strava consent screen in your
browser, captures the redirect on a tiny local web server, exchanges the code,
and prints the long-lived **refresh token** the service needs — plus ready-to-
paste ``.env`` and ``kubectl`` lines.

Strava setup (one time): in your API application settings
(https://www.strava.com/settings/api) set the **Authorization Callback Domain**
to ``localhost``. Strava only redirects to domains you've registered there.

Usage:
    python3 scripts/get_refresh_token.py --client-id 12345 --client-secret <secret>

Client id/secret may instead come from the environment
(``STRAVA_CLIENT_ID`` / ``STRAVA_CLIENT_SECRET``), a local ``.env`` file, or an
interactive prompt. No third-party packages required — standard library only.

If you're on a headless/SSH box where the browser can't reach this machine, add
``--manual`` and paste the redirected URL by hand.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEFAULT_SCOPE = "activity:read_all,activity:write"


def load_env_file(path: str = ".env") -> dict[str, str]:
    """Best-effort KEY=VALUE reader (no python-dotenv dependency)."""
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    env_file = load_env_file()
    client_id = (
        args.client_id
        or os.environ.get("STRAVA_CLIENT_ID")
        or env_file.get("STRAVA_CLIENT_ID")
    )
    client_secret = (
        args.client_secret
        or os.environ.get("STRAVA_CLIENT_SECRET")
        or env_file.get("STRAVA_CLIENT_SECRET")
    )
    if not client_id:
        client_id = input("Strava Client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("Strava Client Secret (hidden): ").strip()
    if not client_id or not client_secret:
        sys.exit("error: client id and secret are both required")
    return client_id, client_secret


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/callback", "/"):
            # Ignore stray requests (e.g. /favicon.ico) without finishing.
            self.send_response(404)
            self.end_headers()
            return
        self.server.oauth_result = {  # type: ignore[attr-defined]
            k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif'>"
            b"<h2>Strava authorization received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args: object) -> None:  # silence access logs
        pass


def capture_code_via_server(authorize_url: str, port: int) -> dict[str, str]:
    try:
        server = HTTPServer(("localhost", port), _CallbackHandler)
    except OSError as exc:
        sys.exit(
            f"error: could not bind localhost:{port} ({exc}). "
            f"Try a different --port, or re-run with --manual."
        )
    server.oauth_result = None  # type: ignore[attr-defined]

    print(f"\nOpening your browser to authorize... if it doesn't open, visit:\n{authorize_url}\n")
    webbrowser.open(authorize_url)
    print(f"Waiting for the Strava redirect on http://localhost:{port}/callback ...")

    while server.oauth_result is None:  # type: ignore[attr-defined]
        server.handle_request()
    server.server_close()
    return server.oauth_result  # type: ignore[attr-defined]


def capture_code_manually(authorize_url: str) -> dict[str, str]:
    print(f"\nOpen this URL in a browser and authorize:\n\n{authorize_url}\n")
    print(
        "After approving you'll be redirected to a localhost URL that won't "
        "load. Copy it from the address bar and paste it here."
    )
    pasted = input("\nRedirected URL (or just the code): ").strip()
    if pasted.startswith("http"):
        query = urllib.parse.urlparse(pasted).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    return {"code": pasted}


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        sys.exit(f"error: token exchange failed ({exc.code}):\n{body}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: could not reach Strava: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", help="Strava OAuth client ID")
    parser.add_argument("--client-secret", help="Strava OAuth client secret")
    parser.add_argument(
        "--port", type=int, default=8721, help="Local callback port (default 8721)"
    )
    parser.add_argument(
        "--scope", default=DEFAULT_SCOPE, help=f"OAuth scope (default {DEFAULT_SCOPE})"
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip the local server; paste the redirected URL by hand",
    )
    args = parser.parse_args()

    client_id, client_secret = resolve_credentials(args)
    redirect_uri = f"http://localhost:{args.port}/callback"

    authorize_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "approval_prompt": "force",
            "scope": args.scope,
        }
    )

    result = (
        capture_code_manually(authorize_url)
        if args.manual
        else capture_code_via_server(authorize_url, args.port)
    )

    if result.get("error"):
        sys.exit(f"error: authorization denied or failed: {result['error']}")
    code = result.get("code")
    if not code:
        sys.exit(f"error: no authorization code returned (got: {result})")

    granted = result.get("scope", "")
    required = set(args.scope.split(","))
    if granted and not required.issubset(set(granted.split(","))):
        print(
            f"\nWARNING: granted scope '{granted}' is missing one of the "
            f"required scopes '{args.scope}'. The service needs "
            f"activity:read_all and activity:write. Re-run and approve all "
            f"boxes.\n"
        )

    tokens = exchange_code(client_id, client_secret, code)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        sys.exit(f"error: response had no refresh_token: {tokens}")

    athlete = tokens.get("athlete") or {}
    name = " ".join(
        filter(None, [athlete.get("firstname"), athlete.get("lastname")])
    )

    print("\n" + "=" * 70)
    print("SUCCESS — refresh token minted")
    if name or athlete.get("id"):
        print(f"Athlete : {name} (id {athlete.get('id')})")
    print(f"Scope   : {granted or args.scope}")
    print("=" * 70)
    print("\nRefresh token (store this securely — it does not expire):\n")
    print(f"  {refresh_token}\n")
    print("Add to your .env:\n")
    print(f"  STRAVA_REFRESH_TOKEN={refresh_token}\n")
    print("Or set it in the Kubernetes secret:\n")
    print(
        "  kubectl create secret generic strava-merger-secret -n default \\\n"
        f"    --from-literal=STRAVA_CLIENT_ID={client_id} \\\n"
        "    --from-literal=STRAVA_CLIENT_SECRET=<secret> \\\n"
        f"    --from-literal=STRAVA_REFRESH_TOKEN={refresh_token} \\\n"
        "    --from-literal=STRAVA_WEBHOOK_VERIFY_TOKEN=<any-random-string>\n"
    )


if __name__ == "__main__":
    main()
