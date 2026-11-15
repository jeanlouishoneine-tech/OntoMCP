"""The active-profile pointer: a saved profile is the one actually in use.

Regression for the bug where save_profile wrote ``plant-breeding`` and replied
"active now" while registry.current() kept serving the built-in default. No
network: freeze runs against a MockTransport client.
"""

import httpx
import pytest

from ontomcp import cli
from ontomcp.core import config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import save_profile


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)


def _ols() -> OLSClient:
    def handler(request):
        if "/terms" in str(request.url):
            return httpx.Response(200, json={"_embedded": {"terms": []}})
        return httpx.Response(200, json={"version": "2026-07-26"})

    return OLSClient(
        client=httpx.AsyncClient(
            base_url=config.OLS_BASE_URL, transport=httpx.MockTransport(handler)
        )
    )


async def _save(name, ontologies):
    async with _ols() as client:
        result, _ = await save_profile(name, ontologies, client=client)
    return result


async def test_saved_profile_becomes_active_without_restart():
    result = await _save("plant-breeding", ["PO", "TO", "GO", "PECO", "ENVO"])
    active = registry.current()
    assert active.profile == "plant-breeding"
    assert active.source == "pointer"
    assert set(active.all_members) == {"PO", "TO", "GO", "PECO", "ENVO"}
    assert result["active_profile"] == "plant-breeding"
    assert "active now" in result["next_step"]


async def test_env_pin_outranks_the_pointer_and_the_response_says_so(monkeypatch):
    await _save("other", ["GO", "CHEBI"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "other")
    result = await _save("plant-breeding", ["PO", "TO"])
    assert registry.current().profile == "other"
    assert result["active_profile"] == "other"
    assert result["active_source"] == "env"
    assert "ONTOMCP_PROFILE" in result["next_step"]
    assert "active now" not in result["next_step"]


async def test_pointer_to_a_missing_profile_falls_back_with_a_warning():
    profile_mod.set_active("ghost")
    active = registry.current()
    assert active.source == "builtin"
    assert any("ghost" in w for w in active.warnings)


async def test_broken_pointer_falls_through_to_default_json():
    await _save("default", ["GO"])
    profile_mod.set_active("ghost")
    active = registry.current()
    assert active.profile == "default"
    assert active.source == "default_file"


async def test_pointer_change_is_picked_up_without_restart():
    await _save("a", ["GO"])
    await _save("b", ["CHEBI"])
    assert registry.current().profile == "b"
    profile_mod.set_active("a")
    assert registry.current().profile == "a"
    profile_mod.clear_active()
    assert registry.current().source == "builtin"


def test_set_active_rejects_invalid_names():
    with pytest.raises(profile_mod.ProfileError):
        profile_mod.set_active("../evil")


async def test_cli_use_sets_the_pointer(capsys):
    await _save("a", ["GO"])
    await _save("b", ["CHEBI"])
    assert cli.main(["profile", "use", "a"]) == 0
    assert registry.current().profile == "a"
    assert cli.main(["profile", "use", "nope"]) == 1
    assert registry.current().profile == "a"
