"""Registry, index generation and validation — the Phase-A gate.

Every assertion here maps to a gate clause in the scaling plan §6 Phase A. The
OLS4 response they run against is a real captured one
(``tests/fixtures/ols4_ontologies.json``, 286 entries, 2026-08-15), not a
hand-written stub, so a rule that only works on invented numbers fails here.
"""

import copy
import json
from pathlib import Path

import pytest

from ontomcp.core import registry
from ontomcp.core.config import ONTOLOGIES, QUARANTINE

FIXTURE = Path(__file__).parent / "fixtures" / "ols4_ontologies.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def index(payload) -> dict:
    return registry.build_index(payload)


# --- the searchable rule ---------------------------------------------------


def test_searchable_classifies_ro_false_and_go_true(index):
    # The gate clause. v3's rule (properties > 0 AND terms == 0) classified
    # nothing; this one is `not (properties > terms)`. Live numbers: RO has 58
    # terms and 829 properties, GO has 85,169 and 484.
    assert index["ontologies"]["RO"]["searchable"] is False
    assert index["ontologies"]["GO"]["searchable"] is True


def test_searchable_is_true_for_large_ontologies_regardless_of_size(index):
    # Size is not the axis. NCBITaxon is enormous and perfectly searchable; it is
    # held back by QUARANTINE for label genericity, which is a different mechanism.
    assert index["ontologies"]["NCBITAXON"]["searchable"] is True
    assert index["ontologies"]["CHEBI"]["searchable"] is True


def test_zero_metadata_is_searchable_plus_a_review_note():
    # A present-and-zero count is OLS4's least trustworthy value, so it must never
    # silently vanish an ontology from search.
    searchable, note = registry._is_searchable(0, 0)
    assert searchable is True
    assert note is not None


def test_absent_metadata_fails_open():
    assert registry._is_searchable(None, None) == (True, None)


# --- key canonicalization --------------------------------------------------


def test_every_index_key_is_uppercase(index):
    assert all(key == key.upper() for key in index["ontologies"])


def test_quarantine_keys_are_uppercase():
    assert all(key == key.upper() for key in QUARANTINE)


def test_no_quarantine_member_is_a_builtin():
    # This is what makes the zero-config compatibility guarantee true rather than
    # asserted: the quarantine split is provably a no-op on the default path.
    assert not (set(QUARANTINE) & set(ONTOLOGIES))


def test_builtin_registry_key_wins_over_ols_preferred_prefix(index):
    # OLS declares preferredPrefix "HP"; the registry, every cached row and every
    # profile use "HPO". Three names for one ontology is how a scoped search
    # silently returns nothing.
    assert "HPO" in index["ontologies"]
    assert "HP" not in index["ontologies"]
    assert index["ontologies"]["HPO"]["slug"] == "hp"


def test_canonical_key_applies_aliases_and_uppercases():
    assert registry.canonical_key("go") == "GO"
    assert registry.canonical_key("hp") == "HPO"
    assert registry.canonical_key(" chebi ") == "CHEBI"


def test_prefix_collision_is_broken_deterministically(index):
    # dpo and fbcv both declare preferredPrefix "FBcv". The larger keeps the key.
    assert index["ontologies"]["FBCV"]["slug"] == "fbcv"
    assert index["ontologies"]["DPO"]["slug"] == "dpo"
    assert any("collision" in note for note in index["review"])


# --- index build fidelity --------------------------------------------------


def test_index_carries_titles_from_config_block(index):
    # title is not a top-level OLS field; reading it from the wrong place yields
    # 286 entries titled None.
    assert index["ontologies"]["GO"]["title"] == "Gene Ontology"
    assert all(entry["title"] for entry in index["ontologies"].values())


def test_index_falls_back_to_slug_when_ols_omits_a_title(index):
    # 14 of 286 entries have no config.title.
    assert index["ontologies"]["GOLD"]["title"] == "gold"


def test_index_version_is_content_addressed(payload):
    first = registry.build_index(payload)["index_version"]
    second = registry.build_index(copy.deepcopy(payload))["index_version"]
    assert first == second

    mutated = copy.deepcopy(payload)
    mutated["_embedded"]["ontologies"][0]["numberOfTerms"] = 999_999
    assert registry.build_index(mutated)["index_version"] != first


def test_protein_ontology_is_not_in_the_index(index):
    # OLS4 stopped serving PR. Recorded as a test so a future refresh that brings
    # it back is a visible, deliberate change rather than a silent one.
    assert "PR" not in index["ontologies"]


# --- the sanity floor: refusing a degraded response ------------------------


def test_validate_accepts_the_real_response(payload):
    _, problems = registry.validate_index(payload)
    assert problems == []


def test_refuses_a_truncated_response(payload):
    truncated = copy.deepcopy(payload)
    truncated["_embedded"]["ontologies"] = truncated["_embedded"]["ontologies"][:50]
    _, problems = registry.validate_index(truncated)
    assert any("floor" in p for p in problems)
    assert any("totalElements" in p for p in problems)


def test_refuses_a_response_missing_an_anchor(payload):
    missing = copy.deepcopy(payload)
    missing["_embedded"]["ontologies"] = [
        e for e in missing["_embedded"]["ontologies"] if e["ontologyId"] != "go"
    ]
    missing["page"]["totalElements"] -= 1
    _, problems = registry.validate_index(missing)
    assert any("anchor GO" in p for p in problems)


def test_refuses_an_unenumerated_removal(payload, index):
    dropped = copy.deepcopy(payload)
    dropped["_embedded"]["ontologies"] = [
        e for e in dropped["_embedded"]["ontologies"] if e["ontologyId"] != "foodon"
    ]
    dropped["page"]["totalElements"] -= 1

    _, problems = registry.validate_index(dropped, previous=index)
    assert any("FOODON" in p for p in problems)

    _, allowed = registry.validate_index(dropped, previous=index, allow_removals=("FOODON",))
    assert allowed == []


def test_refuses_a_changed_anchor_base_uri(payload, index):
    moved = copy.deepcopy(payload)
    for entry in moved["_embedded"]["ontologies"]:
        if entry["ontologyId"] == "efo":
            entry["baseUris"] = ["http://example.org/EFO_"]
    _, problems = registry.validate_index(moved, previous=index)
    assert any("base_uri changed" in p for p in problems)


# --- resolution ------------------------------------------------------------


def _profile(members, quarantine=None, force=None):
    return {
        "name": "test",
        "force_default": force or [],
        "_derived": {
            "index_version": "test",
            "members": {m: {"slug": m.lower()} for m in members},
            "quarantine": quarantine or {},
        },
    }


def test_resolve_splits_quarantined_members_out_of_default_scope():
    resolved = registry.resolve(_profile(["GO", "NCBITAXON"], quarantine={"NCBITAXON": "floods"}))
    assert resolved.default_scope == ("GO",)
    assert resolved.named_only == ("NCBITAXON",)
    assert resolved.reasons == {"NCBITAXON": "floods"}


def test_force_default_puts_a_quarantined_member_back():
    resolved = registry.resolve(
        _profile(["GO", "NCBITAXON"], quarantine={"NCBITAXON": "floods"}, force=["NCBITAXON"])
    )
    assert set(resolved.default_scope) == {"GO", "NCBITAXON"}
    assert resolved.named_only == ()


def test_all_members_covers_both_partitions():
    resolved = registry.resolve(_profile(["GO", "NCBITAXON"], quarantine={"NCBITAXON": "floods"}))
    # bulk_annotate resolves against this: a species column must not silently fail.
    assert set(resolved.all_members) == {"GO", "NCBITAXON"}


def test_resolve_is_pure_and_deterministic():
    profile = _profile(["GO", "CHEBI"])
    assert registry.resolve(profile) == registry.resolve(profile)


# --- env lock resolves verbatim (BL-08) ------------------------------------


def test_env_lock_resolves_verbatim_without_a_quarantine_split(monkeypatch):
    # An admin naming an ontology is the most explicit signal in the system.
    # Applying a quarantine heuristic on top of it is the design second-guessing
    # the one actor it has no business second-guessing.
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "NCBITAXON,GO")
    resolved = registry.current()
    assert set(resolved.default_scope) == {"NCBITAXON", "GO"}
    assert resolved.named_only == ()
    assert resolved.locked is True


def test_zero_config_resolves_the_builtin_registry(monkeypatch):
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    resolved = registry.current()
    assert set(resolved.default_scope) == set(ONTOLOGIES)
    assert resolved.named_only == ()  # no built-in is quarantined


# --- slug lookup: raises rather than fabricating ---------------------------


def test_slug_for_resolves_any_indexed_ontology():
    assert registry.slug_for("GO") == "go"
    assert registry.slug_for("HPO") == "hp"
    assert registry.slug_for("PO") == "po"  # not a built-in — resolution is unrestricted


def test_slug_for_raises_on_an_unknown_code():
    # The dict this replaced fell back to code.lower(), which OLS answered 200
    # with zero documents — indistinguishable from "no such term".
    with pytest.raises(KeyError):
        registry.slug_for("MOONDO")


# --- A2: the load-time membership check ------------------------------------


def test_missing_members_are_named_not_silently_dropped(index):
    members, warnings = registry._members_from_index(["GO", "MOONDO"], index)
    assert set(members) == {"GO"}
    assert any("MOONDO" in w for w in warnings)


def test_members_from_index_carries_the_real_minting_prefix(index):
    # FBBT (our key) mints CURIEs as "FBbt" (OLS's own casing) — rung 3 of
    # iri._generic falls back to the registry key whenever curie_prefix is None,
    # so this must not be discarded even though the index already has it.
    members, warnings = registry._members_from_index(["FBBT"], index)
    assert members["FBBT"]["curie_prefix"] == "FBbt"
    assert warnings == []


# --- index caching on (path, mtime, size) ----------------------------------


def test_index_is_cached_and_reread_when_the_file_changes(tmp_path, index):
    path = tmp_path / "idx.json"
    path.write_text(json.dumps(index))
    first = registry.load_index(path)
    assert registry.load_index(path) is first  # same stat -> cached object

    changed = copy.deepcopy(index)
    changed["index_version"] = "changed"
    path.write_text(json.dumps(changed))
    assert registry.load_index(path)["index_version"] == "changed"


def test_missing_index_raises_with_the_fix_command(tmp_path):
    with pytest.raises(registry.IndexError_, match="refresh_index"):
        registry.load_index(tmp_path / "absent.json")
