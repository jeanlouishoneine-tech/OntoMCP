"""The profile write gate: nothing is written unless the human confirms.

The confirm callback is a plain async fake — the gate is FastMCP-free by design.
No network: freeze runs against a MockTransport client.
"""

import asyncio
import json

import httpx
import pytest

from ontomcp.core import config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.profile_gate import ProfileChange, confirm_and_apply


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


class FakeConfirm:
    """Records every message it is shown and answers with a fixed decision."""

    def __init__(self, decision="accepted", *, hang=False, error=None):
        self.decision, self.hang, self.error = decision, hang, error
        self.messages: list[str] = []

    async def __call__(self, message):
        self.messages.append(message)
        if self.error:
            raise self.error
        if self.hang:
            await asyncio.sleep(10)
        return self.decision


def _replace(name="t", ontologies=("GO", "CHEBI"), before=()):
    return ProfileChange("replace", name, resulting=tuple(ontologies), before=tuple(before))


async def _run(change, confirm):
    async with _ols() as client:
        return await confirm_and_apply(change, confirm, client=client)


# --- the decision decides --------------------------------------------------


async def test_accept_writes_and_activates():
    result = await _run(_replace(), FakeConfirm("accepted"))
    assert result["status"] == "saved"
    assert result["confirmation"] == "elicitation"
    assert profile_mod.profile_path("t").exists()
    assert registry.current().profile == "t"


@pytest.mark.parametrize("decision", ["declined", "cancelled"])
async def test_decline_or_cancel_writes_nothing(decision):
    result = await _run(_replace(), FakeConfirm(decision))
    assert result["status"] == "declined"
    assert not profile_mod.profile_path("t").exists()
    assert profile_mod.read_active() is None


async def test_no_answer_times_out_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(config, "ELICIT_TIMEOUT_SECONDS", 0.05)
    result = await _run(_replace(), FakeConfirm(hang=True))
    assert result["status"] == "no_response"
    assert not profile_mod.profile_path("t").exists()


async def test_a_failing_confirmation_never_falls_through_to_a_write():
    result = await _run(_replace(), FakeConfirm(error=RuntimeError("transport closed")))
    assert result["error"] == "confirmation_failed"
    assert not profile_mod.profile_path("t").exists()


# --- clients that cannot ask in-app ----------------------------------------


async def test_without_in_app_confirmation_the_client_permission_prompt_counts():
    result = await _run(_replace(), None)
    assert result["status"] == "saved"
    assert result["confirmation"] == "client_tool_permission"


async def test_strict_mode_refuses_without_in_app_confirmation(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_CONFIRMATION", True)
    result = await _run(_replace(), None)
    assert result["error"] == "confirmation_unavailable"
    assert "profile create t --ontologies GO,CHEBI" in result["cli_equivalent"]
    assert not profile_mod.profile_path("t").exists()


# --- refusals come before any question --------------------------------------


async def test_admin_lock_refuses_before_asking(monkeypatch):
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "GO")
    confirm = FakeConfirm()
    result = await _run(_replace(), confirm)
    assert result["error"] == "profile_writes_disabled"
    assert confirm.messages == []


async def test_locked_profile_refuses_before_asking():
    profile_mod.profiles_dir().mkdir(parents=True)
    profile_mod.profile_path("t").write_text(json.dumps({"ontologies": ["GO"], "locked": True}))
    confirm = FakeConfirm()
    result = await _run(_replace(), confirm)
    assert "locked" in result["detail"]
    assert confirm.messages == []


async def test_an_invalid_prefix_is_refused_before_asking():
    confirm = FakeConfirm()
    result = await _run(_replace(ontologies=("MOONDO",)), confirm)
    assert result["error"] == "profile_invalid"
    assert confirm.messages == []


# --- what the human sees -----------------------------------------------------


async def test_message_lists_every_prefix_with_its_title():
    confirm = FakeConfirm("declined")
    await _run(_replace(ontologies=("GO", "CHEBI")), confirm)
    message = confirm.messages[0]
    assert "GO: Gene Ontology" in message
    assert "CHEBI: " in message and "terms)" in message


async def test_message_shows_adding_and_removing_for_a_change():
    confirm = FakeConfirm("declined")
    await _run(_replace(ontologies=("GO", "CHEBI"), before=("GO", "PATO")), confirm)
    message = confirm.messages[0]
    assert "Adding:\n  CHEBI: " in message
    assert "Removing:\n  PATO: " in message


# --- reset and undo go through the same gate --------------------------------


async def test_reset_to_defaults_clears_the_pointer_and_keeps_the_file():
    await _run(_replace(), None)
    result = await _run(ProfileChange("reset_defaults", "t"), FakeConfirm())
    assert result["status"] == "reset"
    assert registry.current().source == "builtin"
    assert profile_mod.profile_path("t").exists()


async def test_undo_shows_the_difference_and_restores():
    await _run(_replace(ontologies=("GO",)), None)
    await _run(_replace(ontologies=("GO", "CHEBI"), before=("GO",)), None)
    confirm = FakeConfirm()
    result = await _run(ProfileChange("restore_previous", "t"), confirm)
    assert result["status"] == "restored"
    assert "Removing:\n  CHEBI: " in confirm.messages[0]
    assert profile_mod.current_ontologies("t") == ["GO"]


async def test_undo_with_nothing_to_undo_is_prose():
    result = await _run(ProfileChange("restore_previous", "t"), FakeConfirm())
    assert result["error"] == "nothing_to_undo"
