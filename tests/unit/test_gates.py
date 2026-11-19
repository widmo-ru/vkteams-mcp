"""Gates that hold the rules no single test can: cost, surface, purity, logs, the refusal map, and the
size of the suite itself. Each gate fails on an empty input rather than passing."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

import pytest
from conftest import GROUP, ME, call_tool, ok

ROOT = Path(__file__).resolve().parents[2]

# sha256[:16] of words that name one installation, one person or the company behind this one. The words
# themselves never enter the repository; only their hashes do (manifesto rule 34).
FORBIDDEN = {
    "fd702a2fd7c521dc",
    "615672312a1e106d",
    "ac7df8fb708a7728",
    "1b745fa71569e019",
    "2db16afa1e43dfce",
    "cf72293825af3299",
    "922af9e6420dcf27",
    "9f22916cf7d4179d",
    "16ba5e234e28ecd4",
    "919a4872f3389e72",
    "a05c71105132365d",
}


def tracked() -> list[Path]:
    # -z: names with spaces or non-ASCII letters arrive whole, not split or quoted
    out = (
        subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split("\0")
    )
    return [ROOT / f for f in out if f and (ROOT / f).is_file()]


def _h(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- purity


def impure(files: list[Path], forbidden: set[str]) -> list[str]:
    hits = []
    for f in files:
        text = f.read_text(errors="ignore").lower()
        words = re.findall(r"[\w.@-]+", text)
        tokens = set(words) | {t for w in words for t in re.split(r"[.@-]", w)}
        tokens |= {f"{a} {b}" for a, b in zip(words, words[1:], strict=False)}
        if any(_h(t) in forbidden for t in tokens):
            hits.append(f.name)
    return hits


def test_the_repository_names_no_installation_person_or_company():
    files = [f for f in tracked() if f.suffix in (".py", ".md", ".toml", ".json", ".yml", ".yaml", ".html", ".txt", "")]
    assert len(files) > 30, "the purity gate scanned almost nothing: it is broken"
    assert impure(files, FORBIDDEN) == []


def test_the_purity_gate_catches_a_forbidden_word(tmp_path):
    # the gate's own negative test, on a synthetic word: a host, a bigram and an address must all be caught
    bad = {_h("zqxbadcorp"), _h("zqx corp")}
    for i, text in enumerate(["see https://u.zqxbadcorp.example", "ZQX Corp made it", "x@zqxbadcorp.example"]):
        f = tmp_path / f"f{i}.md"
        f.write_text(text)
        assert impure([f], bad) == [f.name], text
    clean = tmp_path / "clean.md"
    clean.write_text("nothing here")
    assert impure([clean], bad) == []


def test_live_findings_do_not_leak_through_examples():
    for f in (ROOT / "docs").rglob("*.md"):
        assert not re.search(r"\b\d{3,}@chat\.agent\b", f.read_text()), f  # real chat ids look like that


# ---------------------------------------------------------------- cost and surface


async def test_the_catalogue_fits_the_budget():
    from vkteams_mcp.budget import LIMIT_TOOL, measure, problems
    from vkteams_mcp.registry import AREAS, parse_areas

    default = await measure(parse_areas(None))
    assert len(default["tools"]) >= 12 and default["total"] > 1000  # a measure of nothing passes nothing
    assert problems(default, check_total=True) == []
    everything = await measure(frozenset(AREAS))
    assert problems(everything, check_total=False) == []
    assert max(everything["tools"].values()) <= LIMIT_TOOL


async def test_the_surface_lock_matches_the_registry():
    from vkteams_mcp.surface import LOCK, drift, render, surface

    now = await surface()
    assert len(now["tools"]) >= 14
    assert drift(json.loads(LOCK.read_text()), now) == []
    assert LOCK.read_text() == render(now)


def test_drift_names_each_kind_of_change():
    from vkteams_mcp.surface import drift

    a = {"tools": {"x": {"area": "a", "annotations": {}, "gates": {}, "input_schema_sha256": "1"}}}
    b = {
        "tools": {
            "x": {"area": "b", "annotations": {}, "gates": {}, "input_schema_sha256": "2"},
            "y": {"area": "a", "annotations": {}, "gates": {}, "input_schema_sha256": "3"},
        }
    }
    assert drift(a, b) == ["x: area changed", "x: input_schema_sha256 changed", "y: not in the lock"]


# ---------------------------------------------------------------- logs


async def test_a_full_message_flow_logs_no_content(server, chats, caplog):
    caplog.set_level(logging.DEBUG)
    chats.rapi(
        "getHistory",
        ok(
            {
                "messages": [
                    {"msgId": 1, "time": 1, "text": "secret budget figures", "chat": {"sender": "ann@example.com"}}
                ]
            }
        ),
    )
    chats.rapi("message/send", ok({"msgId": 2}))
    chats.rapi("search", ok({"data": [{"sn": "ann@example.com", "anketa": {"friendly": "Ann Smith"}}]}))
    r = await call_tool(server, "list_messages", {"chat": "Project Alpha"})
    assert not r.is_error  # the content-bearing paths must actually run, not refuse early
    r = await call_tool(server, "save_draft", {"chat": GROUP, "text": "confidential reply", "mention": ["Ann Smith"]})
    assert not r.is_error
    r = await call_tool(
        server, "send_draft", {"chat": GROUP, "draft": r.structured_content["draft"], "user_confirmed": True}
    )
    assert not r.is_error
    await call_tool(server, "get_chat", {"chat": "Project"})  # an ambiguous name: candidates in the result only
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "nothing was logged at all: the check would pass vacuously"
    for secret in (
        "secret budget",
        "confidential",
        "Ann Smith",
        "ann@example.com",
        ME,
        "Project Alpha",
        "aimsid-1",
        "token-a",
        "session-key",
    ):
        assert secret not in logged, secret


# ---------------------------------------------------------------- the refusal map


def test_every_must_refuse_row_names_a_test_that_exists():
    text = (ROOT / "docs" / "requirements.md").read_text()
    section = text.split("## Must refuse", 1)
    assert len(section) == 2, "no Must refuse section: the map parser is broken"
    rows = re.findall(r"^\|([^|]+)\|\s*(tests/[^|\s]+)\s*\|", section[1], re.M)
    assert len(rows) >= 15, "too few rows read: the map parser is broken"
    missing = []
    for _, ref in rows:
        path, name = ref.split("::")
        src = (ROOT / path).read_text() if (ROOT / path).exists() else ""
        if not re.search(rf"^(async )?def {re.escape(name)}\(", src, re.M):
            missing.append(ref)
    assert missing == []
    assert len({r for _, r in rows}) == len(rows), "a test is named by two rows"


# ---------------------------------------------------------------- the suite itself


def test_the_suite_is_not_empty(request):
    if [a for a in request.config.args if a.rstrip("/") not in ("tests/unit", str(ROOT / "tests" / "unit"))]:
        pytest.skip("a partial run: the suite size is checked on full runs")
    defined = sum(
        len(re.findall(r"^(?:async )?def test_", f.read_text(), re.M))
        for f in (ROOT / "tests" / "unit").glob("test_*.py")
    )
    assert defined >= 150, "the suite on disk is implausibly small"
    collected = {item.originalname for item in request.session.items}
    assert len(collected) >= defined * 0.95, "tests on disk were not collected"


def test_the_process_listens_on_the_configured_port_without_access_logs(monkeypatch, restore_logging, tmp_path):
    import uvicorn

    from vkteams_mcp import app as app_module

    seen = []

    async def record(self, sockets=None):
        seen.append(self.config)

    monkeypatch.setattr(uvicorn.Server, "serve", record)
    monkeypatch.setenv("VKTEAMS_MCP_PUBLIC_URL", "https://mcp.example.org")
    key = tmp_path / "key"
    key.write_text("y" * 40)
    key.chmod(0o400)
    monkeypatch.setenv("VKTEAMS_MCP_KEY_FILE", str(key))
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("VKTEAMS_MCP_METRICS_PORT", "9123")
    app_module.main()
    public, metrics = sorted(seen, key=lambda c: c.port == 9123)
    assert public.port == 8123 and public.access_log is False and public.log_config is None
    assert metrics.port == 9123 and metrics.access_log is False and metrics.lifespan == "off"
