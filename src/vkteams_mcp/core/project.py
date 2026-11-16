"""Shared pieces of projections: compact dicts and bounded pages."""

from __future__ import annotations

from typing import Any

from vkteams_mcp.core.errors import invalid

DEFAULT_LIMIT = 10
MAX_LIMIT = 25


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values and false booleans: absent means no."""
    return {k: v for k, v in d.items() if not (v is None or v is False or (isinstance(v, (str, list, dict)) and not v))}


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> tuple[int, dict[str, Any]]:
    if limit is None:
        return default, {}
    if limit < 1:
        raise invalid(f"limit is 1 to {cap}.")
    if limit > cap:
        return cap, {"limit_clamped_to": cap}
    return limit, {}


def page_result(items: list[dict[str, Any]], cursor: str | None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"items": items, "count": len(items), "has_more": bool(cursor)}
    if cursor:
        out["cursor"] = cursor
    out.update({k: v for k, v in extra.items() if v is not None})
    if cursor and out.get("limit_clamped_to"):
        out["limit_note"] = f"At most {out['limit_clamped_to']} come at a time; cursor gives the next page."
    return out


def cap_text(text: str, limit: int = 4000) -> dict[str, Any]:
    """Text with a cap that says it was cut."""
    if len(text) <= limit:
        return {"text": text}
    return {"text": text[:limit], "truncated": True, "original_chars": len(text)}
