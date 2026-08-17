"""Profiles, precedence and scope enforcement — the Phase-B gate.

Every assertion maps to a gate clause in the scaling plan §6 Phase B. No network:
freeze is exercised against a MockTransport client, so the probe path is covered
without the tests depending on OLS being up.
"""

import json

import httpx
import pytest

from ontomcp.core import config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import bulk_annotate, map_across_ontologies, save_profile, search_terms


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.ontomcp/profiles."""
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    return tmp_path


@pytest.fixture
def db(tmp_path):
    from ontomcp.core import cache

    path = tmp_path / "cache.db"
    cache.init_db(path)
    return path


def _ols(handler=None):
    """A client whose probes succeed, so freeze records verified members."""

    def default(request):
        url = str(request.url)
        if "/terms" in url:
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "terms": [
                            {"obo_id": "", "short_form": "", "iri": "https://example.org/licence"},
                            {
                                "obo_id": "GO:0008219",
                                "short_form": "GO_0008219",
                                "iri": "http://purl.obolibrary.org/obo/GO_0008219",
                            },
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"version": "2026-07-26"})

    return OLSClient(
        client=httpx.AsyncClient(
            base_url=config.OLS_BASE_URL, transport=httpx.MockTransport(handler or default)
        )
    )


def _search_capturing(seen):
    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "response": {
                    "docs": [
                        {"obo_id": "GO:0008219", "label": "cell death", "iri": "http://x/GO_1"}
                    ]
                }
            },
        )

    return handler


async def _make(name, ontologies, force_default=None):
    async with _ols() as cli:
        return await profile_mod.save(
            name, ontologies, force_default=force_default or [], client=cli
        )


# --- freeze pins inputs ----------------------------------------------------


async def test_freeze_pins_inputs_not_outputs():
    profile = await _make("t", ["GO", "CHEBI"])
    derived = profile["_derived"]
    # The pinned inputs...
    assert derived["index_version"]
    assert derived["frozen_at"]
    assert set(derived["members"]) == {"GO", "CHEBI"}
    # ...and NOT the computed outputs, which resolve() derives in one line.
    assert "default_scope" not in derived
    assert "named_only" not in derived


async def test_freeze_records_the_per_profile_quarantine_map_with_reasons():
    # A scientist reading their own JSON must see which of THEIR ontologies is
    # held back and why — without the file carrying a second derived copy of a
    # computable fact.
    profile = await _make("t", ["GO", "NCBITAXON"])
    assert "NCBITAXON" in profile["_derived"]["quarantine"]
    assert "genus/species" in profile["_derived"]["quarantine"]["NCBITAXON"]
    assert "GO" not in profile["_derived"]["quarantine"]


async def test_freeze_probe_skips_non_class_artifacts():
    """S-8: /terms often starts with a licence node with an empty obo_id.

    Reading size=1 would record curie_prefix "" for every profile with that member.
    """
    profile = await _make("t", ["GO"])
    assert profile["_derived"]["members"]["GO"]["curie_prefix"] == "GO"
    assert profile["_derived"]["members"]["GO"]["base_uri"] == (
        "http://purl.obolibrary.org/obo/GO_"
    )


async def test_freeze_probe_short_form_only_records_the_real_minting_prefix():
    """A term with only `short_form` (no `obo_id`) must not have the canonical
    registry alias (HP -> HPO) re-applied to the probed minting prefix — that
    defeats the entire point of the probe, which is to observe what OLS itself
    mints, not to recompute the alias it already knows about.
    """

    def handler(request):
        url = str(request.url)
        if "/terms" in url:
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "terms": [
                            {
                                "short_form": "HP_0001250",
                                "iri": "http://purl.obolibrary.org/obo/HP_0001250",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"version": "2026-07-26"})

    async with _ols(handler) as cli:
        profile = await profile_mod.save("t", ["HPO"], client=cli)

    member = profile["_derived"]["members"]["HPO"]
    assert member["curie_prefix"] == "HP"  # not "HPO"
    assert member["base_uri"] == "http://purl.obolibrary.org/obo/HP_"


async def test_freeze_member_order_matches_between_in_memory_and_saved_profile():
    profile = await _make("t", ["GO", "CHEBI", "ENVO"])
    # save() writes to disk with sort_keys=True (alphabetical); the in-memory
    # object freeze() returns must already agree, or the same freeze event
    # exposes two different orderings depending on which one the caller sees.
    assert list(profile["_derived"]["members"]) == sorted(profile["_derived"]["members"])
    reloaded = profile_mod.load("t")
    assert list(reloaded["_derived"]["members"]) == list(profile["_derived"]["members"])


async def test_freeze_proceeds_on_transport_failure_and_records_unverified():
    def broken(request):
        raise httpx.ConnectError("no route to host")

    async with _ols(broken) as cli:
        profile = await profile_mod.save("t", ["GO"], client=cli)

    assert profile["_derived"]["members"]["GO"]["verified"] is False
    assert any("unverified" in w for w in profile["_derived"]["warnings"])


async def test_freeze_probe_does_not_mislabel_a_transient_version_failure_as_unversioned():
    """A failed /ontologies/{slug} call (version endpoint) must not be recorded
    as "unversioned" just because the /terms probe right after it succeeds —
    those are different failure modes and the profile JSON must not conflate
    them (see profile.py's own comment on this distinction).
    """

    def handler(request):
        url = str(request.url)
        if "/terms" in url:
            return httpx.Response(
                200,
                json={"_embedded": {"terms": [{"obo_id": "GO:1", "iri": "http://x/GO_1"}]}},
            )
        return httpx.Response(500)  # the version call fails; unrelated to /terms

    async with _ols(handler) as cli:
        profile = await profile_mod.save("t", ["GO"], client=cli)

    member = profile["_derived"]["members"]["GO"]
    assert member.get("unversioned") is not True
    assert member["version"] is None
    assert member["verified"] is True  # /terms still proved OLS was reachable


async def test_require_verified_refuses_instead():
    def broken(request):
        raise httpx.ConnectError("no route to host")

    async with _ols(broken) as cli:
        with pytest.raises(profile_mod.ProfileError, match="require-verified"):
            await profile_mod.save("t", ["GO"], client=cli, require_verified=True)


async def test_freeze_cannot_write_into_the_cache_version_table():
    """B6: ontology_versions has no TTL, so a freeze-time write would echo forever.

    Asserted structurally rather than by observing an empty table — freeze takes no
    ``db_path`` at all, so it has no way to reach the cache. A behavioural check
    against a fresh database would pass even if the parameter were added back.
    """
    import inspect

    assert "db_path" not in inspect.signature(profile_mod.freeze).parameters
    assert "db_path" not in inspect.signature(profile_mod.save).parameters
    assert "cache" not in inspect.getsource(profile_mod)


async def test_profile_cap_is_enforced():
    codes = [
        "GO",
        "CHEBI",
        "MONDO",
        "HPO",
        "UBERON",
        "CL",
        "EFO",
        "MESH",
        "NCIT",
        "DOID",
        "PO",
        "TO",
        "ENVO",
    ]
    with pytest.raises(profile_mod.ProfileError, match="hard cap"):
        await _make("t", codes)


async def test_unknown_prefix_is_refused_with_a_suggestion():
    with pytest.raises(profile_mod.ProfileError, match="MONDO"):
        await _make("t", ["MOONDO"])


# --- precedence ------------------------------------------------------------


async def test_named_profile_scopes_search(db, monkeypatch):
    """Gate: ONTOMCP_PROFILE=plant-breeding searches PO/TO/PECO/GO/ENVO/UO, not NCBITAXON."""
    await _make("plant-breeding", ["PO", "TO", "PECO", "GO", "ENVO", "NCBITAXON", "UO"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")

    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("rice", db_path=db, client=cli)

    slugs = set(seen["params"]["ontology"].split(","))
    assert slugs == {"po", "to", "peco", "go", "envo", "uo"}
    assert "ncbitaxon" not in slugs


async def test_a_held_back_member_is_still_searchable_by_name(db, monkeypatch):
    await _make("plant-breeding", ["PO", "TO", "GO", "NCBITAXON"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")

    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("Oryza", ontologies=["NCBITAXON"], db_path=db, client=cli)
    assert seen["params"]["ontology"] == "ncbitaxon"


async def test_force_default_puts_taxonomy_back_in_default_scope(db, monkeypatch):
    """Gate: ONTOMCP_PROFILE=fermentation DOES hit NCBITAXON."""
    await _make("fermentation", ["NCBITAXON", "CHEBI", "GO"], force_default=["NCBITAXON"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "fermentation")

    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("yeast", db_path=db, client=cli)
    assert "ncbitaxon" in seen["params"]["ontology"].split(",")


async def test_env_lock_beats_a_profile_and_applies_no_quarantine(db, monkeypatch):
    """Gate: ONTOMCP_ONTOLOGIES="NCBITAXON,GO" searches NCBITaxon."""
    await _make("plant-breeding", ["PO", "TO"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "NCBITAXON,GO")

    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("x", db_path=db, client=cli)
    assert set(seen["params"]["ontology"].split(",")) == {"ncbitaxon", "go"}


async def test_no_profile_reproduces_todays_behaviour(db, monkeypatch):
    """Gate: with no profile set, search_terms returns today's results."""
    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("cell death", db_path=db, client=cli)
    assert set(seen["params"]["ontology"].split(",")) == {
        registry.slug_for(k) for k in config.ONTOLOGIES
    }


async def test_default_json_is_picked_up_without_being_named(db, monkeypatch):
    await _make("default", ["GO", "CHEBI"])
    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await search_terms("x", db_path=db, client=cli)
    assert set(seen["params"]["ontology"].split(",")) == {"go", "chebi"}


# --- structured rejections (F3) --------------------------------------------


async def test_out_of_profile_and_unknown_return_different_errors(db, monkeypatch):
    """Gate: ontologies=["FOODON"] and ["MOONDO"] return *different* errors."""
    await _make("fermentation", ["GO", "CHEBI"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "fermentation")

    async with _ols() as cli:
        real, _ = await search_terms("x", ontologies=["FOODON"], db_path=db, client=cli)
        typo, _ = await search_terms("x", ontologies=["MOONDO"], db_path=db, client=cli)

    assert real[0]["error"] == "out_of_profile"
    assert real[0]["title"] == "Food Ontology"
    assert typo[0]["error"] == "unknown_ontology"
    assert "MONDO" in typo[0]["did_you_mean"]


async def test_map_across_ontologies_requires_membership(db, monkeypatch):
    await _make("fermentation", ["GO", "CHEBI"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "fermentation")

    async with _ols() as cli:
        result, _ = await map_across_ontologies("GO:1", "FOODON", db_path=db, client=cli)
    assert result[0]["error"] == "out_of_profile"


# --- bulk_annotate resolves against the FULL profile -----------------------


async def test_bulk_annotate_searches_held_back_members(db, monkeypatch):
    """Gate: bulk_annotate maps a species column under plant-breeding with no hint.

    A spreadsheet with a species column must not silently fail under a profile
    that quarantines NCBITaxon — that is what bulk annotation is for.
    """
    await _make("plant-breeding", ["PO", "TO", "GO", "NCBITAXON"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")

    seen = {}
    async with _ols(_search_capturing(seen)) as cli:
        await bulk_annotate(["Oryza sativa"], db_path=db, client=cli)

    assert "ncbitaxon" in seen["params"]["ontology"].split(",")


# --- drift is surfaced, never applied --------------------------------------


async def test_stale_index_version_still_resolves_from_its_pin(monkeypatch):
    """Gate: a profile with a stale _derived.index_version resolves and reports drift."""
    profile = await _make("t", ["GO", "CHEBI"])
    path = profile_mod.profile_path("t")
    profile["_derived"]["index_version"] = "2020.01+deadbeef"
    path.write_text(json.dumps(profile))

    monkeypatch.setenv("ONTOMCP_PROFILE", "t")
    resolved = registry.current()

    assert set(resolved.default_scope) == {"GO", "CHEBI"}  # resolved from the pin
    assert any("frozen against index 2020.01" in w for w in resolved.warnings)


async def test_a_withdrawn_member_is_named_in_the_drift_note(monkeypatch):
    profile = await _make("t", ["GO"])
    profile["_derived"]["members"]["GONE"] = {"slug": "gone", "title": "Gone"}
    profile_mod.profile_path("t").write_text(json.dumps(profile))

    monkeypatch.setenv("ONTOMCP_PROFILE", "t")
    assert any("GONE" in w for w in registry.current().warnings)


# --- the registry re-reads without a restart -------------------------------


async def test_editing_a_profile_is_picked_up_without_a_restart(db, monkeypatch):
    """Gate: editing a profile file is picked up without a client restart."""
    await _make("t", ["GO"])
    monkeypatch.setenv("ONTOMCP_PROFILE", "t")
    assert set(registry.current().default_scope) == {"GO"}

    await _make("t", ["GO", "CHEBI"])
    assert set(registry.current().default_scope) == {"GO", "CHEBI"}


# --- failure messages ride on the response ---------------------------------


async def test_a_missing_profile_degrades_to_the_default_with_a_warning(monkeypatch):
    """Fail open; never refuse to start. stdio stderr is invisible in Claude Desktop."""
    monkeypatch.setenv("ONTOMCP_PROFILE", "nope")
    resolved = registry.current()
    assert set(resolved.default_scope) == set(config.ONTOLOGIES)
    assert any("not found" in w and "Available" in w for w in resolved.warnings)


async def test_a_malformed_profile_degrades_with_the_path_named(monkeypatch):
    profile_mod.profiles_dir().mkdir(parents=True, exist_ok=True)
    profile_mod.profile_path("broken").write_text("{not json")
    monkeypatch.setenv("ONTOMCP_PROFILE", "broken")

    resolved = registry.current()
    assert set(resolved.default_scope) == set(config.ONTOLOGIES)
    assert any("not valid JSON" in w for w in resolved.warnings)


async def test_an_unfrozen_profile_resolves_with_a_warning_naming_the_fix(monkeypatch):
    profile_mod.profiles_dir().mkdir(parents=True, exist_ok=True)
    profile_mod.profile_path("hand").write_text(json.dumps({"ontologies": ["GO", "CHEBI"]}))
    monkeypatch.setenv("ONTOMCP_PROFILE", "hand")

    resolved = registry.current()
    assert set(resolved.default_scope) == {"GO", "CHEBI"}
    assert any("never been frozen" in w for w in resolved.warnings)


# --- save_profile ----------------------------------------------------------


async def test_save_profile_returns_titles_and_term_counts():
    async with _ols() as cli:
        result, _ = await save_profile("t", ["GO", "CHEBI"], client=cli)
    titles = {m["ontology"]: m["title"] for m in result["members"]}
    assert titles["GO"] == "Gene Ontology"
    assert all(m["terms"] > 0 for m in result["members"])


async def test_save_profile_is_refused_under_the_admin_lock(monkeypatch):
    """Gate: save_profile is refused under the admin lock."""
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "GO,MONDO")
    async with _ols() as cli:
        result, _ = await save_profile("t", ["GO"], client=cli)
    assert result["error"] == "profile_writes_disabled"
    assert not profile_mod.profile_path("t").exists()


async def test_save_profile_is_refused_on_a_locked_profile():
    profile_mod.profiles_dir().mkdir(parents=True, exist_ok=True)
    profile_mod.profile_path("team").write_text(json.dumps({"ontologies": ["GO"], "locked": True}))
    async with _ols() as cli:
        result, _ = await save_profile("team", ["GO", "CHEBI"], client=cli)
    assert result["error"] == "profile_invalid"
    assert "locked" in result["detail"]


def test_profile_path_rejects_path_traversal_names():
    """profile_path is the one place load/save/delete resolve a name to a file.

    A name with path segments must be refused there, not merely by save()'s
    own separate check, so load() and delete() can't be reached with one.
    """
    with pytest.raises(profile_mod.ProfileError, match="not a valid profile name"):
        profile_mod.profile_path("../evil")


async def test_delete_rejects_path_traversal_names():
    with pytest.raises(profile_mod.ProfileError, match="not a valid profile name"):
        profile_mod.delete("../evil")


async def test_save_profile_reports_a_bad_prefix_as_prose_not_an_exception():
    async with _ols() as cli:
        result, _ = await save_profile("t", ["MOONDO"], client=cli)
    assert result["error"] == "profile_invalid"
    assert "MONDO" in result["detail"]


async def test_create_and_edit_always_refreeze():
    """B2: there is no unfreeze verb and no unfrozen state a tool can produce."""
    first = await _make("t", ["GO"])
    second = await _make("t", ["GO", "CHEBI"])
    assert second["_derived"]["frozen_at"] >= first["_derived"]["frozen_at"]
    assert set(second["_derived"]["members"]) == {"GO", "CHEBI"}


# --- one enforcement point: all three surfaces agree -----------------------


async def test_core_and_panel_agree_on_scope(db, monkeypatch):
    """Gate, core + Jupyter halves. The REST half is test_api.py::test_scope_error_matches_core.

    v3 claimed normalize_ontologies was already the single enforcement point. It
    was not — cache.py had a second, independent one, so a scoped query was a
    silent partial truth flagged cache_hit=True.
    """
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "PO,TO")

    async with _ols() as cli:
        core_result, _ = await search_terms("x", ontologies=["GO"], db_path=db, client=cli)

    assert core_result[0]["error"] == "out_of_profile"
    assert core_result[0]["ontology"] == "GO"
    # The panel renders the resolved registry, so its checkboxes are the same scope.
    assert set(registry.current().all_members) == {"PO", "TO"}
