"""The state store's settings: environment in, one StateConfig out. Passwords come from files only."""

from __future__ import annotations

import os

import pytest
from test_key import KEY_A, env_with, key_file

from vkteams_mcp.config import ConfigError, Settings

PW = "hunter2-correct-horse"


@pytest.fixture
def base(tmp_path):
    return env_with(key_file(tmp_path, KEY_A))


def secret_file(tmp_path, name: str, value: str = PW, mode: int = 0o400):
    f = tmp_path / name
    f.write_text(value + "\n")
    f.chmod(mode)
    return f


def state_of(base, **more):
    return Settings.from_env({**base, **more}).state


def test_nothing_set_is_memory(base):
    s = state_of(base)
    assert s.mode == "memory" and not s.shared and s.prefix == "vkteams:"


def test_a_url_is_standalone(base):
    s = state_of(base, VKTEAMS_MCP_STATE_URL="redis://valkey.example.org:6380/3")
    assert s.mode == "standalone" and s.url == "redis://valkey.example.org:6380/3" and s.db is None


def test_sentinels_are_sentinel_mode_with_a_default_master_and_port(base):
    s = state_of(base, VKTEAMS_MCP_STATE_SENTINELS="s1.example.org:26379, s2.example.org")
    assert s.mode == "sentinel" and s.sentinels == (("s1.example.org", 26379), ("s2.example.org", 26379))
    assert s.master == "mymaster"
    s = state_of(base, VKTEAMS_MCP_STATE_SENTINELS="s1:1", VKTEAMS_MCP_STATE_SENTINEL_MASTER="primary")
    assert s.master == "primary"


def test_a_url_and_sentinels_together_are_a_config_error(base):
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h:6379/0", VKTEAMS_MCP_STATE_SENTINELS="s:26379")
    assert "not both" in str(e.value)


@pytest.mark.parametrize(
    "url", ["http://h:6379/0", "valkey:6379", "redis://", "redis://u:p@h:6379/0", "redis://:p@h/0", "redis://h/0?x=1"]
)
def test_a_url_that_is_not_a_plain_redis_url_is_refused(base, url):
    with pytest.raises(ConfigError):
        state_of(base, VKTEAMS_MCP_STATE_URL=url)


def test_a_password_in_the_url_is_refused_and_not_echoed(base):
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://user:s3cr3tpass@h:6379/0")
    assert "s3cr3tpass" not in str(e.value) and "STATE_PASSWORD_FILE" in str(e.value)


@pytest.mark.parametrize("bad", ["", "h:port", "h:0", "h:70000", ":26379"])
def test_a_bad_sentinel_address_is_refused(base, bad):
    with pytest.raises(ConfigError):
        state_of(base, VKTEAMS_MCP_STATE_SENTINELS=f"ok:26379,{bad}" if bad else " , ")


def test_db_tls_username_and_prefix(base):
    s = state_of(
        base,
        VKTEAMS_MCP_STATE_URL="rediss://h:6379",
        VKTEAMS_MCP_STATE_DB="4",
        VKTEAMS_MCP_STATE_TLS="true",
        VKTEAMS_MCP_STATE_USERNAME="mcp",
        VKTEAMS_MCP_STATE_PREFIX="prod-vk:",
    )
    assert (s.db, s.tls, s.username, s.prefix) == (4, True, "mcp", "prod-vk:")
    with pytest.raises(ConfigError):
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_DB="x")


def test_passwords_are_read_from_files_and_never_shown(base, tmp_path):
    s = state_of(
        base,
        VKTEAMS_MCP_STATE_SENTINELS="s:26379",
        VKTEAMS_MCP_STATE_PASSWORD_FILE=str(secret_file(tmp_path, "pw", PW)),
        VKTEAMS_MCP_STATE_SENTINEL_PASSWORD_FILE=str(secret_file(tmp_path, "spw", "sentinel-pass")),
    )
    assert s.password == PW and s.sentinel_password == "sentinel-pass"  # the trailing newline is not part of it
    shown = repr(
        Settings.from_env(
            {
                **base,
                "VKTEAMS_MCP_STATE_URL": "redis://h",
                "VKTEAMS_MCP_STATE_PASSWORD_FILE": str(secret_file(tmp_path, "pw2")),
            }
        )
    )
    assert PW not in shown and KEY_A not in shown


def test_the_default_password_file_is_not_required(base):
    assert state_of(base, VKTEAMS_MCP_STATE_URL="redis://h").password == ""


def test_a_password_file_that_was_named_but_is_missing_is_an_error(base, tmp_path):
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_PASSWORD_FILE=str(tmp_path / "nope"))
    assert "does not exist" in str(e.value)


@pytest.mark.parametrize("mode", [0o440, 0o444, 0o404, 0o640])
def test_a_password_file_others_can_read_is_refused(base, tmp_path, mode):
    f = secret_file(tmp_path, "pw", mode=mode)
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_PASSWORD_FILE=str(f))
    assert "readable by others" in str(e.value) and PW not in str(e.value)


def test_a_password_file_of_another_user_a_fifo_or_a_huge_file_is_refused(base, tmp_path, monkeypatch):
    f = secret_file(tmp_path, "pw")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(f).st_uid + 1)
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_PASSWORD_FILE=str(f))
    assert "belongs to uid" in str(e.value)
    monkeypatch.undo()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o400)
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_PASSWORD_FILE=str(fifo))
    assert "not a regular file" in str(e.value)
    big = secret_file(tmp_path, "big", "x" * 5000)
    with pytest.raises(ConfigError) as e:
        state_of(base, VKTEAMS_MCP_STATE_URL="redis://h", VKTEAMS_MCP_STATE_PASSWORD_FILE=str(big))
    assert "longer than" in str(e.value)


@pytest.mark.parametrize(
    "name",
    ["VKTEAMS_MCP_STATE_PASSWORD_FILE", "VKTEAMS_MCP_STATE_SENTINEL_PASSWORD_FILE", "VKTEAMS_MCP_STATE_USERNAME"],
)
def test_a_state_setting_without_a_store_is_an_error_not_a_silent_memory_mode(base, tmp_path, name):
    value = str(secret_file(tmp_path, "pw")) if name.endswith("FILE") else "mcp"
    with pytest.raises(ConfigError) as e:
        state_of(base, **{name: value})
    assert "no VKTEAMS_MCP_STATE_URL" in str(e.value)


def test_the_metrics_port_defaults_to_9090_and_is_never_the_public_port(base):
    assert Settings.from_env(base).metrics_port == 9090
    assert Settings.from_env({**base, "VKTEAMS_MCP_METRICS_PORT": "9191"}).metrics_port == 9191
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "PORT": "9090"})
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "VKTEAMS_MCP_METRICS_PORT": "nine"})
