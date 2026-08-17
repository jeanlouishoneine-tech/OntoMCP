"""Starters, policy and the escape hatch — the Phase-D gate.

Every assertion maps to a gate clause in the scaling plan §6 Phase D.
"""

import json
import re

import httpx
import pytest

from ontomcp.core import cache, config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.config import QUARANTINE
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import escape_hatch, get_profile_setup_guidance, search_terms

STARTERS = profile_mod.starters()


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    monkeypatch.delenv("ONTOMCP_ESCAPE_HATCH", raising=False)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cache.db"
    cache.init_db(path)
    return path


def _client(handler):
    return OLSClient(
        client=httpx.AsyncClient(
            base_url=config.OLS_BASE_URL, transport=httpx.MockTransport(handler)
        )
    )


def _docs(*rows, facets=None):
    body = {"response": {"docs": list(rows)}}
    if facets is not None:
        flat = []
        for prefix, count in facets.items():
            flat.extend([prefix, count])
        body["facet_counts"] = {"facet_fields": {"ontologyPreferredPrefix": flat}}
    return httpx.Response(200, json=body)


def _doc(obo_id, label):
    return {"obo_id": obo_id, "label": label, "ontology_prefix": obo_id.split(":")[0]}


# --- D1: the seven starters ------------------------------------------------


def test_seven_starters_ship():
    assert len(STARTERS) == 7
    assert set(STARTERS) == {
        "pharma",
        "biotech-molecular",
        "plant-breeding",
        "fermentation",
        "clinical-rare-disease",
        "microbiome",
        "omics",
    }


@pytest.mark.parametrize("name", STARTERS)
def test_every_starter_member_is_in_the_index(name):
    """Gate: every starter resolves against the index."""
    entries = registry.load_index()["ontologies"]
    profile = profile_mod.load(name)
    missing = [c for c in profile["ontologies"] if registry.canonical_key(c) not in entries]
    assert not missing, f"{name} names {missing}, which OLS4 does not serve"


@pytest.mark.parametrize("name", STARTERS)
def test_no_starter_forces_a_non_quarantined_ontology(name):
    """Gate: no starter carries force_default for a non-QUARANTINE ontology.

    A force_default on an ontology that was never held back is a calibration
    failure dressed as a shipped workaround.
    """
    profile = profile_mod.load(name)
    for code in profile.get("force_default") or []:
        assert registry.canonical_key(code) in QUARANTINE


@pytest.mark.parametrize("name", STARTERS)
def test_every_starter_default_scope_is_at_most_eight(name):
    """Gate: every starter's default scope is <= 8."""
    resolved = registry.resolve(profile_mod.load(name))
    assert len(resolved.default_scope) <= config.DEFAULT_SCOPE_WARN


@pytest.mark.parametrize("name", STARTERS)
def test_every_starter_is_within_the_hard_cap(name):
    assert len(profile_mod.load(name)["ontologies"]) <= config.PROFILE_MAX


def test_protein_ontology_is_in_no_starter():
    """OLS4 stopped serving PR; two starters used to name it."""
    for name in STARTERS:
        assert "PR" not in profile_mod.load(name)["ontologies"]


def test_omics_is_the_zero_risk_compatibility_fixture():
    """omics is today's built-in default minus the disease-thesaurus tail."""
    members = set(profile_mod.load("omics")["ontologies"])
    assert members < set(config.ONTOLOGIES)
    assert not (members & {"NCIT", "DOID", "MESH"})


def test_clinical_rare_disease_states_the_reproducibility_limit():
    """The regulated case is a shipped starter, so the warning rides at the point of use."""
    description = profile_mod.load("clinical-rare-disease")["description"]
    assert "not what they said" in description
    assert "record the CURIEs" in description


def test_a_starter_resolves_without_any_setup(monkeypatch):
    """Naming a starter must work with no profile file on disk at all."""
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")
    resolved = registry.current()
    assert set(resolved.default_scope) == {"PO", "TO", "PECO", "GO", "ENVO", "UO"}
    assert resolved.named_only == ("NCBITAXON",)


def test_saving_over_a_starter_name_shadows_it(monkeypatch):
    assert not profile_mod.profile_path("pharma").exists()
    assert profile_mod.load("pharma")["ontologies"]  # from the shipped starter


# --- D2: policy.md ---------------------------------------------------------


def test_policy_ships_and_is_returned_verbatim():
    guidance, _ = get_profile_setup_guidance()
    assert "proposer-policy" in guidance["policy"]
    assert (
        guidance["policy"]
        == profile_mod.Path(str(config.INDEX_PATH.parent / "policy.md")).read_text()
    )


# All-caps words the policy file uses as vocabulary rather than as prefixes.
# Kept explicit and tiny: anything NOT listed here must be a real ontology, so a
# new ontology name can never be added without the index agreeing it exists.
_POLICY_VOCABULARY = {"OLS", "CURIE", "CURIES", "IRI", "NNN", "NNNNNNN"}


def test_every_all_caps_token_in_policy_is_a_real_prefix():
    """Gate: the file cannot rot into naming ontologies OLS4 no longer serves."""
    entries = registry.load_index()["ontologies"]
    text = (config.INDEX_PATH.parent / "policy.md").read_text()
    # Strip comments: the failing-example lines quote user queries, not prefixes.
    body = "\n".join(line.split("#")[0] for line in text.splitlines())
    tokens = set(re.findall(r"\b[A-Z][A-Z0-9]{2,}\b", body)) - _POLICY_VOCABULARY
    unknown = {t for t in tokens if registry.canonical_key(t) not in entries}
    assert not unknown, f"policy.md names {unknown}, which are not in the index"
    assert tokens, "the token scan found nothing — the check would pass vacuously"


def test_policy_records_the_real_ordo_prefix():
    """S-8: OLS4 mints ORDO:NNN. The v4 draft had this backwards."""
    text = (config.INDEX_PATH.parent / "policy.md").read_text()
    assert 'CURIEs are "ORDO:NNN"' in text


def test_every_starter_prefix_appears_in_the_grounding_list():
    """Gate: every prefix in a starter or policy.md appears in the grounding list.

    A shipped starter's own member being unproposable by the model meant to
    propose it is exactly the failure the deleted `terms < 100` rule would cause.
    """
    guidance, _ = get_profile_setup_guidance()
    grounding = set(guidance["grounding_list"])
    for name in STARTERS:
        for code in profile_mod.load(name)["ontologies"]:
            assert registry.canonical_key(code) in grounding, f"{code} ({name}) is unproposable"


# --- D3: the escape hatch --------------------------------------------------


def test_stage_one_names_the_held_back_member_and_how_to_search_it():
    text = escape_hatch.stage_one("Oryza sativa", {"NCBITAXON": 12}, ["PO", "TO", "GO"])
    assert "No matches in your default scope (PO, TO, GO)" in text
    assert "NCBITAXON is in your profile but held back" in text
    assert "12 matches there" in text
    assert 'search_terms("Oryza sativa", ontologies=["NCBITAXON"])' in text


def test_stage_one_remediation_is_prose_not_a_shell_line():
    """In Claude Code a shell line in a tool result is an executable side effect."""
    text = escape_hatch.stage_one("x", {"NCBITAXON": 1}, ["GO"])
    assert not any(line.strip().startswith(("$", "ontomcp ", "uv ")) for line in text.splitlines())


def test_stage_two_ranks_by_exact_count_and_names_titles():
    index = registry.load_index()
    out = escape_hatch.stage_two("bile acid", {"FOODON": 40, "ENVO": 5, "GO": 100}, {"GO"}, index)
    names = [s["ontology"] for s in out["add_to_profile"]]
    assert names == ["FOODON", "ENVO"]  # GO is already a member; ranked by count
    assert out["add_to_profile"][0]["title"] == "Food Ontology"
    assert out["add_to_profile"][0]["matches"] == 40


def test_stage_two_caveats_that_counts_are_by_indexing_not_minting_ontology():
    """The facet counts here are per-INDEXING ontology; search_terms filters by
    minting ontology. Adding a suggested ontology is not guaranteed to surface
    the counted hits, so every record must say so rather than promise a fix.
    """
    index = registry.load_index()
    out = escape_hatch.stage_two("bile acid", {"FOODON": 40}, {"GO"}, index)
    note = out["add_to_profile"][0]["matches_note"]
    assert "INDEXES" in note and "MINTED" in note


def test_stage_two_names_a_quarantined_ontology_but_never_recommends_it():
    """Gate: named, with its direct-search line — suppressed only from the add list.

    v3 suppressed it from all output, which recreated the dead end the escape hatch
    exists to close, for exactly the users most likely to hit it.
    """
    index = registry.load_index()
    out = escape_hatch.stage_two("Oryza sativa", {"NCBITAXON": 900}, set(), index)

    assert "add_to_profile" not in out
    named = out["named_but_not_recommended"]
    assert named[0]["ontology"] == "NCBITAXON"
    assert named[0]["matches"] == 900
    assert "genus/species" in named[0]["reason"]
    assert 'ontologies=["NCBITAXON"]' in named[0]["search_directly"]


def test_stage_two_names_a_non_searchable_ontology_but_never_recommends_it():
    index = registry.load_index()
    out = escape_hatch.stage_two("part of", {"RO": 50}, set(), index)
    assert "add_to_profile" not in out
    assert out["named_but_not_recommended"][0]["ontology"] == "RO"


def test_stage_two_never_returns_terms():
    """It returns ontology names and counts. Terms would be a second search."""
    index = registry.load_index()
    out = escape_hatch.stage_two("x", {"FOODON": 3}, set(), index)
    blob = json.dumps(out)
    assert "curie" not in blob and "label" not in blob


async def test_stage_one_fires_before_stage_two(db, monkeypatch):
    """Gate: Stage 1 fires before Stage 2 and surfaces in-profile held-back members."""
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        onts = request.url.params.get("ontology", "")
        if "ncbitaxon" in onts:
            return _docs(_doc("NCBITaxon:4530", "Oryza sativa"))
        return _docs(facets={"NCBITAXON": 900})

    async with _client(handler) as cli:
        payload, _ = await search_terms("Oryza sativa", db_path=db, client=cli)

    assert payload["results"] == []
    notice = " ".join(payload["meta"]["notice"])
    assert "NCBITAXON is in your profile but held back" in notice
    assert "elsewhere" not in payload["meta"], "Stage 2 must not speak once Stage 1 has"


async def test_stage_one_does_not_claim_a_foreign_minted_row_is_in_the_profile(db, monkeypatch):
    """A row minted by an ontology that is not in the profile at all must never be
    reported as "in your profile but held back" — that notice's remediation call
    (``search_terms(..., ontologies=[prefix])``) would be rejected out_of_profile.
    """
    monkeypatch.setenv("ONTOMCP_PROFILE", "plant-breeding")

    def handler(request):
        onts = request.url.params.get("ontology", "")
        if "ncbitaxon" in onts:
            return _docs(
                _doc("NCBITaxon:4530", "Oryza sativa"),
                _doc("FOODON:1", "Oryza sativa food product"),  # not in this profile at all
            )
        return _docs(facets={"NCBITAXON": 900})

    async with _client(handler) as cli:
        payload, _ = await search_terms("Oryza sativa", db_path=db, client=cli)

    notice = " ".join(payload["meta"]["notice"])
    assert "NCBITAXON is in your profile but held back" in notice
    assert "FOODON" not in notice


async def test_stage_two_fires_when_nothing_is_held_back(db, monkeypatch):
    monkeypatch.setenv("ONTOMCP_PROFILE", "pharma")  # no quarantined members

    def handler(request):
        if request.url.params.get("rows") == "0":
            return _docs(facets={"FOODON": 40, "CHEBI": 0})
        return _docs()

    async with _client(handler) as cli:
        payload, _ = await search_terms("tempeh", db_path=db, client=cli)

    assert payload["meta"]["elsewhere"]["add_to_profile"][0]["ontology"] == "FOODON"


async def test_the_escape_hatch_never_fires_on_an_error(db, monkeypatch):
    """Only on zero results in scope. An OLS failure is not evidence about scope."""
    monkeypatch.setenv("ONTOMCP_PROFILE", "pharma")

    async with _client(lambda r: httpx.Response(500)) as cli:
        results, _ = await search_terms("x", db_path=db, client=cli)
    assert results[0]["error"] == "search_failed"


async def test_the_escape_hatch_can_be_disabled(db, monkeypatch):
    monkeypatch.setenv("ONTOMCP_PROFILE", "pharma")
    monkeypatch.setenv("ONTOMCP_ESCAPE_HATCH", "0")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs()

    async with _client(handler) as cli:
        payload, _ = await search_terms("x", db_path=db, client=cli)

    assert "elsewhere" not in payload["meta"]
    assert calls["n"] == 1, "disabled means no extra call, not a suppressed one"


async def test_the_escape_hatch_stays_quiet_when_the_user_named_a_scope(db, monkeypatch):
    """If they asked for CHEBI specifically, 'try FOODON' is not an answer."""
    monkeypatch.setenv("ONTOMCP_PROFILE", "pharma")

    async with _client(lambda r: _docs()) as cli:
        payload, _ = await search_terms("x", ontologies=["CHEBI"], db_path=db, client=cli)

    assert "elsewhere" not in payload["meta"]


# --- integration: the starters against LIVE OLS4 ---------------------------


@pytest.mark.integration
@pytest.mark.parametrize("name", STARTERS)
async def test_every_starter_member_is_live_on_ols4(name):
    """D1: validated against the live index, not only the committed one.

    The committed index is a snapshot. This is what catches OLS4 withdrawing an
    ontology between refreshes — the failure mode that made PR silently unusable.
    """
    profile = profile_mod.load(name)
    async with OLSClient() as cli:
        for code in profile["ontologies"]:
            slug = registry.slug_for(code)
            response = await cli._get(f"/ontologies/{slug}")
            assert response.status_code == 200, f"{name}: OLS4 does not serve {code} ({slug})"


def test_an_over_cap_suggestion_is_dropped_not_mislabelled():
    """A ranked list that runs out of room has nothing to explain.

    Overflowing into the "named but not recommended" list attached a
    property-dominant reason to MESH and CHEBI, which are neither.
    """
    index = registry.load_index()
    counts = {"NCIT": 117, "DRON": 55, "SNOMED": 36, "MESH": 18, "CHEBI": 13}
    out = escape_hatch.stage_two("trastuzumab", counts, set(), index)

    assert [s["ontology"] for s in out["add_to_profile"]] == ["NCIT", "DRON", "SNOMED"]
    assert "named_but_not_recommended" not in out, "searchable ontologies must not be maligned"


def test_a_genuinely_non_recommendable_ontology_still_appears_alongside_a_full_list():
    index = registry.load_index()
    counts = {"NCIT": 117, "DRON": 55, "SNOMED": 36, "NCBITAXON": 900}
    out = escape_hatch.stage_two("x", counts, set(), index)
    assert len(out["add_to_profile"]) == 3
    assert out["named_but_not_recommended"][0]["ontology"] == "NCBITAXON"
