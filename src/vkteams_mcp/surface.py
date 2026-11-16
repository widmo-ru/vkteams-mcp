"""The tool surface lock: area, annotations and input-schema hash of every tool with all areas on.

python -m vkteams_mcp.surface > tool-surface.lock.json     # regenerate
python -m vkteams_mcp.surface --check                        # exit 1 on drift
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from vkteams_mcp.registry import AREAS, all_tools

LOCK = Path(__file__).resolve().parents[2] / "tool-surface.lock.json"


async def surface() -> dict:
    from vkteams_mcp.server import VKServer

    specs = all_tools()
    out = {}
    for t in await VKServer(frozenset(AREAS)).list_tools():
        schema = json.dumps(t.input_schema, sort_keys=True, separators=(",", ":"))
        out[t.name] = {
            "area": specs[t.name].area,
            "annotations": t.annotations.model_dump(by_alias=True, exclude_none=True) if t.annotations else {},
            "gates": specs[t.name].gates,
            "input_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
        }
    return {"tools": dict(sorted(out.items()))}


def render(s: dict) -> str:
    return json.dumps(s, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def drift(locked: dict, now: dict) -> list[str]:
    out = []
    for name in sorted(set(locked["tools"]) | set(now["tools"])):
        a, b = locked["tools"].get(name), now["tools"].get(name)
        if a is None:
            out.append(f"{name}: not in the lock")
        elif b is None:
            out.append(f"{name}: in the lock but no longer registered")
        else:
            out += [
                f"{name}: {k} changed" for k in ("area", "annotations", "gates", "input_schema_sha256") if a[k] != b[k]
            ]
    return out


def main(argv: list[str]) -> int:
    now = asyncio.run(surface())
    if "--check" in argv:
        problems = drift(json.loads(LOCK.read_text()), now)
        for p in problems:
            print(p)
        if problems:
            print("REGENERATE: python -m vkteams_mcp.surface > tool-surface.lock.json")
        return 1 if problems else 0
    sys.stdout.write(render(now))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
