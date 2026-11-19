"""Sign-in by the code VK Teams e-mails: the exact requests, the trap that burns a code, and the
limits that keep the public page from being used to send mail."""

from __future__ import annotations

import pytest
from conftest import BASE, HOST, ME, wim_error, wim_ok

from vkteams_mcp.api.install import Installation
from vkteams_mcp.api.login import CodeLimiter, finish_sign_in, request_code
from vkteams_mcp.core.errors import VKError
from vkteams_mcp.core.state import MemoryStore, derive_key

INST = Installation(host=HOST, binary_host=HOST, api_version=139)
LOGIN = f"{BASE}/wim/auth/clientLogin"


def login_ok(token: str = "tok") -> dict:
    return wim_ok(
        {"hostTime": 1_800_000_000, "token": {"a": token, "expiresIn": 0}, "sessionSecret": "secret", "loginId": ME}
    )


START = wim_ok(
    {
        "aimsid": "aimsid-x",
        "fetchBaseURL": f"https://{HOST}{BASE}/bos/n/aim/fetchEvents?aimsid=aimsid-x",
        "myInfo": {"aimId": ME, "friendly": "Test Person"},
    }
)


async def test_requesting_a_code_sends_the_desktop_request(fake, http):
    fake.on("POST", LOGIN, wim_ok({"token": {"a": ""}}))
    await request_code(INST, ME, http, dev_id="dev")
    s = fake.seen[-1]
    assert s.form["tokenType"] == "otp_via_email" and s.form["pwd"] == "none"
    assert s.form["s"] == ME and s.form["devId"] == "dev" and s.form["f"] == "json"


async def test_a_refused_code_request_does_not_say_whether_the_address_exists(fake, http):
    fake.on("POST", LOGIN, wim_error(330, "Password/LoginId Required/Invalid"))
    with pytest.raises(VKError) as e:
        await request_code(INST, "nobody@example.com", http, dev_id="dev")
    assert "nobody@example.com" not in e.value.text and "exist" not in e.value.text


async def test_finishing_sign_in_never_asks_for_another_code(fake, http):
    fake.on("POST", LOGIN, login_ok())
    fake.on("POST", f"{BASE}/wim/aim/startSession", START)
    s = await finish_sign_in(INST, ME, " ab12cd ", http, dev_id="dev")
    login = [x for x in fake.seen if x.path == LOGIN][0]
    assert login.form["pwd"] == "AB12CD"  # codes are upper case; spaces are a typo
    assert "otp_via_email" not in login.form.values()
    assert (s.aimsid, s.my_sn, s.token_a, s.host) == ("aimsid-x", ME, "tok", HOST)
    start = [x for x in fake.seen if x.path.endswith("startSession")][0]
    assert start.form["a"] == "tok" and start.form["sig_sha256"]


async def test_a_wrong_code_is_refused_and_nothing_is_resent(fake, http):
    fake.on("POST", LOGIN, wim_error(330, "Password/LoginId Required/Invalid"))
    with pytest.raises(VKError) as e:
        await finish_sign_in(INST, ME, "WRONG1", http, dev_id="dev")
    assert "code" in e.value.text.lower()
    assert all(x.form.get("tokenType") != "otp_via_email" for x in fake.seen)
    assert not [x for x in fake.seen if x.path.endswith("startSession")]


async def test_an_empty_token_on_finish_is_a_refusal_not_a_session(fake, http):
    fake.on("POST", LOGIN, wim_ok({"token": {"a": ""}}))
    with pytest.raises(VKError):
        await finish_sign_in(INST, ME, "AB12CD", http, dev_id="dev")


def limiter(clock=lambda: 0.0) -> CodeLimiter:
    return CodeLimiter(MemoryStore(derive_key("k" * 40), clock=clock))


async def test_code_requests_are_limited():
    now = [0.0]
    lim = limiter(lambda: now[0])
    for _ in range(3):
        await lim.take(address="a@example.com", network="198.51.100.1")
    with pytest.raises(VKError) as e:
        await lim.take(address="a@example.com", network="198.51.100.2")
    assert "a@example.com" not in e.value.text
    now[0] = 3601
    await lim.take(address="a@example.com", network="198.51.100.1")


async def test_one_network_cannot_mail_many_addresses():
    lim = limiter()
    for i in range(10):
        await lim.take(address=f"p{i}@example.com", network="198.51.100.9")
    with pytest.raises(VKError):
        await lim.take(address="p10@example.com", network="198.51.100.9")


async def test_addresses_are_compared_case_blind():
    lim = limiter()
    for a in ("A@Example.com", "a@example.com", "a@EXAMPLE.COM"):
        await lim.take(address=a, network=None)
    with pytest.raises(VKError):
        await lim.take(address="a@example.com", network=None)


@pytest.mark.parametrize(
    "body",
    [
        [1],
        {"response": "error"},
        {"response": {"statusCode": 200, "data": [1]}},
        {"response": {"statusCode": 200, "data": {"token": "x"}}},
    ],
)
async def test_a_strange_answer_from_the_typed_server_is_a_refusal_not_a_crash(fake, http, body):
    fake.on("POST", LOGIN, body)
    with pytest.raises(VKError):
        await request_code(INST, ME, http, dev_id="dev") if body == [1] else await finish_sign_in(
            INST, ME, "AB12CD", http, dev_id="dev"
        )


# ---------------------------------------------------------------- language


def test_the_language_follows_the_browser_by_quality():
    from vkteams_mcp.signin import pick_lang

    assert pick_lang("ru-RU,ru;q=0.9,en-US;q=0.8") == "ru"
    assert pick_lang("en-GB,ru;q=0.5") == "en"
    assert pick_lang("de-DE,ru;q=0.7,en;q=0.9") == "en"
    assert pick_lang("ru;q=0,en;q=0.1") == "en"  # q=0 means "not this one"
    assert pick_lang("") == pick_lang("fr,de") == pick_lang("ru;q=junk") == "en"


def test_both_languages_say_the_same_things():
    from vkteams_mcp.signin import T

    assert set(T["ru"]) == set(T["en"])
    for key, text in T["en"].items():
        assert ("{email}" in text) == ("{email}" in T["ru"][key]), key
        assert ("{n}" in text) == ("{n}" in T["ru"][key]), key


def test_every_refusal_the_page_can_show_has_a_russian_sentence():
    """The sentences come from api.install and api.login; a new one without a translation fails here."""
    import ast
    import inspect

    from vkteams_mcp.api import install, login
    from vkteams_mcp.signin import _RU_ERRORS

    found = set()
    for mod, fn in ((install, "_refuse"), (login, "invalid")):
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == fn and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    assert len(found) >= 10, found  # the scan itself found the refusals
    assert found == set(_RU_ERRORS)  # nothing untranslated, nothing stale


def test_a_refusal_with_the_address_hint_is_translated_whole():
    from vkteams_mcp.api.install import _refuse
    from vkteams_mcp.signin import translate

    text = _refuse("Only https addresses are accepted.").text
    ru = translate("ru", text)
    assert ru.startswith("Подходят только адреса https.") and "Type the address" not in ru
    assert translate("en", text) == text
    assert translate("ru", "Some new sentence.") == "Some new sentence."
