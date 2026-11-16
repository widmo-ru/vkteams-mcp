"""The server address is typed by a stranger on a public page: prove it is a VK Teams installation
before trusting it, and never let it point the server at a private network."""

from __future__ import annotations

import pytest
from conftest import API, BIN_HOST, CONFIG, HOST, public_resolver

from vkteams_mcp.api.install import Policy, discover, normalise
from vkteams_mcp.core.errors import VKError


@pytest.mark.parametrize(
    "typed,host",
    [
        ("teams.example.com", "teams.example.com"),
        ("https://teams.example.com/", "teams.example.com"),
        ("  Teams.Example.COM  ", "teams.example.com"),
        ("https://teams.example.com/some/path?x=1", "teams.example.com"),
    ],
)
def test_an_address_is_normalised_to_a_host(typed, host):
    assert normalise(typed) == host


@pytest.mark.parametrize(
    "typed",
    [
        "http://teams.example.com",
        "ftp://teams.example.com",
        "10.0.0.5",
        "https://127.0.0.1",
        "[::1]",
        "https://teams.example.com:8443",
        "user@teams.example.com",
        "localhost",
        "",
        "a" * 300 + ".com",
        "https://teams.example.com@evil.com",
    ],
)
def test_private_and_plain_addresses_are_refused(typed):
    with pytest.raises(VKError):
        normalise(typed)


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "172.16.0.1",
        "::1",
        "fd00::1",
        "0.0.0.0",
        "100.64.0.1",
    ],
)
async def test_a_host_resolving_to_a_private_network_is_refused(http, fake, ip):
    with pytest.raises(VKError) as e:
        await discover("teams.example.com", http, Policy(), resolve=lambda h: [ip])
    assert "private" in e.value.text
    assert fake.seen == []  # nothing was fetched


async def test_private_networks_can_be_allowed_by_the_operator(http, fake):
    inst = await discover("teams.example.com", http, Policy(allow_private=True), resolve=lambda h: ["10.1.2.3"])
    assert inst.host == HOST


async def test_a_host_with_a_vk_config_is_accepted(http, fake):
    inst = await discover("teams.example.com", http, Policy(), resolve=public_resolver)
    assert (inst.host, inst.binary_host, inst.api_version) == (HOST, BIN_HOST, API)
    assert fake.seen[0].path == "/myteam-config.json"


async def test_a_host_without_vk_config_is_refused(http, fake):
    fake.routes.clear()
    with pytest.raises(VKError) as e:
        await discover("teams.example.com", http, Policy(), resolve=public_resolver)
    assert "not a VK Teams server" in e.value.text


@pytest.mark.parametrize(
    "bad",
    [
        {"api-version": "x"},
        {"api-urls": {}},
        {"api-version": 139},
        {"api-version": 139, "api-urls": {"main-api": "http://plain.example.com"}},
    ],
)
async def test_a_config_without_usable_api_urls_is_refused(http, fake, bad):
    fake.routes.clear()
    fake.on("GET", "/myteam-config.json", bad)
    with pytest.raises(VKError):
        await discover("teams.example.com", http, Policy(), resolve=public_resolver)


async def test_the_api_host_named_by_the_config_is_checked_too(http, fake):
    fake.routes.clear()
    cfg = dict(CONFIG, **{"api-urls": {"main-api": "https://internal.example.com"}})
    fake.on("GET", "/myteam-config.json", cfg)
    with pytest.raises(VKError):
        await discover(
            "teams.example.com",
            http,
            Policy(),
            resolve=lambda h: ["10.0.0.1"] if h == "internal.example.com" else ["93.184.215.14"],
        )


async def test_pinned_installations_only(http, fake):
    policy = Policy(installations=frozenset({"other.example.com"}))
    with pytest.raises(VKError) as e:
        await discover("teams.example.com", http, policy, resolve=public_resolver)
    assert "only" in e.value.text and fake.seen == []
    inst = await discover("teams.example.com", http, Policy(installations=frozenset({HOST})), resolve=public_resolver)
    assert inst.host == HOST


async def test_redirects_are_not_followed(http, fake):
    import httpx

    fake.routes.clear()
    fake.on("GET", "/myteam-config.json", httpx.Response(302, headers={"Location": "http://10.0.0.1/"}))
    with pytest.raises(VKError):
        await discover("teams.example.com", http, Policy(), resolve=public_resolver)
    assert len(fake.seen) == 1
