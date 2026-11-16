"""What the tool catalogue costs the model, measured with a real tokenizer on the exact tools/list JSON.

python -m vkteams_mcp.budget            # table for the default areas
python -m vkteams_mcp.budget --check    # exit 1 over a limit (default areas: total, area, tool)
python -m vkteams_mcp.budget --all      # every area on (area and tool limits only)
"""

from __future__ import annotations

import asyncio
import json
import sys

import tiktoken

from vkteams_mcp.registry import AREAS, all_tools, parse_areas

LIMIT_TOTAL = 10_000
LIMIT_AREA = 3_000
LIMIT_TOOL = 600


async def measure(areas: frozenset[str]) -> dict:
    from vkteams_mcp.server import VKServer

    enc = tiktoken.get_encoding("o200k_base")
    tools = await VKServer(areas).list_tools()
    dumped = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    specs = all_tools()
    per_tool = {d["name"]: len(enc.encode(json.dumps(d, separators=(",", ":"), ensure_ascii=False))) for d in dumped}
    per_area: dict[str, int] = {}
    for name, n in per_tool.items():
        per_area[specs[name].area] = per_area.get(specs[name].area, 0) + n
    total = len(enc.encode(json.dumps(dumped, separators=(",", ":"), ensure_ascii=False)))
    return {"total": total, "areas": per_area, "tools": per_tool}


def problems(m: dict, check_total: bool) -> list[str]:
    out = []
    if check_total and m["total"] > LIMIT_TOTAL:
        out.append(f"catalogue {m['total']} > {LIMIT_TOTAL}")
    out += [f"area {a} {n} > {LIMIT_AREA}" for a, n in m["areas"].items() if n > LIMIT_AREA]
    out += [f"tool {t} {n} > {LIMIT_TOOL}" for t, n in m["tools"].items() if n > LIMIT_TOOL]
    return out


def main(argv: list[str]) -> int:
    every = "--all" in argv
    areas = frozenset(AREAS) if every else parse_areas(None)
    m = asyncio.run(measure(areas))
    for name, n in sorted(m["tools"].items(), key=lambda x: -x[1]):
        print(f"{n:6}  {name}")
    print(f"{m['total']:6}  total ({len(m['tools'])} tools, {'all areas' if every else 'default areas'})")
    bad = problems(m, check_total=not every)
    if "--check" in argv and bad:
        print("OVER BUDGET: " + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
