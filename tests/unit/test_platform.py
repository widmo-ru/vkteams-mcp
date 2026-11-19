"""What the platform asks of every server: LOG_LEVEL, trusted proxies, a CA bundle."""

from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from conftest import SECRET, public_resolver
from test_http import PUBLIC, authorize, register, vk_sign_in
from test_key import KEY_A, env_with, key_file

from vkteams_mcp import observability as obs
from vkteams_mcp.app import build
from vkteams_mcp.config import ConfigError, Settings
from vkteams_mcp.core import context

# ---------------------------------------------------------------- LOG_LEVEL


@pytest.mark.parametrize(
    ("raw", "want"), [(None, "INFO"), ("", "INFO"), ("debug", "DEBUG"), (" Warning ", "WARNING"), ("loud", "INFO")]
)
def test_log_level_is_read_from_the_unprefixed_variable(raw, want):
    assert obs.log_level({} if raw is None else {"LOG_LEVEL": raw}) == want


def test_the_root_logger_follows_log_level_and_a_bad_one_says_so(monkeypatch, capsys, restore_logging):
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    obs.configure_logging()
    assert logging.getLogger().level == logging.ERROR
    monkeypatch.setenv("LOG_LEVEL", "loud")
    obs.configure_logging()
    assert logging.getLogger().level == logging.INFO
    assert "LOG_LEVEL is not one of" in capsys.readouterr().err


# ---------------------------------------------------------------- trusted proxies


def test_trusted_proxies_are_parsed_and_checked(tmp_path):
    base = env_with(key_file(tmp_path, KEY_A))
    assert Settings.from_env(base).trusted_proxies == ()
    got = Settings.from_env({**base, "VKTEAMS_MCP_TRUSTED_PROXIES": "10.0.0.0/8, 192.168.1.5,fd00::/8"})
    assert got.trusted_proxies == ("10.0.0.0/8", "192.168.1.5", "fd00::/8")
    with pytest.raises(ConfigError) as e:
        Settings.from_env({**base, "VKTEAMS_MCP_TRUSTED_PROXIES": "10.0.0.0/8,not-a-cidr"})
    assert "not-a-cidr" in str(e.value)


async def test_the_limit_per_network_is_off_without_trusted_proxies_and_on_with_them(fake):
    """Behind an ingress every request has the ingress's address: one bucket for the whole company. Off unless the
    operator names the proxies; the limit per address is on either way."""

    async def eleven_addresses(settings: Settings) -> list[bool]:
        app, _, _ = build(settings, http=httpx.AsyncClient(transport=fake.transport()))
        context.runtime().resolve = public_resolver
        context.session_override.set(None)
        vk_sign_in(fake)
        limited = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as web:
            rid = await authorize(web, await register(web))
            for i in range(11):
                r = await web.post(
                    "/signin",
                    data={
                        "request": rid,
                        "step": "address",
                        "server": "teams.example.com",
                        "email": f"p{i}@example.com",
                    },
                )
                limited.append("Too many codes" in r.text)
        return limited

    off = await eleven_addresses(Settings(public_url=PUBLIC, secret=SECRET))
    assert off == [False] * 11
    on = await eleven_addresses(Settings(public_url=PUBLIC, secret=SECRET, trusted_proxies=("127.0.0.1",)))
    assert on == [False] * 10 + [True]
    # the limit per address does not depend on it
    app, _, _ = build(Settings(public_url=PUBLIC, secret=SECRET), http=httpx.AsyncClient(transport=fake.transport()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as web:
        rid = await authorize(web, await register(web))
        seen = [
            "Too many codes"
            in (
                await web.post(
                    "/signin",
                    data={
                        "request": rid,
                        "step": "address",
                        "server": "teams.example.com",
                        "email": "same@example.com",
                    },
                )
            ).text
            for _ in range(4)
        ]
    assert seen == [False, False, False, True]


async def test_uvicorn_trusts_only_the_listed_proxies(monkeypatch, tmp_path, restore_logging):
    import uvicorn

    from vkteams_mcp import app as app_module

    seen = []

    async def record(self, sockets=None):
        seen.append(self.config)

    monkeypatch.setattr(uvicorn.Server, "serve", record)
    f = key_file(tmp_path, KEY_A)
    for proxies, headers in (("", False), ("10.0.0.0/8,192.168.0.1", True)):
        seen.clear()
        s = Settings.from_env(env_with(f, VKTEAMS_MCP_TRUSTED_PROXIES=proxies))
        app, _, _ = build(s, http=httpx.AsyncClient())
        await app_module.serve(app, s)
        public = [c for c in seen if c.port == s.port][0]
        assert public.proxy_headers is headers
        if headers:
            assert public.forwarded_allow_ips == "10.0.0.0/8,192.168.0.1"
        else:
            assert public.forwarded_allow_ips != "*", "the default trusts only loopback; never everyone"


def test_the_start_says_the_network_limit_is_off(tmp_path):
    import signal
    import subprocess
    import sys
    import time
    import urllib.request

    from test_logs import env_for, every_line_is_json, free_port

    port = free_port()
    env = env_for(
        tmp_path,
        PORT=str(port),
        VKTEAMS_MCP_METRICS_PORT=str(free_port()),
        VKTEAMS_MCP_PUBLIC_URL=f"http://127.0.0.1:{port}",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "vkteams_mcp.app"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )  # noqa: S603
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    assert "network_limit_off" in [x["event"] for x in every_line_is_json(out)]


# ---------------------------------------------------------------- CA bundle


def make_ca_and_cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    now = datetime.datetime.now(datetime.UTC)
    ca_key, leaf_key = key(), key()
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("test ca"))
        .issuer_name(name("test ca"))
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name("localhost"))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.key").write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return tmp_path / "ca.pem", tmp_path / "leaf.pem", tmp_path / "leaf.key"


@pytest.fixture
def tls_server(tmp_path):
    ca, leaf, key = make_ca_and_cert(tmp_path)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            return

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield ca, f"https://127.0.0.1:{port}/"
    srv.shutdown()


def test_the_ca_bundle_setting_is_checked(tmp_path):
    base = env_with(key_file(tmp_path, KEY_A))
    assert Settings.from_env(base).ca_bundle == ""
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "VKTEAMS_MCP_CA_BUNDLE": str(tmp_path / "nope.pem")})
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(ConfigError) as e:
        Settings.from_env({**base, "VKTEAMS_MCP_CA_BUNDLE": str(junk)})
    assert "no readable PEM" in str(e.value)


async def test_a_server_behind_an_internal_ca_is_reached_only_with_the_bundle(tls_server):
    ca, url = tls_server
    build(Settings(public_url=PUBLIC, secret=SECRET, policy=policy(True)))
    with pytest.raises(httpx.HTTPError):
        await context.runtime().http.get(url)  # the system's roots do not know the test CA
    build(Settings(public_url=PUBLIC, secret=SECRET, policy=policy(True), ca_bundle=str(ca)))
    r = await context.runtime().http.get(url)
    assert r.status_code == 200 and r.text == "ok"


async def test_the_public_only_transport_uses_the_bundle_too(tls_server):
    from vkteams_mcp.api.egress import PublicOnlyTransport

    ca, url = tls_server
    port = url.rsplit(":", 1)[1].rstrip("/")

    async def resolve(host):
        return "127.0.0.1"  # the name is public as far as this test is concerned; the connection is local

    plain = httpx.AsyncClient(transport=PublicOnlyTransport(resolve=resolve))
    with pytest.raises(httpx.HTTPError):
        await plain.get(f"https://localhost:{port}/")
    ctx = ssl.create_default_context(cafile=str(ca))
    trusting = httpx.AsyncClient(transport=PublicOnlyTransport(resolve=resolve, verify=ctx))
    assert (await trusting.get(f"https://localhost:{port}/")).text == "ok"
    await plain.aclose()
    await trusting.aclose()


def policy(allow_private: bool):
    from vkteams_mcp.api.install import Policy

    return Policy(allow_private=allow_private)
