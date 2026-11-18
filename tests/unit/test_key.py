"""The sealing key: delivered by an init container as a file in a memory-backed volume, read once at
start and deleted, so nothing `kubectl exec` can reach in the server's container holds it. A new key is a
new deploy: every token sealed under the old one is refused, and the client signs in again."""

from __future__ import annotations

import logging
import os
import stat

import httpx
import pytest
from conftest import public_resolver
from test_http import PUBLIC, authorize, full_flow, mcp, register, serve

from vkteams_mcp.config import ConfigError, Settings

KEY_A = "A" * 20 + "k3y-0f-th3-f1rst-d3pl0y" + "a" * 21
KEY_B = "B" * 20 + "k3y-0f-th3-n3xt-d3pl0y" + "b" * 22


def key_file(tmp_path, key: str, mode: int = 0o400):
    d = tmp_path / "key"
    d.mkdir(exist_ok=True)
    f = d / "key"
    f.write_text(key + "\n")
    f.chmod(mode)
    return f


def env_with(f, **more) -> dict[str, str]:
    return {"VKTEAMS_MCP_PUBLIC_URL": PUBLIC, "VKTEAMS_MCP_KEY_FILE": str(f), **more}


def everything_written_under(path) -> list[bytes]:
    out = []
    for root, _, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            if stat.S_ISREG(os.lstat(p).st_mode):
                with open(p, "rb") as fh:
                    out.append(fh.read())
    return out


def test_the_key_is_read_from_the_file_and_the_file_is_deleted(tmp_path):
    f = key_file(tmp_path, KEY_A)
    s = Settings.from_env(env_with(f))
    assert s.secret == KEY_A
    assert not f.exists()
    assert list(f.parent.iterdir()) == []


def test_the_key_is_in_no_environment_variable_file_or_log_line(tmp_path, monkeypatch, caplog):
    """The whole start of the process, as `main()` runs it: settings from the real environment, the app,
    a request. The key must be in memory and nowhere a person with `kubectl exec` can look."""
    caplog.set_level(logging.DEBUG)
    f = key_file(tmp_path, KEY_A)
    monkeypatch.setenv("VKTEAMS_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("VKTEAMS_MCP_KEY_FILE", str(f))
    monkeypatch.chdir(tmp_path)
    from vkteams_mcp.app import build

    s = Settings.from_env()
    app, _, _ = build(s, http=httpx.AsyncClient())
    assert s.secret == KEY_A  # it is in memory
    assert not any(KEY_A in v for v in os.environ.values())
    assert not any(KEY_A.encode() in b for b in everything_written_under(tmp_path))
    assert KEY_A not in repr(s) and KEY_A not in str(s)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY_A not in logged and KEY_A[:16] not in logged


def test_a_missing_key_file_refuses_to_start_and_says_why(tmp_path):
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(tmp_path / "absent"))
    msg = str(e.value)
    assert str(tmp_path / "absent") in msg and "init container" in msg and "delete the pod" in msg


def test_a_key_file_that_cannot_be_deleted_refuses_to_start(tmp_path):
    """A Secret mounted straight into this container is read-only: the key would stay readable to anyone
    with `kubectl exec`, which is exactly what the init container is there to prevent."""
    f = key_file(tmp_path, KEY_A)
    f.parent.chmod(0o500)
    try:
        with pytest.raises(ConfigError) as e:
            Settings.from_env(env_with(f))
    finally:
        f.parent.chmod(0o700)
    assert "could not be deleted" in str(e.value) and "init container" in str(e.value)
    assert KEY_A not in str(e.value)


def test_a_key_path_that_is_not_a_readable_file_says_so_without_a_traceback(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(tmp_path / "dir"))
    assert "could not be read" in str(e.value)


def test_a_key_that_is_not_text_is_refused_and_still_deleted(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(b"\xff\xfe" * 40)
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(f))
    assert "not text" in str(e.value)
    assert not f.exists()


def test_a_key_longer_than_the_limit_is_refused_not_shortened(tmp_path):
    from vkteams_mcp.config import MAX_KEY

    ok = tmp_path / "ok"
    ok.write_text("k" * MAX_KEY)  # the limit counts the file's bytes
    assert Settings.from_env(env_with(ok)).secret == "k" * MAX_KEY
    f = tmp_path / "long"
    f.write_text("k" * (MAX_KEY + 1))
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(f))
    assert "longer than" in str(e.value) and not f.exists()


def test_a_short_key_in_the_file_is_refused_and_still_deleted(tmp_path):
    f = key_file(tmp_path, "too-short")
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(f))
    assert "32" in str(e.value) and "too-short" not in str(e.value)
    assert not f.exists()


def test_a_leftover_secret_variable_still_starts_the_server_with_a_warning(tmp_path, monkeypatch, caplog):
    """One release of grace: a deployment that still passes VKTEAMS_MCP_SECRET keeps running, and keeps
    its sessions, instead of crash-looping on upgrade."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("VKTEAMS_MCP_SECRET", KEY_A)
    env = env_with(tmp_path / "absent", VKTEAMS_MCP_SECRET=KEY_A)
    s = Settings.from_env(env)
    assert s.secret == KEY_A
    warned = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "VKTEAMS_MCP_SECRET" in warned and "next release" in warned and KEY_A not in warned


def test_the_secret_variable_is_dropped_from_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VKTEAMS_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("VKTEAMS_MCP_KEY_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("VKTEAMS_MCP_SECRET", KEY_A)
    assert Settings.from_env().secret == KEY_A
    assert "VKTEAMS_MCP_SECRET" not in os.environ


def test_the_key_file_wins_over_a_leftover_secret_variable(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    f = key_file(tmp_path, KEY_B)
    s = Settings.from_env(env_with(f, VKTEAMS_MCP_SECRET=KEY_A))
    assert s.secret == KEY_B
    warned = "\n".join(r.getMessage() for r in caplog.records)
    assert "ignored" in warned and KEY_A not in warned and KEY_B not in warned


# ------------------------------------------------------------ a deploy, a restart, signing in again


def an_app(fake, key: str):
    from vkteams_mcp.app import build
    from vkteams_mcp.core import context

    app, _, _ = build(Settings(public_url=PUBLIC, secret=key), http=httpx.AsyncClient(transport=fake.transport()))
    context.runtime().resolve = public_resolver
    context.session_override.set(None)
    return app


async def test_a_restart_with_the_same_key_keeps_everyone_signed_in(fake):
    """A pod restarted without a deploy: its init container copies the same Secret again."""
    async with serve(an_app(fake, KEY_A)) as web:
        tok = await full_flow(web, fake)
    async with serve(an_app(fake, KEY_A)) as web:
        assert (await mcp(web, tok["access_token"], "tools/list")).status_code == 200
        r = await web.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": tok["client_id"]},
        )
        assert r.status_code == 200


async def test_a_new_key_signs_everyone_out_once_and_they_sign_in_again(fake):
    """A deploy brings a new key. The old access token gets 401 pointing at the metadata, the refresh is
    refused as an unknown client (so an MCP client drops its registration and registers again), the old
    registration gets the connect-again page, and a fresh registration signs in and works."""
    async with serve(an_app(fake, KEY_A)) as web:
        old = await full_flow(web, fake)
        assert (await mcp(web, old["access_token"], "tools/list")).status_code == 200

    async with serve(an_app(fake, KEY_B)) as web:
        r = await mcp(web, old["access_token"], "tools/list")
        assert r.status_code == 401
        challenge = r.headers.get("www-authenticate", "")
        assert 'error="invalid_token"' in challenge and "resource_metadata=" in challenge

        r = await web.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": old["refresh_token"], "client_id": old["client_id"]},
        )
        assert r.status_code == 401 and r.json()["error"] == "invalid_client"

        page = await web.get(
            "/authorize",
            params={"response_type": "code", "client_id": old["client_id"], "redirect_uri": "https://x.example/cb"},
        )
        assert page.status_code == 400 and "location" not in page.headers

        new = await full_flow(web, fake)
        assert new["client_id"] != old["client_id"]
        assert (await mcp(web, new["access_token"], "tools/list")).status_code == 200


async def test_register_and_authorize_still_work_after_a_new_key(fake):
    async with serve(an_app(fake, KEY_B)) as web:
        cid = await register(web)
        assert await authorize(web, cid)
