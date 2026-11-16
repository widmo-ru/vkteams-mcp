"""Live end-to-end over the real path: a running vkteams-mcp, OAuth 2.1 with PKCE, the person signing in
on the page with the e-mailed code, then tool calls through /mcp with the bearer token.

    1. start the server (see README, "Run locally"), e.g. on http://localhost:8791
    2. uv run python tests/live/e2e_oauth.py login http://localhost:8791     # prints a URL; sign in there
    3. uv run python tests/live/e2e_oauth.py call http://localhost:8791 list_chats '{"filter":"unread"}'

The token is kept in ~/.config/vkteams-mcp/e2e-token.json (mode 600). Nothing here names an installation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

TOKEN_FILE = Path("~/.config/vkteams-mcp/e2e-token.json").expanduser()
CB_PORT = 8792
CB = f"http://localhost:{CB_PORT}/cb"


def login(base: str) -> None:
    reg = httpx.post(f"{base}/register", json={"redirect_uris": [CB], "token_endpoint_auth_method": "none",
                                               "grant_types": ["authorization_code", "refresh_token"],
                                               "response_types": ["code"], "client_name": "e2e"}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(8)
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got.update({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", CB_PORT), H)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    url = f"{base}/authorize?" + urlencode({"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CB,
                                            "code_challenge": challenge, "code_challenge_method": "S256",
                                            "state": state, "resource": f"{base}/mcp"})
    print("Open and sign in:", url, flush=True)
    if os.environ.get("E2E_OPEN") == "1":
        webbrowser.open(url)
    t.join()
    if got.get("state") != state or "code" not in got:
        raise SystemExit("the callback's state does not match this sign-in: refusing to exchange it")
    tok = httpx.post(f"{base}/token", data={"grant_type": "authorization_code", "code": got["code"], "redirect_uri": CB,
                                            "client_id": reg["client_id"], "code_verifier": verifier})
    tok.raise_for_status()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"base": base, "client_id": reg["client_id"], **tok.json()}))
    os.chmod(TOKEN_FILE, 0o600)
    print("token saved; expires_in", tok.json().get("expires_in"))


def rpc(base: str, method: str, params: dict) -> dict:
    saved = json.loads(TOKEN_FILE.read_text())
    if saved["base"].rstrip("/") != base.rstrip("/"):
        raise SystemExit("the saved token belongs to another server; refusing to send it there")
    r = _post(base, saved, method, params)
    if r.status_code == 401:  # the access token expired: rotate with the refresh token, once
        t = httpx.post(f"{base}/token", data={"grant_type": "refresh_token", "refresh_token": saved["refresh_token"],
                                              "client_id": saved["client_id"]})
        t.raise_for_status()
        saved.update(t.json())
        TOKEN_FILE.write_text(json.dumps(saved))
        os.chmod(TOKEN_FILE, 0o600)
        print("access token refreshed", flush=True)
        r = _post(base, saved, method, params)
    r.raise_for_status()
    return r.json()


def _post(base: str, saved: dict, method: str, params: dict) -> httpx.Response:
    tok = saved["access_token"]
    return httpx.post(f"{base}/mcp", headers={"Authorization": f"Bearer {tok}",
                                           "Accept": "application/json, text/event-stream",
                                           "MCP-Protocol-Version": "2025-06-18"},
                   json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)


if __name__ == "__main__":
    cmd, base = sys.argv[1], sys.argv[2].rstrip("/")
    if cmd == "login":
        login(base)
    elif cmd == "list":
        print(json.dumps([t["name"] for t in rpc(base, "tools/list", {})["result"]["tools"]]))
    elif cmd == "call":
        args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        print(json.dumps(rpc(base, "tools/call", {"name": sys.argv[3], "arguments": args}), ensure_ascii=False))
