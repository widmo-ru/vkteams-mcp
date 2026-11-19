"""`as_int`: what `int()` will take, and nothing it will choke on."""

from __future__ import annotations

import pytest

from vkteams_mcp.core.num import as_int


@pytest.mark.parametrize(("value", "want"), [(5, 5), ("12", 12), (" 7 ", 7), ("-3", -3), ("0", 0)])
def test_numbers_are_numbers(value, want):
    assert as_int(value) == want


@pytest.mark.parametrize("value", ["²", "١٢", "1_000", "", "x", "1.5", None, True, 1.5, "9" * 30, [1]])
def test_anything_else_is_none_not_an_exception(value):
    assert as_int(value) is None


def test_a_superscript_digit_in_a_header_or_a_setting_is_refused_not_a_crash(tmp_path):
    from test_key import KEY_A, env_with, key_file

    from vkteams_mcp.api.client import _retry_after
    from vkteams_mcp.config import ConfigError, Settings

    assert _retry_after("²") == 30
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env_with(key_file(tmp_path, KEY_A), VKTEAMS_MCP_METRICS_PORT="²"))
    assert "METRICS_PORT" in str(e.value)
