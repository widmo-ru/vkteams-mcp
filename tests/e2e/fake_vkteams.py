"""A VK Teams installation for the chart's end-to-end run: it answers the three routes the sign-in uses, over TLS.

It is the shapes of tests/unit/conftest.py (recorded from a live server, docs/live-findings.md) behind a real
socket, so the server in the kind cluster signs a person in with no real VK Teams. Test code, never in the image.

    FAKE_HOST=fake-vk.ns.svc.cluster.local python fake_vkteams.py /tls/tls.crt /tls/tls.key

Every request is logged as one JSON line (path, and the tokenType or "code" for clientLogin), so the run can
count what reached "VK Teams": a sign-in refused by the server must leave no clientLogin here.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = os.environ["FAKE_HOST"]
API = 139
CODE = "AB12CD"  # what the e-mailed code is, as test_http.py's fake has it
ME = "e2e.person@example.com"


def wim(data: dict | None = None, code: int = 200, text: str = "Ok") -> dict:
    return {"response": {"statusCode": code, "statusText": text, "data": data or {}}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # the default access log is not JSON
        return

    def note(self, **fields) -> None:
        print(json.dumps({"ts": time.time(), "path": urlsplit(self.path).path, **fields}), flush=True)

    def send_json(self, body: dict, status: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        self.note(method="GET")
        if path == "/myteam-config.json":
            self.send_json(
                {
                    "api-version": API,
                    "api-urls": {"main-api": f"https://{HOST}", "main-binary-api": f"https://{HOST}"},
                    "is-on-premise": True,
                }
            )
        else:
            self.send_json({}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        form = {
            k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).items()
        }
        if path == f"/api/v{API}/wim/auth/clientLogin":
            if form.get("tokenType") == "otp_via_email":
                self.note(method="POST", step="send_code")
                self.send_json(wim({"token": {"a": ""}}))
            elif form.get("pwd") == CODE:
                self.note(method="POST", step="code_ok")
                self.send_json(
                    wim(
                        {
                            "hostTime": int(time.time()),
                            "token": {"a": "e2e-token", "expiresIn": 0},
                            "sessionSecret": "e2e-secret",
                            "loginId": ME,
                        }
                    )
                )
            else:
                self.note(method="POST", step="code_wrong")
                self.send_json(wim(code=330, text="Password/LoginId Required/Invalid"))
        elif path == f"/api/v{API}/wim/aim/startSession":
            self.note(method="POST", step="start_session")
            self.send_json(
                wim(
                    {
                        "aimsid": "e2e-aimsid",
                        "fetchBaseURL": f"https://{HOST}/api/v{API}/bos/n/aim/fetchEvents?aimsid=e2e-aimsid",
                        "myInfo": {"aimId": ME, "friendly": "E2E Person"},
                    }
                )
            )
        else:
            self.note(method="POST")
            self.send_json({}, 404)


def main() -> None:
    cert, key = sys.argv[1:3]
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)  # noqa: S104
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"ts": time.time(), "event": "listening", "host": HOST}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
