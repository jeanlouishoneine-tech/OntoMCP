"""get_profile and update_profile: view, edit, reset and undo from the conversation.

No network: freeze runs against a MockTransport client. Writes go through the
gate with no confirm callback (the client-permission path) unless a test says
otherwise.
"""

import httpx
import pytest

from ontomcp.core import config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import get_profile, save_profile, update_profile


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    monkeypatch.setattr(config, "REQUIRE_CONFIRMATION", False)


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


async def _update(**kwargs):
    async with _ols() as client:
        result, _ = await update_profile(client=client, **kwargs)
    return result


# --- get_profile -----------------------------------------------------------


def test_get_profile_reports_the_builtin_default():
    result, _ = get_profile()
    assert result["source"] == "builtin"
    assert result["undo_available"] is False
    go = next(m for m in result["members"] if m["ontology"] == "GO")
    assert go["title"] == "Gene Ontology" and go["terms"] > 0


async def test_get_profile_reports_a_saved_profile_and_its_source():
    await _save("plant", ["PO", "TO", "GO"])
    result, _ = get_profile()
    assert result["profile"] == "plant"
    assert result["source"] == "pointer"
    assert {m["ontology"] for m in result["members"]} == {"PO", "TO", "GO"}


async def test_get_profile_reports_an_env_pin(monkeypatch):
    await _save("plant", ["PO"])
    await _save("other", ["GO"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant")
    result, _ = get_profile()
    assert (result["profile"], result["source"]) == ("plant", "env")


async def test_get_profile_says_when_an_undo_exists():
    await _save("plant", ["PO"])
    await _save("plant", ["PO", "TO"])
    assert get_profile()[0]["undo_available"] is True


# --- update_profile: add / remove ------------------------------------------


async def test_add_and_remove_change_the_active_profile():
    await _save("plant", ["PO", "TO", "ENVO"])
    result = await _update(add=["NCBITaxon"], remove=["ENVO"])
    assert result["status"] == "saved"
    assert set(registry.current().all_members) == {"PO", "TO", "NCBITAXON"}


async def test_editing_the_builtin_default_saves_it_as_default():
    result = await _update(remove=["MESH"])
    assert result["status"] == "saved"
    assert result["profile"] == "default"
    assert "MESH" not in registry.current().all_members


async def test_an_unknown_prefix_is_prose_with_a_suggestion():
    await _save("plant", ["PO"])
    result = await _update(add=["MOONDO"])
    assert result["error"] == "profile_invalid"
    assert "MONDO" in result["detail"]


async def test_removing_something_not_in_the_profile_is_refused():
    await _save("plant", ["PO", "TO"])
    result = await _update(remove=["GO"])
    assert result["error"] == "bad_request"


async def test_removing_everything_is_refused_with_a_pointer_to_reset():
    await _save("plant", ["PO"])
    result = await _update(remove=["PO"])
    assert result["error"] == "bad_request"
    assert "reset" in result["detail"]


async def test_going_over_the_cap_is_refused():
    await _save("big", ["GO", "CHEBI", "PO", "TO", "ENVO", "UBERON", "CL", "HP"])
    extra = ["MONDO", "DOID", "NCIT", "OBI", "SO"]
    result = await _update(add=extra)
    assert result["error"] == "profile_invalid"
    assert str(config.PROFILE_MAX) in result["detail"]


async def test_bad_argument_combinations_are_refused():
    assert (await _update())["error"] == "bad_request"
    assert (await _update(add=["GO"], reset="defaults"))["error"] == "bad_request"
    assert (await _update(reset="everything"))["error"] == "bad_request"


# --- update_profile: reset -------------------------------------------------


async def test_reset_defaults_goes_back_to_the_builtin_set():
    await _save("plant", ["PO"])
    result = await _update(reset="defaults")
    assert result["status"] == "reset"
    assert registry.current().source == "builtin"


async def test_reset_defaults_when_already_on_defaults_is_a_no_op():
    assert (await _update(reset="defaults"))["status"] == "unchanged"


async def test_reset_defaults_under_an_env_pin_explains_the_pin(monkeypatch):
    await _save("plant", ["PO"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant")
    result = await _update(reset="defaults")
    assert result["error"] == "pinned_by_config"


async def test_reset_previous_restores_the_prior_list():
    await _save("plant", ["PO", "TO", "ENVO"])
    await _update(remove=["ENVO"])
    result = await _update(reset="previous")
    assert result["status"] == "restored"
    assert set(registry.current().all_members) == {"PO", "TO", "ENVO"}


async def test_reset_previous_with_nothing_to_undo_is_prose():
    await _save("plant", ["PO"])
    assert (await _update(reset="previous"))["error"] == "nothing_to_undo"


async def test_update_keeps_force_default_and_description():
    async with _ols() as client:
        await save_profile(
            "tax", ["GO", "NCBITaxon"], force_default=["NCBITaxon"], description="d", client=client
        )
    await _update(add=["CHEBI"])
    stored = profile_mod.load("tax")
    assert stored["force_default"] == ["NCBITAXON"]
    assert stored["description"] == "d"
