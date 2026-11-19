"""Logs are JSON only: every line every process of the image writes parses, and the count is not zero.

The in-process tests pin the formatter; the subprocess tests run the real things (the server, the init
container's staging, a start that fails, an exception nobody catches) and `json.loads` every line they print.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vkteams_mcp import observability as obs

KEY = "k" * 48
RFC3339_MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def record(msg="hello", level=logging.INFO, name="vkteams_mcp", exc_info=None, **extra):
    r = logging.LogRecord(name, level, __file__, 1, msg, (), exc_info)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def line(**kw) -> dict:
    out = obs.JsonFormatter().format(record(**kw))
    assert "\n" not in out, "a record is one line"
    return json.loads(out)


# ---------------------------------------------------------------- the formatter


def test_a_record_has_the_common_fields():
    j = line(msg="started", event="start", mode="memory")
    assert RFC3339_MS.match(j["ts"]) and j["level"] == "INFO" and j["logger"] == "vkteams_mcp"
    assert j["event"] == "start" and j["msg"] == "started" and j["mode"] == "memory"


def test_a_message_with_quotes_newlines_and_unicode_is_still_one_valid_line():
    nasty = 'he said "stop"\nthen {"level":"FATAL"} \\ \t and Привет \u2028 end'
    assert line(msg=nasty)["msg"] == nasty


def test_without_an_event_the_logger_name_is_the_event():
    assert line(name="mcp.server")["event"] == "mcp.server"


def test_only_allowlisted_fields_are_carried():
    j = line(msg="x", tool="get_chat", person="ann@example.com", chat_title="Project Alpha", arbitrary="x")
    assert j["tool"] == "get_chat" and "person" not in j and "chat_title" not in j and "arbitrary" not in j


def test_event_refuses_a_field_that_is_not_allowlisted():
    with pytest.raises(KeyError):
        obs.event(logging.getLogger("x"), logging.INFO, "e", "m", person="ann")


def test_a_traceback_is_frames_and_types_never_messages():
    try:
        try:
            raise ValueError("the chat Project Alpha for ann@example.com")
        except ValueError as inner:
            raise RuntimeError("second: secret budget") from inner
    except RuntimeError:
        j = line(msg="boom", level=logging.ERROR, exc_info=sys.exc_info())
    assert "ValueError" in j["exc"] and "RuntimeError" in j["exc"] and "test_logs.py" in j["exc"]
    for secret in ("Project Alpha", "ann@example.com", "secret budget"):
        assert secret not in j["exc"] and secret not in json.dumps(j)


def test_a_long_message_is_cut():
    assert len(line(msg="x" * 10_000)["msg"]) == obs.MAX_MSG


@pytest.mark.parametrize(
    "content",
    [
        "sent to ann@example.com",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
        "blob " + "A1b2C3d4-" * 12,
        "aimsid=0123456789abcdef",
    ],
)
def test_a_record_that_looks_like_content_is_dropped_and_counted(content):
    before = obs.LOG_DROPPED.labels(obs.SERVER)._value.get()
    assert obs.ContentFilter().filter(record(msg=content)) is False
    assert obs.LOG_DROPPED.labels(obs.SERVER)._value.get() == before + 1
    assert obs.ContentFilter().filter(record(msg="tool get_chat refused: confirm")) is True


def test_a_configured_process_writes_json_for_every_logger_and_for_warnings(capsys, restore_logging):
    import warnings

    obs.configure_logging()
    logging.getLogger("uvicorn.error").warning("uvicorn says %s", 'a "quote"')
    logging.getLogger("mcp.server").error("sdk says\nmore")
    warnings.warn("careful", UserWarning, stacklevel=1)
    lines = [x for x in capsys.readouterr().err.splitlines() if x]
    assert len(lines) >= 3
    parsed = [json.loads(x) for x in lines]
    assert {"uvicorn.error", "mcp.server"} <= {p["logger"] for p in parsed}
    assert any(p["logger"] == "py.warnings" for p in parsed)


# ---------------------------------------------------------------- the real processes


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    key = tmp_path / "key"
    if not key.exists():
        key.write_text(KEY)
        key.chmod(0o400)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VKTEAMS_MCP_")}
    env.update(
        PYTHONUNBUFFERED="1",
        LOG_LEVEL="INFO",
        VKTEAMS_MCP_KEY_FILE=str(key),
        VKTEAMS_MCP_PUBLIC_URL="http://localhost:18999",
    )
    env.update(extra)
    return env


def every_line_is_json(text: str) -> list[dict]:
    lines = [x for x in text.splitlines() if x.strip()]
    assert lines, "no output at all: the check would pass vacuously"
    out = []
    for x in lines:
        try:
            out.append(json.loads(x))
        except ValueError:
            pytest.fail(f"not JSON: {x!r}")
        assert isinstance(out[-1], dict) and RFC3339_MS.match(out[-1]["ts"]) and out[-1]["event"], x
    return out


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603


def test_a_start_that_fails_prints_one_json_line_and_exits_2(tmp_path):
    env = env_for(tmp_path)
    env["VKTEAMS_MCP_KEY_FILE"] = str(tmp_path / "absent")
    p = run(["-m", "vkteams_mcp.app"], env)
    assert p.returncode == 2
    lines = every_line_is_json(p.stdout + p.stderr)
    assert lines[-1]["event"] == "config_error" and "no sealing key" in lines[-1]["msg"]
    assert "Traceback" not in p.stderr


def test_a_bad_state_setting_fails_the_start_in_json(tmp_path):
    p = run(
        ["-m", "vkteams_mcp.app"],
        env_for(tmp_path, VKTEAMS_MCP_STATE_URL="redis://h:6379/0", VKTEAMS_MCP_STATE_SENTINELS="s:26379"),
    )
    assert p.returncode == 2
    assert every_line_is_json(p.stderr)[-1]["event"] == "config_error"


def test_an_exception_nobody_caught_is_a_json_line_without_its_message(tmp_path):
    code = "from vkteams_mcp import observability as o\no.configure_logging()\nraise RuntimeError('SECRET-WORDS')\n"
    p = run(["-c", code], env_for(tmp_path))
    assert p.returncode == 1
    lines = every_line_is_json(p.stderr)
    assert lines[-1]["event"] == "uncaught_exception" and "RuntimeError" in lines[-1]["exc"]
    assert "SECRET-WORDS" not in p.stderr


def test_a_thread_that_dies_is_a_json_line_too(tmp_path):
    code = (
        "import threading\nfrom vkteams_mcp import observability as o\no.configure_logging()\n"
        "t = threading.Thread(target=lambda: 1/0)\nt.start()\nt.join()\n"
    )
    p = run(["-c", code], env_for(tmp_path))
    assert every_line_is_json(p.stderr)[-1]["event"] == "uncaught_thread_exception"


def test_the_init_container_stages_files_and_every_line_is_json(tmp_path):
    src = tmp_path / "secret"
    src.mkdir()
    (src / "key").write_text(KEY + "\n")
    (src / "password").write_text("hunter2-not-for-logs\n")
    dst = tmp_path / "run"
    dst.mkdir()
    p = run(
        ["-m", "vkteams_mcp.stage", f"{src}/key:{dst}/key", f"{src}/password:{dst}/state-password"], env_for(tmp_path)
    )
    assert p.returncode == 0, p.stderr
    lines = every_line_is_json(p.stderr + p.stdout)
    assert [x["event"] for x in lines].count("staged") == 2
    for name, want in (("key", KEY + "\n"), ("state-password", "hunter2-not-for-logs\n")):
        f = dst / name
        assert f.read_text() == want and (f.stat().st_mode & 0o777) == 0o400
    assert "hunter2" not in p.stderr + p.stdout and KEY not in p.stderr + p.stdout
    # a second run replaces the files (the 0400 file is not opened for writing: it is renamed over)
    assert run(["-m", "vkteams_mcp.stage", f"{src}/key:{dst}/key"], env_for(tmp_path)).returncode == 0
    assert not list(dst.glob("*.staging"))


@pytest.mark.parametrize(
    "setup",
    ["missing", "empty", "directory", "usage"],
)
def test_staging_that_fails_says_so_in_json_and_exits_2(tmp_path, setup):
    src = tmp_path / "secret"
    src.mkdir()
    dst = tmp_path / "run"
    dst.mkdir()
    args = [f"{src}/key:{dst}/key"]
    if setup == "empty":
        (src / "key").write_text("\n")
    elif setup == "directory":
        (src / "key").mkdir()
    elif setup == "usage":
        args = []
    p = run(["-m", "vkteams_mcp.stage", *args], env_for(tmp_path))
    assert p.returncode == 2
    assert every_line_is_json(p.stderr)[-1]["event"] in ("stage_failed", "stage_usage")
    assert not (dst / "key").exists()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_running_server_writes_only_json_from_start_to_stop(tmp_path):
    port, mport = free_port(), free_port()
    env = env_for(
        tmp_path,
        PORT=str(port),
        VKTEAMS_MCP_METRICS_PORT=str(mport),
        VKTEAMS_MCP_PUBLIC_URL=f"http://127.0.0.1:{port}",
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "vkteams_mcp.app"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=1)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the server never answered")
        for path in ("/readyz", "/nothing-here", "/signin?request=bad", "/files/d/forged"):
            try:
                urllib.request.urlopen(f"{base}{path}", timeout=5)  # noqa: S310
            except urllib.error.HTTPError:
                pass
        # and the two kinds of request the SDK logs about: a bad token and a malformed body
        for req in (
            urllib.request.Request(f"{base}/mcp", data=b"{not json", headers={"Authorization": "Bearer nope"}),
            urllib.request.Request(f"{base}/token", data=b"grant_type=x", method="POST"),
        ):
            try:
                urllib.request.urlopen(req, timeout=5)  # noqa: S310
            except urllib.error.HTTPError:
                pass
        scraped = urllib.request.urlopen(f"http://127.0.0.1:{mport}/metrics", timeout=5).read().decode()  # noqa: S310
        assert "mcp_state_store_up" in scraped
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    lines = every_line_is_json(out)
    events = [x["event"] for x in lines]
    assert "start" in events and "state_memory" in events
    # uvicorn finishes its shutdown, then re-raises the signal it was stopped by, so a clean stop reads -15
    assert proc.returncode in (0, -signal.SIGTERM)


def test_an_optional_missing_source_removes_a_stale_destination(tmp_path):
    """A retry of the init container after the Secret lost an entry must not keep using what an earlier try staged."""
    src = tmp_path / "secret"
    src.mkdir()
    (src / "key").write_text(KEY)
    (src / "password").write_text("old-password")
    dst = tmp_path / "run"
    dst.mkdir()
    args = [f"{src}/key:{dst}/key", "--optional", f"{src}/password:{dst}/state-password"]
    assert run(["-m", "vkteams_mcp.stage", *args], env_for(tmp_path)).returncode == 0
    assert (dst / "state-password").exists()
    (src / "password").unlink()
    p = run(["-m", "vkteams_mcp.stage", *args], env_for(tmp_path))
    assert p.returncode == 0 and not (dst / "state-password").exists()
    assert "stage_skipped" in [x["event"] for x in every_line_is_json(p.stderr)]
