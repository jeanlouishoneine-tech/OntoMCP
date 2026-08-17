"""Unit tests for the 10 core tools. OLS is mocked — no network (CLAUDE.md).

Each test injects a tmp_db_path and a MockTransport-backed OLSClient so the
cache-first / OLS-fallback / error-propagation paths and the payload caps are all
exercised offline. Integration tests at the bottom hit live OLS and are skipped
unless ``-m integration`` is selected.
"""

from pathlib import Path

import httpx
import pytest

from ontomcp.core import cache, config
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import (
    bulk_annotate,
    find_synonyms,
    get_ancestors,
    get_children,
    get_descendants,
    get_parents,
    get_profile_setup_guidance,
    get_term,
    get_term_graph,
    map_across_ontologies,
    search_terms,
    validate_term,
)


def _client(handler) -> OLSClient:
    """OLSClient wired to a MockTransport handler (no network)."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=config.OLS_BASE_URL,
        transport=transport,
        headers={"User-Agent": config.USER_AGENT},
    )
    return OLSClient(client=http)


@pytest.fixture
def db(tmp_db_path):
    cache.init_db(tmp_db_path)
    return tmp_db_path


# OLS response builders ------------------------------------------------------


def _term_doc(obo_id="GO:0008219", label="cell death", desc="a process", obsolete=False):
    return {
        "obo_id": obo_id,
        "label": label,
        "description": [desc],
        "is_obsolete": obsolete,
    }


def _search_response(docs):
    return httpx.Response(200, json={"response": {"docs": docs}})


def _terms_response(terms):
    return httpx.Response(200, json={"_embedded": {"terms": terms}})


def _hier_response(curies):
    terms = [{"obo_id": c, "label": c} for c in curies]
    return _terms_response(terms)


# --- search_terms ----------------------------------------------------------


async def test_search_terms_ols_fallback_and_caches(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _search_response([_term_doc(desc="end of cell life")])

    async with _client(handler) as cli:
        payload, cached = await search_terms("cell death", db_path=db, client=cli)
    assert payload["results"][0]["curie"] == "GO:0008219"
    assert cached is False  # served from OLS, not cache
    assert payload["meta"]["source"] == "ols"
    assert calls["n"] == 1

    # Second call must hit FTS cache, not OLS — and only because the ledger records
    # that OLS answered this exact (query, scope). "FTS returned a row" is not proof.
    async with _client(handler) as cli:
        again, cached = await search_terms("cell death", db_path=db, client=cli)
    assert again["results"][0]["curie"] == "GO:0008219"
    assert cached is True
    assert again["meta"]["source"] == "cache"
    assert calls["n"] == 1  # no new OLS call

    # rank is the ordinal position, identical regardless of which path served it.
    assert payload["results"][0]["rank"] == again["results"][0]["rank"] == 1


async def test_search_terms_rejects_an_unknown_ontology_instead_of_dropping_it(db):
    """F3: a rejected code never widens scope, and never silently narrows it either.

    Dropping "bogus" and searching only GO answered a question the user did not
    ask. The structured error is what lets the model offer a one-step fix.
    """

    def handler(request):
        raise AssertionError("a rejected scope must not reach the network")

    async with _client(handler) as cli:
        results, hit = await search_terms("x", ontologies=["go", "bogus"], db_path=db, client=cli)
    assert results[0]["error"] == "unknown_ontology"
    assert results[0]["ontology"] == "BOGUS"
    assert hit is False


async def test_search_terms_distinguishes_a_real_ontology_from_a_typo(db):
    """FOODON is real but out of profile; MOONDO is a mistake. Different errors."""

    def handler(request):
        raise AssertionError("a rejected scope must not reach the network")

    async with _client(handler) as cli:
        out_of_profile, _ = await search_terms("x", ontologies=["FOODON"], db_path=db, client=cli)
        unknown, _ = await search_terms("x", ontologies=["MOONDO"], db_path=db, client=cli)

    assert out_of_profile[0]["error"] == "out_of_profile"
    assert out_of_profile[0]["title"] == "Food Ontology"
    assert "add FOODON" in out_of_profile[0]["remediation"]

    assert unknown[0]["error"] == "unknown_ontology"
    assert "MONDO" in unknown[0]["did_you_mean"]


async def test_search_terms_aliases_hp_to_hpo_before_the_membership_test(db):
    """A12: aliases apply first, so ["HP"] resolves rather than being rejected."""
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return _search_response([_term_doc()])

    async with _client(handler) as cli:
        await search_terms("x", ontologies=["HP"], db_path=db, client=cli)
    assert seen["params"].get("ontology") == "hp"


async def test_unfiltered_search_is_scoped_to_the_resolved_default(db):
    """F2: an unfiltered search used to hit all of OLS4, not "all of my ontologies"."""
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return _search_response([_term_doc()])

    async with _client(handler) as cli:
        await search_terms("x", db_path=db, client=cli)
    slugs = set(seen["params"]["ontology"].split(","))
    assert "go" in slugs and "mondo" in slugs
    assert len(slugs) == len(config.ONTOLOGIES)


def _empty_broken_registry():
    """A ``registry.current()`` result shaped like a genuinely unresolvable
    registry (unreadable index) — post-fix, ``current()`` never raises, it
    degrades to this instead."""
    from ontomcp.core.registry import ResolvedRegistry

    return ResolvedRegistry(
        profile="default",
        default_scope=(),
        named_only=(),
        members={},
        index_version="",
        warnings=("Ontology index not found. Run: python scripts/refresh_index.py",),
    )


async def test_unfiltered_search_refuses_rather_than_returning_a_false_empty(db, monkeypatch):
    """An unresolvable registry must not widen to all of OLS4 and then filter to
    nothing — that is a real failure dressed up as a zero-hit success."""
    from ontomcp.core import registry

    monkeypatch.setattr(registry, "current", _empty_broken_registry)

    called = False

    def handler(request):
        nonlocal called
        called = True
        return _search_response([_term_doc()])

    async with _client(handler) as cli:
        result, cache_hit = await search_terms("x", db_path=db, client=cli)
    assert result["error"] == "registry_unavailable"
    assert cache_hit is False
    assert not called  # never even reaches OLS


async def test_explicit_scope_search_still_works_when_the_registry_is_unresolvable(db, monkeypatch):
    """An explicitly named ontology must still search fail-open, unlike the
    unfiltered case above — the user told us exactly what they want."""
    from ontomcp.core import registry

    monkeypatch.setattr(registry, "current", _empty_broken_registry)

    def handler(request):
        return _search_response([_term_doc(obo_id="GO:0008219")])

    async with _client(handler) as cli:
        result, _ = await search_terms("x", ontologies=["GO"], db_path=db, client=cli)
    assert "error" not in result
    assert any(r["curie"] == "GO:0008219" for r in result["results"])
    assert result["meta"]["notice"] == list(_empty_broken_registry().warnings)


# (helper) every tool now returns (result, cache_hit); tests unpack accordingly.


async def test_search_terms_clamps_oversized_limit(db):
    seen = {}

    def handler(request):
        seen["rows"] = request.url.params.get("rows")
        return _search_response([_term_doc()])

    async with _client(handler) as cli:
        await search_terms("x", limit=100000, db_path=db, client=cli)
    assert seen["rows"] == str(config.SEARCH_LIMIT_MAX)  # clamped, not 100000


async def test_search_terms_propagates_error(db):
    def handler(request):
        return httpx.Response(500)

    async with _client(handler) as cli:
        results, _ = await search_terms("x", db_path=db, client=cli)
    assert results[0]["error"] == "search_failed"  # errors stay a bare list


async def test_search_terms_rejects_an_empty_query_without_calling_ols(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _search_response([_term_doc()])

    async with _client(handler) as cli:
        result, cached = await search_terms("   ", db_path=db, client=cli)
    assert result["error"] == "bad_query"
    assert cached is False
    assert calls["n"] == 0  # never reached OLS


async def test_search_terms_multi_row_cache_hit_preserves_ols_order(db):
    """A covered cache hit must return the same #1 result OLS originally gave.

    Local bm25 and OLS relevance are different algorithms; the docs are chosen so
    naive bm25 would rank them differently than OLS's own order below.
    """

    def handler(request):
        return _search_response(
            [
                _term_doc(obo_id="GO:3", label="third", desc="cell death regulation"),
                _term_doc(obo_id="GO:1", label="first", desc="cell death"),
                _term_doc(obo_id="GO:2", label="second", desc="the process of cell death"),
            ]
        )

    async with _client(handler) as cli:
        fresh, _ = await search_terms("cell death", db_path=db, client=cli)
    assert [r["curie"] for r in fresh["results"]] == ["GO:3", "GO:1", "GO:2"]

    async with _client(handler) as cli:
        hit, cached = await search_terms("cell death", db_path=db, client=cli)
    assert cached is True
    assert [r["curie"] for r in hit["results"]] == ["GO:3", "GO:1", "GO:2"]


async def test_search_terms_flags_obsolete_hits(db):
    def handler(request):
        live = _term_doc(label="cell death")
        live["is_obsolete"] = False
        dead = _term_doc(obo_id="GO:0006917", label="induction of apoptosis")
        dead["is_obsolete"] = True
        return _search_response([live, dead])

    async with _client(handler) as cli:
        payload, _ = await search_terms("apoptosis", db_path=db, client=cli)
    flags = {r["curie"]: r["is_obsolete"] for r in payload["results"]}
    assert flags["GO:0008219"] is False
    assert flags["GO:0006917"] is True


# --- get_term / find_synonyms ----------------------------------------------


async def test_get_term_caches_then_serves_from_cache(db):
    # Count only term-record fetches; the one-time ontology-version lookup
    # (/ontologies/{slug}) is separate and cached independently.
    calls = {"n": 0}

    def handler(request):
        if str(request.url).rstrip("/").endswith("/ontologies/go"):
            return httpx.Response(200, json={"version": "2026-05-19"})
        calls["n"] += 1
        doc = _term_doc()
        doc["obo_synonym"] = [{"type": "hasExactSynonym", "name": "cell killing"}]
        return _terms_response([doc])

    async with _client(handler) as cli:
        term, cached = await get_term("GO:0008219", db_path=db, client=cli)
    assert term["label"] == "cell death"
    assert term["synonyms"]["exact"] == ["cell killing"]
    assert term["ontology_version"] == "2026-05-19"
    assert cached is False  # first fetch from OLS

    async with _client(handler) as cli:
        again, cached = await get_term("GO:0008219", db_path=db, client=cli)
    assert cached is True  # fresh cache hit
    assert again["ontology_version"] == "2026-05-19"  # version served from cache
    assert calls["n"] == 1  # fresh cache hit, no second term fetch


async def test_get_term_not_found(db):
    def handler(request):
        return httpx.Response(404)

    async with _client(handler) as cli:
        term, _ = await get_term("GO:9999999", db_path=db, client=cli)
    assert term["error"] == "not_found"


async def test_get_term_bad_curie_returns_error(db):
    async with _client(lambda r: httpx.Response(200)) as cli:
        term, _ = await get_term("not-a-curie", db_path=db, client=cli)
    assert term["error"] == "bad_curie"


async def test_find_synonyms_returns_buckets(db):
    def handler(request):
        doc = _term_doc()
        doc["obo_synonym"] = [{"type": "hasRelatedSynonym", "name": "apoptosis"}]
        return _terms_response([doc])

    async with _client(handler) as cli:
        syns, _ = await find_synonyms("GO:0008219", db_path=db, client=cli)
    assert syns["related"] == ["apoptosis"]
    assert syns["narrow"] == []


async def test_find_synonyms_propagates_error(db):
    """A not_found term must surface as an error dict, not raise on ["synonyms"]."""
    async with _client(lambda r: httpx.Response(404)) as cli:
        result, cached = await find_synonyms("GO:9999999", db_path=db, client=cli)
    assert result["error"] == "not_found"
    assert cached is False


async def test_get_term_warns_on_do_not_annotate_subset(db):
    def handler(request):
        doc = _term_doc()
        doc["in_subset"] = ["gocheck_do_not_annotate"]
        return _terms_response([doc])

    async with _client(handler) as cli:
        term, _ = await get_term("GO:0008219", db_path=db, client=cli)
    assert term["subsets"] == ["gocheck_do_not_annotate"]
    assert any("do_not_annotate" in w or "annotation" in w for w in term["warnings"])


async def test_get_term_warns_on_obsolete_with_consider(db):
    def handler(request):
        doc = _term_doc(obo_id="GO:0000002", label="obsolete term", obsolete=True)
        doc["annotation"] = {"consider": ["GO:0006915"]}
        return _terms_response([doc])

    async with _client(handler) as cli:
        term, _ = await get_term("GO:0000002", db_path=db, client=cli)
    assert term["consider"] == ["GO:0006915"]
    assert any("obsolete" in w.lower() for w in term["warnings"])
    assert any("GO:0006915" in w for w in term["warnings"])


async def test_get_term_no_warnings_for_clean_term(db):
    async with _client(lambda r: _terms_response([_term_doc()])) as cli:
        term, _ = await get_term("GO:0008219", db_path=db, client=cli)
    assert term["warnings"] == []


# --- validate_term (never cached) ------------------------------------------


async def test_validate_term_always_hits_ols(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _terms_response([_term_doc(obsolete=True)])

    async with _client(handler) as cli:
        first, cached = await validate_term("GO:0008219", client=cli)
        await validate_term("GO:0008219", client=cli)

    assert first["is_obsolete"] is True
    assert first["is_current"] is False
    assert "checked_at" in first
    assert first["consider"] == []  # present even when empty
    assert cached is False  # validate_term is never cached
    assert calls["n"] == 2  # never cached — OLS called every time


async def test_validate_term_returns_consider_when_no_replacement(db):
    def handler(request):
        doc = _term_doc(obsolete=True)
        doc["annotation"] = {"consider": ["GO:0006915", "GO:0012501"]}
        return _terms_response([doc])

    async with _client(handler) as cli:
        result, _ = await validate_term("GO:0000002", client=cli)
    assert result["is_current"] is False
    assert result["replaced_by"] is None
    assert result["consider"] == ["GO:0006915", "GO:0012501"]


# --- hierarchy -------------------------------------------------------------


async def test_get_parents_stores_direct_edges(db):
    # /parents returns the DIRECT parents; these are true is_a edges and ARE
    # written to the relationships table.
    def handler(request):
        assert str(request.url).endswith("/parents")
        return _hier_response(["GO:0012501"])

    async with _client(handler) as cli:
        parents, _ = await get_parents("GO:0006915", db_path=db, client=cli)
    assert [p["curie"] for p in parents] == ["GO:0012501"]
    assert parents[0]["depth"] == 1
    assert parents[0]["rel_type"] == "is_a"
    cached = cache.get_ancestors_cached(db, "GO:0006915")
    assert {c["curie"] for c in cached} == {"GO:0012501"}


async def test_get_children_caps_at_50_and_stores_direct_edges(db):
    many = [f"GO:{i:07d}" for i in range(60)]

    def handler(request):
        assert str(request.url).endswith("/children")
        return _hier_response(many)

    async with _client(handler) as cli:
        children, _ = await get_children("GO:0008150", db_path=db, client=cli)
    assert len(children) == config.DESCENDANTS_CAP  # 50
    assert children[0]["depth"] == 1

    # The cap must also bound what gets persisted, not just the returned payload.
    conn = cache._connect(db)
    try:
        n_rels = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        conn.close()
    assert n_rels == config.DESCENDANTS_CAP


async def test_get_ancestors_is_transitive_and_writes_no_edges(db):
    # /ancestors returns the full transitive closure. It must NOT be recorded as
    # direct edges (that would fabricate the topology) and depth is "transitive".
    def handler(request):
        assert str(request.url).endswith("/ancestors")
        return _hier_response(["GO:0012501", "GO:0008219", "GO:0008150"])

    async with _client(handler) as cli:
        ancestors, _ = await get_ancestors("GO:0006915", db_path=db, client=cli)
    assert {a["curie"] for a in ancestors} == {"GO:0012501", "GO:0008219", "GO:0008150"}
    assert all(a["depth"] == "transitive" for a in ancestors)
    assert all(a["rel_type"] == "ancestor" for a in ancestors)

    conn = cache._connect(db)
    try:
        n_rels = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        conn.close()
    assert n_rels == 0  # transitive results never become direct edges


async def test_get_descendants_caps_at_50_and_writes_no_edges(db):
    many = [f"GO:{i:07d}" for i in range(60)]

    def handler(request):
        assert str(request.url).endswith("/hierarchicalDescendants")
        return _hier_response(many)

    async with _client(handler) as cli:
        desc, _ = await get_descendants("GO:0008150", db_path=db, client=cli)
    assert len(desc) == config.DESCENDANTS_CAP  # 50
    assert all(d["depth"] == "transitive" for d in desc)

    conn = cache._connect(db)
    try:
        n_rels = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        conn.close()
    assert n_rels == 0  # transitive results never become direct edges


# --- get_profile_setup_guidance (pure logic, zero arguments) ---------------


def test_guidance_takes_no_arguments():
    # The objective's one prohibited behaviour is inferring ontologies from a
    # research description. No code path may accept one.
    import inspect

    assert inspect.signature(get_profile_setup_guidance).parameters == {}


def test_guidance_flags_policy_unavailable_instead_of_hiding_it(monkeypatch):
    """An unreadable policy.md must not look like an intentionally short/empty
    policy — the payload must say so, not just return "" silently."""
    from ontomcp.core.tools import suggest as suggest_module

    monkeypatch.setattr(suggest_module, "POLICY_PATH", Path("/nowhere/policy.md"))

    result, _ = get_profile_setup_guidance()
    assert result["policy"] == ""
    assert result["policy_unavailable"] is True


def test_guidance_returns_policy_rules_and_grounding():
    out, cached = get_profile_setup_guidance()
    assert cached is False  # pure logic, no cache
    assert set(out) >= {"policy", "grounding_list", "rules", "next_step", "index_version"}
    assert out["rules"]["max_ontologies"] == 12


def test_guidance_grounding_list_carries_titles_not_just_prefixes():
    # An existence check proves "PO" is a real prefix, not that the user meant
    # Plant Ontology rather than Population and Community Ontology.
    out, _ = get_profile_setup_guidance()
    assert out["grounding_list"]["GO"] == "Gene Ontology"
    assert all(isinstance(v, str) and v for v in out["grounding_list"].values())


def test_guidance_omits_non_searchable_ontologies():
    # RO is property-dominant (58 terms, 829 properties), so proposing it would
    # waste a profile slot on an ontology that returns almost nothing.
    out, _ = get_profile_setup_guidance()
    assert "RO" not in out["grounding_list"]
    assert "GO" in out["grounding_list"]


def test_guidance_names_quarantined_ontologies_with_reasons():
    out, _ = get_profile_setup_guidance()
    assert set(out["rules"]["quarantined"]) == {"NCBITAXON", "PATO"}
    assert all(reason for reason in out["rules"]["quarantined"].values())


def test_keyword_matcher_is_gone():
    from ontomcp.core.tools import suggest

    assert not hasattr(suggest, "_KEYWORD_RULES")
    assert not hasattr(suggest, "_EXAMPLE_TERMS")


# --- map_across_ontologies -------------------------------------------------


async def test_map_exact_xref(db):
    def handler(request):
        doc = _term_doc(obo_id="MONDO:0004992", label="cancer")
        doc["obo_xref"] = [{"database": "MESH", "id": "D009369"}]
        return _terms_response([doc])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("MONDO:0004992", "MESH", db_path=db, client=cli)
    assert out[0]["curie"] == "MESH:D009369"
    assert out[0]["match_type"] == "exact_xref"
    assert out[0]["mapping_predicate"] == "skos:exactMatch"
    assert out[0]["confidence"] == 1.0


async def test_map_xref_id_already_prefixed(db):
    # OLS sometimes returns the xref id already prefixed; must not double-prefix.
    def handler(request):
        doc = _term_doc(obo_id="MONDO:0004992", label="cancer")
        doc["obo_xref"] = [{"database": "MESH", "id": "MESH:D009369"}]
        return _terms_response([doc])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("MONDO:0004992", "MESH", db_path=db, client=cli)
    assert out[0]["curie"] == "MESH:D009369"
    assert out[0]["match_type"] == "exact_xref"


async def test_map_xref_deduped_across_forms(db):
    # Same xref listed in both structured and flat forms yields a single result.
    def handler(request):
        doc = _term_doc(obo_id="MONDO:0004992", label="cancer")
        doc["obo_xref"] = [{"database": "MESH", "id": "D009369"}]
        doc["oboXref"] = ["MESH:D009369"]
        return _terms_response([doc])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("MONDO:0004992", "MESH", db_path=db, client=cli)
    assert [r["curie"] for r in out] == ["MESH:D009369"]


async def test_map_label_match_fallback(db):
    def handler(request):
        if "/search" in str(request.url):
            return _search_response([_term_doc(obo_id="MESH:D003643", label="cell death")])
        return _terms_response([_term_doc(obo_id="GO:0008219", label="cell death")])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("GO:0008219", "MESH", db_path=db, client=cli)
    assert out[0]["curie"] == "MESH:D003643"
    assert out[0]["match_type"] == "label_match"
    assert out[0]["mapping_predicate"] == "heuristic_label"
    # A heuristic match must never reach or tie a curated xref (confidence 1.0).
    assert out[0]["confidence"] < 1.0
    assert out[0]["confidence"] <= 0.7


async def test_map_label_match_confidence_below_xref(db):
    # A near-but-not-identical label clears the threshold yet stays well under an
    # xref's 1.0 — string similarity is reported as a candidate, not equivalence.
    def handler(request):
        if "/search" in str(request.url):
            return _search_response([_term_doc(obo_id="MONDO:0000001", label="lung cancers")])
        return _terms_response([_term_doc(obo_id="GO:0000001", label="lung cancer")])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("GO:0000001", "MONDO", db_path=db, client=cli)
    assert out  # cleared the 0.85 threshold
    assert out[0]["match_type"] == "label_match"
    assert 0.0 < out[0]["confidence"] < 0.7  # below a perfect/corroborated match


async def test_map_orphanet_xref_resolves_to_ordo(db, monkeypatch):
    # OLS's own xref annotations spell the ORDO cross-reference "Orphanet:NNN",
    # not "ORDO:NNN" (the CURIE prefix ORDO's own obo_id mints with). Without the
    # alias, a curated exact match here is silently dropped. ORDO is not one of
    # the ten built-in default ontologies, so lock scope to include it.
    monkeypatch.setenv("ONTOMCP_ONTOLOGIES", "ORDO,MONDO")

    def handler(request):
        doc = _term_doc(obo_id="MONDO:0007739", label="Huntington disease")
        doc["annotation"] = {"database_cross_reference": ["Orphanet:399"]}
        return _terms_response([doc])

    async with _client(handler) as cli:
        out, _ = await map_across_ontologies("MONDO:0007739", "ORDO", db_path=db, client=cli)
    assert out[0]["curie"] == "ORDO:399"
    assert out[0]["match_type"] == "exact_xref"


async def test_map_unknown_target(db):
    async with _client(lambda r: httpx.Response(200)) as cli:
        out, _ = await map_across_ontologies("GO:0008219", "BOGUS", db_path=db, client=cli)
    assert out[0]["error"] == "unknown_ontology"


async def test_map_propagates_source_not_found(db):
    """When the source term itself is missing, return its error rather than mapping."""
    async with _client(lambda r: httpx.Response(404)) as cli:
        out, cached = await map_across_ontologies("GO:9999999", "MONDO", db_path=db, client=cli)
    assert out[0]["error"] == "not_found"
    assert cached is False


# --- bulk_annotate ---------------------------------------------------------


async def test_bulk_annotate_matches(db):
    def handler(request):
        q = request.url.params.get("q", "")
        return _search_response([_term_doc(obo_id="CL:0000236", label=q)])

    async with _client(handler) as cli:
        out, _ = await bulk_annotate(["B cell", "T cell"], db_path=db, client=cli)
    assert len(out["results"]) == 2
    assert out["results"][0]["best_match"]["curie"] == "CL:0000236"


async def test_bulk_annotate_surfaces_a_search_failure_instead_of_no_match(db):
    """A failed OLS search must not read the same as a genuine zero-hit search."""
    async with _client(lambda r: httpx.Response(500)) as cli:
        out, _ = await bulk_annotate(["B cell"], db_path=db, client=cli)
    row = out["results"][0]
    assert row["best_match"] is None
    assert row["error"] == "search_failed"


async def test_bulk_annotate_rejected_ontology_hint_matches_search_terms_shape(db):
    # Same error taxonomy as search_terms's rejected-scope return (a list), not a
    # bare dict — the envelope shape must not diverge between the two tools.
    async with _client(lambda r: httpx.Response(200)) as cli:
        out, cached = await bulk_annotate(
            ["x"], ontology_hint="NOTAREALONTOLOGY", db_path=db, client=cli
        )
    assert isinstance(out, list)
    assert out[0]["error"] == "unknown_ontology"
    assert cached is False


async def test_bulk_annotate_dedupes_inputs(db):
    queries = []

    def handler(request):
        q = request.url.params.get("q", "")
        queries.append(q)
        return _search_response([_term_doc(obo_id="CL:0000236", label=q)])

    async with _client(handler) as cli:
        out, _ = await bulk_annotate(["T cell", "T cell", "B cell"], db_path=db, client=cli)

    # One result row per input, preserving order and duplicates.
    assert [r["input"] for r in out["results"]] == ["T cell", "T cell", "B cell"]
    # Distinct terms searched once each — "T cell" not queried twice.
    assert sorted(queries) == ["B cell", "T cell"]


async def test_bulk_annotate_hard_error_over_max(db):
    out, _ = await bulk_annotate(["x"] * (config.BULK_MAX + 1), db_path=db)
    assert out["error"] == "too_many_terms"


async def test_bulk_annotate_warns_over_soft_cap(db):
    def handler(request):
        return _search_response([])

    async with _client(handler) as cli:
        out, _ = await bulk_annotate(["x"] * (config.BULK_WARN + 1), db_path=db, client=cli)
    assert "warning" in out
    assert out["results"][0]["best_match"] is None


async def test_bulk_annotate_hint_fails_open_when_registry_is_unreadable(db, monkeypatch):
    """A broken/missing index must not turn ontology_hint into a hard rejection.

    The no-hint path already degrades gracefully on this failure; the hinted path
    must match it instead of rejecting every hint as out_of_profile.
    """
    from ontomcp.core.tools import bulk as bulk_module

    def broken_current():
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(bulk_module.registry, "current", broken_current)

    def handler(request):
        return _search_response([_term_doc(obo_id="GO:0008150", label="biological_process")])

    async with _client(handler) as cli:
        out, _ = await bulk_annotate(["biological_process"], ontology_hint="GO", db_path=db, client=cli)

    assert "error" not in out
    assert out["results"][0]["best_match"]["curie"] == "GO:0008150"


# --- get_term_graph --------------------------------------------------------


async def test_get_term_graph_shape_and_roles(db):
    # The graph is built from DIRECT parents/children, so it must hit /parents and
    # /children (not the transitive endpoints) to produce truthful one-hop edges.
    def handler(request):
        url = str(request.url)
        if url.endswith("/parents"):
            return _hier_response(["GO:0012501"])
        if url.endswith("/children"):
            return _hier_response(["GO:0070997"])
        return _terms_response([_term_doc()])

    async with _client(handler) as cli:
        graph, _ = await get_term_graph(
            "GO:0008219", include_siblings=False, db_path=db, client=cli
        )
    assert graph["focus_curie"] == "GO:0008219"
    roles = {n["curie"]: n["role"] for n in graph["nodes"]}
    assert roles["GO:0008219"] == "focus"
    assert roles["GO:0012501"] == "ancestor"  # direct parent
    assert roles["GO:0070997"] == "descendant"  # direct child
    assert any(e["source"] == "GO:0008219" and e["target"] == "GO:0012501" for e in graph["edges"])
    assert any(e["source"] == "GO:0070997" and e["target"] == "GO:0008219" for e in graph["edges"])


async def test_get_term_graph_caps_nodes(db):
    big = [f"GO:{i:07d}" for i in range(100)]

    def handler(request):
        url = str(request.url)
        if url.endswith("/children"):
            return _hier_response(big)
        if url.endswith("/parents"):
            return _hier_response([])
        return _terms_response([_term_doc()])

    async with _client(handler) as cli:
        graph, _ = await get_term_graph(
            "GO:0008219", include_siblings=False, db_path=db, client=cli
        )
    assert len(graph["nodes"]) <= config.GRAPH_NODE_CAP
    assert any(n["role"] == "focus" for n in graph["nodes"])


async def test_get_term_graph_reports_a_degraded_parent_fetch_instead_of_hiding_it(db):
    # A hierarchy fetch failure must not look like "this term has no parents" —
    # the graph should still render (children succeed) but must name the failure.
    def handler(request):
        url = str(request.url)
        if url.endswith("/parents"):
            return httpx.Response(403, text="boom")  # not a retry status, not a 404
        if url.endswith("/children"):
            return _hier_response(["GO:0070997"])
        return _terms_response([_term_doc()])

    async with _client(handler) as cli:
        graph, _ = await get_term_graph(
            "GO:0008219", include_siblings=False, db_path=db, client=cli
        )
    roles = {n["curie"]: n["role"] for n in graph["nodes"]}
    assert roles["GO:0070997"] == "descendant"
    assert not any(n["role"] == "ancestor" for n in graph["nodes"])
    assert graph["degraded"] == [
        {"edge": "parents", "error": "fetch_failed", "detail": graph["degraded"][0]["detail"], "curie": "GO:0008219"}
    ]


# --- integration (live OLS, skipped offline) -------------------------------


@pytest.mark.integration
async def test_search_terms_live(db):
    payload, _ = await search_terms("cell death", ontologies=["GO"], db_path=db)
    assert any(r["curie"] == "GO:0008219" for r in payload["results"])
    assert payload["meta"]["scope"] == ["GO"]


@pytest.mark.integration
async def test_get_term_live(db):
    term, _ = await get_term("GO:0008219", db_path=db)
    assert term["label"].lower() == "cell death"
