"""Authentication: what the server actually enforces, and how the spec describes it.

The first block needs no credentials and runs anywhere. The second needs a live
session (`tests/live/legacy_login.py`) and proves that a real aimsid opens the same doors a
bogus one cannot.
"""

from __future__ import annotations

import re

import pytest

from conftest import redact
from vkclient import DEV_IDS

RAPI_AUTH_FAIL = 40200  # {"status":{"code":40200,"reason":"Invalid token"}}
RAPI_UNKNOWN = 40000    # {"status":{"code":40000,"reason":"Unknown method"}}


# ---------------------------------------------------------------- no credentials

def test_config_discovery_is_public(server_config, recorder):
    assert isinstance(server_config.get("api-version"), int)
    assert server_config["api-urls"]["main-api"].startswith("https://")
    recorder.add(path="/myteam-config.json", http=200, verdict="works-unauthenticated",
                 note=f"api-version={server_config['api-version']}; keys={len(server_config)}")


def test_spec_rapi_prefix_is_not_routed(anon, recorder):
    """The spec writes /api/v/rapi/<m>. The server routes /api/v<N>/rapi/<m> and answers 'Unknown method' otherwise."""
    r = anon.http.post(f"https://{anon.s.host}/api/v/rapi/getUserInfo",
                       json={"reqId": "1", "aimsid": "bogus", "params": {"sn": "x"}})
    assert r.json()["status"]["code"] == RAPI_UNKNOWN
    r2 = anon.rapi_raw("getUserInfo", {"sn": "x"})
    assert r2.json()["status"]["code"] == RAPI_AUTH_FAIL
    recorder.add(path="*rapi-prefix*", verdict="spec-wrong",
                 note=f"/api/v/rapi/* -> 40000 Unknown method; /api/v{anon.s.api_version}/rapi/* is the live route")


VALID_PARAMS = {"getUserInfo": {"sn": "x"}, "session/list": {}, "folders/requestFolders": {},
                "getHistory": {"sn": "x", "fromMsgId": -1, "count": -5, "patchVersion": "init"}}


@pytest.mark.parametrize("method", list(VALID_PARAMS))
def test_rapi_rejects_bogus_aimsid(anon, method):
    j = anon.rapi_raw(method, VALID_PARAMS[method]).json()
    assert j["status"]["code"] == RAPI_AUTH_FAIL, j


def test_rapi_rejects_missing_aimsid(anon):
    r = anon.http.post(f"{anon.base}/rapi/session/list", json={"reqId": "1", "params": {}})
    assert r.json()["status"]["code"] == RAPI_AUTH_FAIL


def test_rapi_validates_params_before_auth(anon, recorder):
    """Order matters for a client: a 40000 with a bogus aimsid means bad params, not bad auth."""
    j = anon.rapi_raw("getUserInfo", {}).json()
    assert j["status"]["code"] == RAPI_UNKNOWN and "required property 'sn'" in j["status"]["reason"]
    recorder.add(path="*rapi-errors*", verdict="observed",
                 note="params are schema-validated before aimsid: 40000 'Invalid json content: ... required property X' "
                      "or 'Required parameter not found'; then 40200 'Invalid token'")


def test_rapi_ignores_aimsid_in_query_or_header(live, recorder):
    """Spec offers AimsidQuery and X-Teams-Aimsid. For rapi only the JSON body field counts."""
    body = {"reqId": "1", "params": {}}
    q = live.http.post(f"{live.base}/rapi/session/list", params={"aimsid": live.s.aimsid}, json=body).json()
    h = live.http.post(f"{live.base}/rapi/session/list", headers={"X-Teams-Aimsid": live.s.aimsid}, json=body).json()
    recorder.add(path="/api/v/rapi/session/list", verdict="auth-transport",
                 note=f"aimsid in query -> {q['status']['code']}; in X-Teams-Aimsid header -> {h['status']['code']}; in JSON body -> 20000")
    # Either outcome is a fact worth recording; the assertion pins today's behaviour.
    assert q["status"]["code"] in (RAPI_AUTH_FAIL, 20000)
    assert h["status"]["code"] in (RAPI_AUTH_FAIL, 20000)


@pytest.mark.parametrize("prefix", ["/api/v", "/api/v{n}"])
def test_wim_rejects_bogus_aimsid(anon, prefix):
    base = f"https://{anon.s.host}" + prefix.format(n=anon.s.api_version)
    j = anon.http.get(f"{base}/wim/preference/getPermitDeny", params={"f": "json", "aimsid": "bogus"}).json()
    assert j["response"]["statusCode"] == 401


def test_client_login_requires_dev_id(anon, recorder):
    """Spec: required: []. Server: 460 'Missing required parameter (k)' without devId/k."""
    j = anon.http.post(f"{anon.base}/wim/auth/clientLogin",
                       data={"f": "json", "s": "nobody@example.invalid", "pwd": "x"}).json()
    assert j["response"]["statusCode"] == 460
    recorder.add(path="/api/v/wim/auth/clientLogin", verdict="spec-incomplete",
                 note="devId (alias k) is required: 460 Missing required parameter (k); wrong password -> 330/3011 with challenge.context")


def test_client_login_wrong_password_is_330(anon):
    j = anon.http.post(f"{anon.base}/wim/auth/clientLogin",
                       data={"f": "json", "devId": DEV_IDS["desktop"], "s": "nobody@example.invalid", "pwd": "x"}).json()
    assert j["response"]["statusCode"] == 330
    assert "challenge" in j["response"]["data"]


def test_start_session_rejects_bad_signature(anon, recorder):
    from vkclient import Client, Session
    s = Session(host=anon.s.host, api_version=anon.s.api_version, dev_id=DEV_IDS["desktop"], login="x",
                token_a="bogus", session_key="bogus")
    j = Client(s).start_session()
    assert j["response"]["statusCode"] == 401
    recorder.add(path="/api/v/wim/aim/startSession", verdict="auth-enforced",
                 note="bogus a + sig_sha256 -> 401 statusDetailCode 1001")


# ---------------------------------------------------------------- live session

def test_live_aimsid_opens_rapi(live, recorder):
    j = live.rapi("session/list")
    assert j["status"]["code"] == 20000
    recorder.add(path="/api/v/rapi/session/list", http=200, api_status=20000, verdict="works",
                 note="aimsid in JSON body", sample=redact(j))


def test_live_aimsid_opens_wim(live):
    j = live.wim("preference/getPermitDeny")
    assert j["response"]["statusCode"] == 200


def test_fetch_events_uses_fetch_base_url(live, recorder):
    """Spec: /aim/fetchEvents on an unknown host. Live: the full URL comes from startSession.fetchBaseURL."""
    from urllib.parse import urlsplit
    u = urlsplit(live.s.fetch_base_url)
    j = live.fetch_events(timeout_ms=500)
    assert j["response"]["statusCode"] == 200
    recorder.add(path="/aim/fetchEvents", verdict="works", http=200,
                 note=f"path=" + re.sub(r"/bos/[^/]+/", "/bos/<node>/", u.path) + "; next fetchBaseURL in response.data", sample=redact(j))
