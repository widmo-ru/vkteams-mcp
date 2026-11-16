"""Shared fixtures: an OpenAPI description to check answers against, a live session, and a recorder.

Every live call is recorded to results/live-<timestamp>.json. The suites that check answers against an
OpenAPI description of the client API need it in VKTEAMS_SPEC; without it they are skipped.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from vkclient import Client, Session, discover

SPEC_PATH = Path(os.environ["VKTEAMS_SPEC"]).expanduser() if os.environ.get("VKTEAMS_SPEC") else None
def _default_host() -> str:
    s = Session.load()
    return s.host if s else ""


HOST = os.environ.get("VKTEAMS_HOST") or _default_host()
RESULTS = Path(__file__).resolve().parent.parent / "results"


@pytest.fixture(scope="session")
def spec() -> dict:
    if SPEC_PATH is None or not SPEC_PATH.is_file():
        pytest.skip("set VKTEAMS_SPEC to an OpenAPI description of the VK Teams client API")
    return yaml.safe_load(SPEC_PATH.read_text())


class SpecValidator:
    def __init__(self, spec: dict):
        self.spec = spec
        res = Resource.from_contents(spec, default_specification=DRAFT202012)
        self.registry = Registry().with_resource("urn:spec", res)

    def _validator(self, schema: dict) -> Draft202012Validator:
        return Draft202012Validator({"$ref": "urn:spec" + schema["$ref"]} if "$ref" in schema else
                                    {**schema, "$id": "urn:inline"}, registry=self.registry)

    def operation(self, path: str) -> tuple[str, dict]:
        item = self.spec["paths"][path]
        for m in ("post", "get", "put", "delete"):
            if m in item:
                return m, item[m]
        raise KeyError(path)

    def errors(self, schema: dict, instance) -> list[str]:
        v = self._validator(schema)
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}" for e in v.iter_errors(instance)]

    def check_response(self, path: str, body) -> dict:
        _, op = self.operation(path)
        r200 = op["responses"].get("200", {})
        schema = r200.get("content", {}).get("application/json", {}).get("schema")
        out = {"envelope_errors": self.errors(schema, body) if schema else ["no 200 schema in spec"]}
        ref = r200.get("x-results-payload-schema-ref")
        if ref and isinstance(body, dict) and "results" in body:
            out["results_schema"] = ref
            out["results_errors"] = self.errors({"$ref": ref}, body["results"])
        return out


@pytest.fixture(scope="session")
def validator(spec) -> SpecValidator:
    return SpecValidator(spec)


class Recorder:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, **row):
        row.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.rows.append(row)

    def dump(self) -> Path | None:
        if not self.rows:
            return None
        RESULTS.mkdir(exist_ok=True)
        p = RESULTS / f"live-{time.strftime('%Y%m%d-%H%M%S')}.json"
        p.write_text(json.dumps({"host": HOST, "rows": self.rows}, indent=1, ensure_ascii=False))
        return p


@pytest.fixture(scope="session")
def recorder():
    r = Recorder()
    yield r
    p = r.dump()
    if p:
        print(f"\nlive observations → {p}")


@pytest.fixture(scope="session")
def server_config() -> dict:
    if not HOST:
        pytest.skip("set VKTEAMS_HOST or log in first (python tests/live/legacy_login.py --host ...)")
    return discover(HOST)


@pytest.fixture(scope="session")
def anon(server_config) -> Client:
    """A client with no credentials (bogus aimsid) — for auth-enforcement tests."""
    s = Session(host=HOST, api_version=int(server_config["api-version"]), dev_id="", login="", aimsid="bogus")
    return Client(s)


@pytest.fixture(scope="session")
def live() -> Client:
    s = Session.load()
    if not s or not s.aimsid:
        pytest.skip("no live session: run `uv run python tests/live/legacy_login.py --host ...` first")
    return Client(s)


# Never let a real aimsid/token end up in a recorded body.
SECRET_KEYS = {"aimsid", "a", "sessionSecret", "token", "fetchBaseURL", "o2token", "authToken"}


def placeholders(x, ctx: dict):
    """Replace real ids with role names so a params example can go into the spec."""
    names = {str(v): f"<{k}>" for k, v in (ctx or {}).items() if v not in (None, "")}
    if isinstance(x, dict):
        return {k: placeholders(v, ctx) for k, v in x.items()}
    if isinstance(x, list):
        return [placeholders(v, ctx) for v in x]
    return names.get(str(x), x)


def redact(x):
    if isinstance(x, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS and isinstance(v, str) else redact(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [redact(v) for v in x[:3]]
    if isinstance(x, str) and len(x) > 300:
        return x[:300] + "…"
    return x
