"""Logs and metrics, the same shape in every server of this family.

Logs: every line the process writes, from this code, from uvicorn, from the MCP SDK, from an uncaught
exception, is one JSON object (`json.dumps`, so a quote, a newline or a traceback cannot split a record):

    {"ts": "2026-11-19T12:00:00.123Z", "level": "INFO", "logger": "vkteams_mcp", "event": "start", "msg": "…"}

`event` is a stable key to filter by; `msg` is for people. Only the fields in `FIELDS` are carried beside
them, and a traceback is its frames and exception types, never what the exceptions said. A record that
looks like content (an address, a token) is dropped and counted (manifesto rule 31).

Metrics: Prometheus, served on a port of their own (`serve_metrics`), never on the public one. Every
metric has the constant label `server`, so one dashboard and one autoscaling rule fit every server of the
family. Labels are a closed set of words; a person, a login, an address or a tool argument is never one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from vkteams_mcp import __version__

SERVER = "vkteams"

# ---------------------------------------------------------------- metrics

_S = ["server"]
HTTP_IN_FLIGHT = Gauge("mcp_http_requests_in_flight", "Requests being served right now", [*_S, "route_group"])
HTTP_REQUESTS = Counter(
    "mcp_http_requests_total", "HTTP requests served", [*_S, "route_group", "method", "status_class"]
)
HTTP_SECONDS = Histogram(
    "mcp_http_request_duration_seconds",
    "HTTP request duration, until the last byte of the response",
    [*_S, "route_group"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
TOOL_CALLS = Counter("mcp_tool_calls_total", "Tool calls", [*_S, "tool", "result"])
TOOL_SECONDS = Histogram(
    "mcp_tool_duration_seconds", "Tool call duration", [*_S, "tool"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
)
UPSTREAM_REQUESTS = Counter("mcp_upstream_requests_total", "Requests to VK Teams", [*_S, "status_class"])
UPSTREAM_SECONDS = Histogram(
    "mcp_upstream_duration_seconds",
    "VK Teams request duration",
    _S,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
SIGNIN = Counter("mcp_signin_total", "Sign-in steps by outcome", [*_S, "result"])
AUTH_REFUSALS = Counter("mcp_auth_refusals_total", "Refused tokens, clients and codes", [*_S, "reason"])
GUARD_REFUSALS = Counter("mcp_guard_refusals_total", "Refusals by the guard in front of sending", [*_S, "reason"])
LINK_TRANSFERS = Counter("mcp_link_transfers_total", "Signed-link transfers", [*_S, "op", "result"])
STATE_UP = Gauge("mcp_state_store_up", "1 while the shared state store answers, 0 when it does not", _S)
STATE_ERRORS = Counter("mcp_state_store_errors_total", "State store operations that failed", [*_S, "op"])
STATE_SECONDS = Histogram(
    "mcp_state_store_duration_seconds",
    "State store operation duration",
    [*_S, "op"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 3),
)
BUILD_INFO = Gauge("mcp_build_info", "The version this process runs; always 1", [*_S, "version"])
LOG_DROPPED = Counter("mcp_log_records_dropped_total", "Log records not written", _S)

for _g in ("mcp", "oauth", "signin", "links", "health", "other"):  # a gauge that never moved still reports 0
    HTTP_IN_FLIGHT.labels(SERVER, _g)
STATE_UP.labels(SERVER).set(1)
LOG_DROPPED.labels(SERVER)
BUILD_INFO.labels(SERVER, __version__).set(1)


def inc(counter: Counter, *labels: str) -> None:
    counter.labels(SERVER, *labels).inc()


def scrape() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------- logs

FIELDS = frozenset(
    {"event", "tool", "key", "reason", "route_group", "method", "status", "op", "result", "mode", "ms", "version"}
)
# What content looks like: an address, a bearer token, one of our sealed blobs, a session id.
_CONTENT = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|[A-Za-z0-9_-]{80,}|aimsid[=:]\s*[^\s&]{6,}"
)
MAX_MSG = 2000


def _ts(created: float) -> str:
    return datetime.fromtimestamp(created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bare_traceback(exc_info: Any) -> str:
    """Where it failed, without what it said: the frames and the exception types, never an exception's message
    or a local value, which can hold a name or an address that no pattern recognises."""
    part: traceback.TracebackException | None = traceback.TracebackException(*exc_info, capture_locals=False)
    lines: list[str] = []
    seen = 0
    while part is not None and seen < 5:
        lines.append("Traceback (most recent call last), messages withheld:")
        lines += [f'  File "{fr.filename}", line {fr.lineno}, in {fr.name}' for fr in part.stack]
        lines.append(getattr(part, "exc_type_str", None) or getattr(part.exc_type, "__qualname__", "Exception"))
        part = part.__cause__ or part.__context__
        seen += 1
    return "\n".join(lines)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": _ts(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.name,
            "msg": record.getMessage()[:MAX_MSG],
        }
        for k in FIELDS - {"event"}:
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info and record.exc_info[0] is not None:
            out["exc"] = bare_traceback(record.exc_info)
        elif record.exc_text:
            out["exc"] = "(traceback withheld)"
        return json.dumps(out, ensure_ascii=False, default=str)


class ContentFilter(logging.Filter):
    """Drops a record that looks like content, and counts it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot render is not written
            inc(LOG_DROPPED)
            return False
        if _CONTENT.search(msg):
            inc(LOG_DROPPED)
            return False
        return True


class _Handler(logging.StreamHandler):  # type: ignore[type-arg]
    def handleError(self, record: logging.LogRecord) -> None:  # a failed write is counted, never printed as text
        inc(LOG_DROPPED)


def _emit_raw(level: str, event: str, msg: str, exc_info: Any = None) -> None:
    rec = {
        "ts": _ts(datetime.now(UTC).timestamp()),
        "level": level,
        "logger": "vkteams_mcp",
        "event": event,
        "msg": msg[:MAX_MSG],
    }
    if exc_info and exc_info[0] is not None:
        rec["exc"] = bare_traceback(exc_info)
    try:
        sys.stderr.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001, S110 - nowhere left to report it
        pass


def install_exception_hooks() -> None:
    """An exception nobody caught is a JSON line too, not a traceback in plain text."""

    def hook(kind, value, tb):
        _emit_raw("CRITICAL", "uncaught_exception", "an exception ended the process", (kind, value, tb))

    def thread_hook(args):
        _emit_raw(
            "ERROR",
            "uncaught_thread_exception",
            "an exception ended a thread",
            (args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = hook
    threading.excepthook = thread_hook


def asyncio_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    _emit_raw(
        "ERROR",
        "asyncio_exception",
        str(context.get("message", "an asyncio task failed"))[:200],
        (type(exc), exc, exc.__traceback__) if exc else None,
    )


LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def log_level(env: dict[str, str] | None = None) -> str:
    """LOG_LEVEL (unprefixed, the platform's name for it): one of LEVELS, INFO when unset or not one of them."""
    raw = ((os.environ if env is None else env).get("LOG_LEVEL") or "INFO").strip().upper()
    return raw if raw in LEVELS else "INFO"


def configure_logging(level: int | None = None) -> None:
    """One root handler writing JSON; uvicorn is started with `log_config=None`, so its loggers keep none
    of their own and propagate here. Warnings from Python itself go through it too. The level is LOG_LEVEL."""
    level = getattr(logging, log_level()) if level is None else level
    handler = _Handler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContentFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.captureWarnings(True)
    # httpx logs every URL at INFO, and wim URLs carry the aimsid; uvicorn's access log carries query strings
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_exception_hooks()
    asked = (os.environ.get("LOG_LEVEL") or "").strip()
    if asked and asked.upper() not in LEVELS:
        event(
            logging.getLogger("vkteams_mcp"),
            logging.WARNING,
            "log_level",
            f"LOG_LEVEL is not one of {', '.join(LEVELS)}: using INFO",
        )


def event(log: logging.Logger, level: int, name: str, msg: str, **fields: Any) -> None:
    """A record with a stable event key and only allowlisted fields."""
    bad = set(fields) - FIELDS
    if bad:
        raise KeyError(f"not an allowed log field: {sorted(bad)}")
    log.log(level, msg, extra={"event": name, **fields})
