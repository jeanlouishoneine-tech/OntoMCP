"""Search semantics, the FTS rebuild and the response channel — the Phase-C gate.

Every assertion maps to a gate clause in the scaling plan §6 Phase C.
"""

import httpx
import pytest

from ontomcp.core import cache, config
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import bulk_annotate, get_term, search_terms


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


def _docs(*rows):
    return httpx.Response(200, json={"response": {"docs": list(rows)}})


def _doc(obo_id, label, prefix=None, obsolete=False):
    return {
        "obo_id": obo_id,
        "label": label,
        "ontology_prefix": prefix or obo_id.split(":")[0],
        "description": ["d"],
        "is_obsolete": obsolete,
    }


# --- C1: the FTS rebuild ---------------------------------------------------


def test_a_synonym_only_query_hits_the_cache(db):
    """THE gate clause. Every cache_hit was synonym-blind before this."""
    cache.put_term(
        db,
        {
            "curie": "CHEBI:15365",
            "ontology": "CHEBI",
            "label": "acetylsalicylic acid",
            "definition": "a salicylate",
            "synonyms": {"exact": ["aspirin"], "related": ["ASA"]},
        },
    )
    assert [h["curie"] for h in cache.fts_search(db, "aspirin")] == ["CHEBI:15365"]
    assert [h["curie"] for h in cache.fts_search(db, "ASA")] == ["CHEBI:15365"]


def test_stemming_matches_a_morphological_variant(db):
    cache.put_term(
        db,
        {
            "curie": "GO:0006915",
            "ontology": "GO",
            "label": "apoptotic process",
            "definition": "programmed cell death",
        },
    )
    # Unstemmed unicode61 matched neither of these.
    assert cache.fts_search(db, "apoptosis") or cache.fts_search(db, "programmed deaths")


def test_prefix_matching_on_the_final_token(db):
    cache.put_term(db, {"curie": "GO:1", "ontology": "GO", "label": "apoptotic process"})
    assert [h["curie"] for h in cache.fts_search(db, "apopto")] == ["GO:1"]


def test_the_rebuild_loses_no_cached_term(db):
    """terms_fts is external-content, so a rebuild costs an index, never a term."""
    cache.put_term(db, {"curie": "GO:1", "ontology": "GO", "label": "cell death"})

    conn = cache._connect(db)
    with conn:
        for trigger in ("terms_ai", "terms_ad", "terms_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE terms_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE terms_fts USING fts5("
            "curie UNINDEXED, label, definition, content='terms', content_rowid='rowid')"
        )
    conn.close()

    cache.init_db(db)  # must detect the old declaration and rebuild

    assert cache.get_term(db, "GO:1") is not None
    assert [h["curie"] for h in cache.fts_search(db, "cell death")] == ["GO:1"]

    conn = cache._connect(db)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='terms_fts'").fetchone()["sql"]
    conn.close()
    assert "synonyms_text" in ddl and "porter" in ddl


def test_the_migration_is_idempotent(db):
    cache.init_db(db)
    cache.init_db(db)
    cache.put_term(db, {"curie": "GO:1", "ontology": "GO", "label": "x"})
    assert cache.fts_search(db, "x")


async def test_a_synonym_query_is_served_from_cache_end_to_end(db):
    """The FULL search_terms path, not just cache.fts_search — the C1 evidence gap.

    Models the real annotation flow: search, open the term, search again using a
    synonym from the record. Pre-C1 step 3 went back to OLS every time, because FTS
    could not match a synonym; post-C1 it is served from cache.

    The label deliberately does not contain the query token, so the only possible
    match path is the synonym — a pass here cannot come from anything else.
    """
    LABEL = "acetylsalicylic acid"
    assert "aspirin" not in LABEL.lower()

    calls = {"search": 0, "term": 0}

    def handler(request):
        url = str(request.url)
        if "/search" in url:
            calls["search"] += 1
            return _docs(_doc("CHEBI:15365", LABEL, prefix="CHEBI"))
        if "/terms" in url:
            calls["term"] += 1
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "terms": [
                            {
                                "obo_id": "CHEBI:15365",
                                "label": LABEL,
                                "obo_synonym": [{"type": "hasExactSynonym", "name": "aspirin"}],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"version": "2026-07-26"})

    async with _client(handler) as cli:
        # 1. OLS answers, a stub is warmed, and the ledger records the coverage.
        _first, hit1 = await search_terms("aspirin", db_path=db, client=cli)
        # 2. The scientist opens the term. This writes the scoped synonyms.
        await get_term("CHEBI:15365", db_path=db, client=cli)
        # 3. The same synonym query is now answerable from the cache.
        second, hit2 = await search_terms("aspirin", db_path=db, client=cli)

    assert (hit1, hit2) == (False, True)
    assert calls["search"] == 1, "step 3 must not reach OLS"
    assert second["meta"]["source"] == "cache"
    assert [r["curie"] for r in second["results"]] == ["CHEBI:15365"]


async def test_a_search_warmed_stub_is_synonym_searchable(db):
    """A term warmed by search ALONE must answer a synonym query from cache.

    Without synonyms on the stub this is impossible: the stub's label does not
    contain the query token, FTS finds nothing, the coverage check fails, and the
    query falls through to OLS forever. That left the synonym rebuild's benefit
    stranded on the get_term path.
    """
    LABEL = "acetylsalicylic acid"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        doc = _doc("CHEBI:15365", LABEL, prefix="CHEBI")
        doc["synonym"] = ["aspirin", "ASA", "aspirin"]  # OLS really does repeat
        return _docs(doc)

    async with _client(handler) as cli:
        await search_terms("aspirin", db_path=db, client=cli)
        second, hit = await search_terms("aspirin", db_path=db, client=cli)

    assert hit is True
    assert calls["n"] == 1, "the second identical query must be served from cache"
    assert [r["curie"] for r in second["results"]] == ["CHEBI:15365"]


async def test_search_results_never_leak_synonyms_into_the_payload(db):
    """Synonyms are fetched for the cache, never for the model.

    CHEBI:15365 alone carries 21 of them; ten rows would add several hundred
    strings the model has no use for, which is what the payload cap rule forbids.
    """

    def handler(request):
        doc = _doc("CHEBI:15365", "acetylsalicylic acid", prefix="CHEBI")
        doc["synonym"] = ["aspirin", "ASA"]
        return _docs(doc)

    async with _client(handler) as cli:
        payload, _ = await search_terms("aspirin", db_path=db, client=cli)

    row = payload["results"][0]
    assert "synonyms" not in row
    assert set(row) == {
        "curie",
        "label",
        "ontology",
        "definition",
        "is_obsolete",
        "iri",
        "rank",
    }
    # ...but the cache did keep them, which is the point.
    assert [h["curie"] for h in cache.fts_search(db, "ASA")] == ["CHEBI:15365"]


# --- C2: the coverage ledger ----------------------------------------------


async def test_a_partial_fts_result_is_not_treated_as_cache_coverage(db):
    """F5. `if hits: return hits` served a partial answer as complete for 7 days."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("GO:1", "cell death"), _doc("GO:2", "cell cycle"))

    # Seed ONE matching row by a route that does not record ledger coverage.
    cache.put_term(db, {"curie": "GO:9", "ontology": "GO", "label": "cell wall"})

    async with _client(handler) as cli:
        await search_terms("cell", db_path=db, client=cli)
    assert calls["n"] == 1, "an unrecorded query must fall through to OLS"


async def test_a_recorded_query_is_served_from_cache(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        first, hit1 = await search_terms("cell death", db_path=db, client=cli)
        second, hit2 = await search_terms("cell death", db_path=db, client=cli)

    assert (hit1, hit2) == (False, True)
    assert calls["n"] == 1
    assert second["meta"]["source"] == "cache"


async def test_a_zero_result_query_is_never_served_from_a_stale_cache_claim(db, monkeypatch):
    """A genuinely empty OLS answer must not be recorded as coverage: row_count=0
    makes `fts_rows >= min(row_count, limit)` vacuously true, so a repeat of the
    same empty query would otherwise be served `source: "cache"` — and silently
    suppress the escape hatch, which only fires on this (OLS) branch — for the
    full cache TTL. Escape-hatch disabled so this isolates the coverage-ledger
    behaviour from its own (separately tested) OLS calls.
    """
    monkeypatch.setenv("ONTOMCP_ESCAPE_HATCH", "0")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs()

    async with _client(handler) as cli:
        first, hit1 = await search_terms("nothing here", db_path=db, client=cli)
        second, hit2 = await search_terms("nothing here", db_path=db, client=cli)

    assert (hit1, hit2) == (False, False)
    assert calls["n"] == 2
    assert first["meta"]["source"] == second["meta"]["source"] == "ols"


async def test_a_larger_limit_is_not_served_from_a_smaller_recorded_one(db):
    """The ledger records the limit, so asking for more must re-ask OLS."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        await search_terms("cell death", limit=5, db_path=db, client=cli)
        await search_terms("cell death", limit=50, db_path=db, client=cli)
    assert calls["n"] == 2


async def test_the_ledger_is_keyed_on_scope_not_just_query(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        await search_terms("cell death", ontologies=["GO"], db_path=db, client=cli)
        await search_terms("cell death", ontologies=["MONDO"], db_path=db, client=cli)
    assert calls["n"] == 2, "a different scope is a different question"


# --- C3/C4: over-fetch, collapse, the minting filter, filtered_out ---------


async def test_over_fetch_happens_unfiltered_too(db):
    """Dedup (_collapse) runs on every path, unfiltered included, so both must over-fetch."""
    seen = {}

    def handler(request):
        seen.setdefault("rows", []).append(request.url.params.get("rows"))
        return _docs(_doc("GO:1", "x"))

    async with _client(handler) as cli:
        await search_terms("x", db_path=db, client=cli)  # unfiltered
        await search_terms("y", ontologies=["GO"], limit=10, db_path=db, client=cli)

    assert seen["rows"][0] == "30"  # unfiltered: limit * 3, so dedup can't shrink
    assert seen["rows"][1] == "30"  # filtered: limit * 3, so the filter can't shrink


async def test_a_scoped_search_returns_only_minted_rows_and_names_the_rest(db):
    """Gate: an EFO-scoped search returns no MONDO-minted rows, and says so."""

    def handler(request):
        return _docs(
            _doc("EFO:1", "asthma", prefix="EFO"),
            _doc("MONDO:2", "asthma", prefix="EFO"),  # indexed by EFO, minted by MONDO
        )

    async with _client(handler) as cli:
        payload, _ = await search_terms("asthma", ontologies=["EFO"], db_path=db, client=cli)

    assert [r["curie"] for r in payload["results"]] == ["EFO:1"]
    assert payload["meta"]["filtered_out"] == ["MONDO"]


async def test_an_imported_term_still_warms_the_cache(db):
    """The write-back happens BEFORE the filter, so a dropped row is not lost."""

    def handler(request):
        return _docs(_doc("EFO:1", "asthma"), _doc("MONDO:2", "asthma"))

    async with _client(handler) as cli:
        await search_terms("asthma", ontologies=["EFO"], db_path=db, client=cli)

    assert cache.get_term(db, "MONDO:2") is not None


async def test_the_same_scoped_search_twice_is_served_from_cache(db):
    """Gate: the identical EFO-scoped query must not fall through to OLS forever.

    This is the defect the minting-prefix filter exists to fix: without it, MONDO
    rows cache as MONDO, the next EFO-filtered FTS query finds fewer rows than were
    served, and it re-asks OLS every time.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("EFO:1", "asthma"), _doc("MONDO:2", "asthma"))

    async with _client(handler) as cli:
        await search_terms("asthma", ontologies=["EFO"], db_path=db, client=cli)
        payload, hit = await search_terms("asthma", ontologies=["EFO"], db_path=db, client=cli)

    assert hit is True
    assert calls["n"] == 1
    assert [r["curie"] for r in payload["results"]] == ["EFO:1"]


async def test_default_scope_search_also_filters_foreign_minted_rows(db):
    """Gate: the minting-prefix filter runs on the default (no `ontologies` arg)
    path too, not just an explicitly-scoped one. The common, unscoped path used
    to return a foreign-minted row unfiltered while `meta.scope` claimed a
    narrower set and `meta.filtered_out` reported nothing dropped.
    """

    def handler(request):
        return _docs(
            _doc("GO:1", "cell death"),
            _doc("FOODON:2", "cell death"),  # minted outside the built-in default scope
        )

    async with _client(handler) as cli:
        payload, _ = await search_terms("cell death", db_path=db, client=cli)

    assert [r["curie"] for r in payload["results"]] == ["GO:1"]
    assert payload["meta"]["filtered_out"] == ["FOODON"]


async def test_the_same_default_scope_search_twice_is_served_from_cache(db):
    """Same defect as the explicit-scope case, on the default-scope path: without
    the filter running here too, a foreign-minted row caches under its own
    ontology, the next default-scoped FTS query finds fewer rows than were
    served, and it re-asks OLS forever.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _docs(_doc("GO:1", "cell death"), _doc("FOODON:2", "cell death"))

    async with _client(handler) as cli:
        await search_terms("cell death", db_path=db, client=cli)
        payload, hit = await search_terms("cell death", db_path=db, client=cli)

    assert hit is True
    assert calls["n"] == 1
    assert [r["curie"] for r in payload["results"]] == ["GO:1"]


async def test_duplicate_curies_are_collapsed(db):
    def handler(request):
        return _docs(_doc("GO:1", "cell death"), _doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        payload, _ = await search_terms("cell death", db_path=db, client=cli)
    assert [r["curie"] for r in payload["results"]] == ["GO:1"]


# --- C6/C7: the response channel ------------------------------------------


async def test_search_returns_results_and_meta_with_the_three_always_present(db):
    def handler(request):
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        payload, _ = await search_terms("cell death", db_path=db, client=cli)

    assert set(payload) == {"results", "meta"}
    assert payload["meta"]["source"] == "ols"
    assert payload["meta"]["profile"] == "default"
    assert set(payload["meta"]["scope"]) == set(config.ONTOLOGIES)


async def test_no_response_anywhere_contains_a_score(db):
    """Gate: `score` is deleted, not merely unused."""

    def handler(request):
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        payload, _ = await search_terms("cell death", db_path=db, client=cli)
        again, _ = await search_terms("cell death", db_path=db, client=cli)

    for row in payload["results"] + again["results"]:
        assert "score" not in row
        assert row["rank"] >= 1


async def test_held_back_members_are_reported_when_present(db, tmp_path, monkeypatch):
    from ontomcp.core import profile as profile_mod

    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "p"))

    def handler(request):
        if "/terms" in str(request.url) and "obo_id" not in str(request.url):
            return httpx.Response(
                200, json={"_embedded": {"terms": [{"obo_id": "GO:1", "iri": "http://x/GO_1"}]}}
            )
        if "/ontologies/" in str(request.url) and "/terms" not in str(request.url):
            return httpx.Response(200, json={"version": "v1"})
        return _docs(_doc("GO:1", "cell death"))

    async with _client(handler) as cli:
        await profile_mod.save("t", ["GO", "NCBITAXON"], client=cli)
        monkeypatch.setenv("ONTOMCP_PROFILE", "t")
        payload, _ = await search_terms("cell death", db_path=db, client=cli)

    assert "NCBITAXON" in payload["meta"]["held_back"]
    assert "NCBITAXON" not in payload["meta"]["scope"]


# --- C8: bulk_annotate ----------------------------------------------------


async def test_a_near_miss_is_shown_rather_than_hidden(db):
    """A 0.79 near-miss used to vanish behind best_match: null."""

    def handler(request):
        return _docs(_doc("CL:1", "T-cell, alpha-beta"))

    async with _client(handler) as cli:
        payload, _ = await bulk_annotate(["T cell"], threshold=0.99, db_path=db, client=cli)

    row = payload["results"][0]
    assert row["best_match"] is None
    assert row["alternatives"], "the top candidate must still be offered"
    assert row["alternatives"][0]["curie"] == "CL:1"


async def test_bulk_flags_an_obsolete_best_match(db):
    """Gate: 500 rows is the highest-volume surface; an obsolete CURIE must be flagged."""

    def handler(request):
        return _docs(_doc("GO:0006917", "induction of apoptosis", obsolete=True))

    async with _client(handler) as cli:
        payload, _ = await bulk_annotate(
            ["induction of apoptosis"], threshold=0.8, db_path=db, client=cli
        )

    assert payload["results"][0]["best_match"]["is_obsolete"] is True


async def test_bulk_does_not_repeat_the_meta_block_per_row(db):
    """500 copies of the same scope statement is the context tax meta exists to avoid."""

    def handler(request):
        return _docs(_doc("CL:1", "T cell"))

    async with _client(handler) as cli:
        payload, _ = await bulk_annotate(["T cell", "B cell"], db_path=db, client=cli)

    assert "meta" not in payload
    assert set(payload["results"][0]) == {"input", "best_match", "alternatives"}


# --- integration: the live service must actually honour the fieldList -------


@pytest.mark.integration
async def test_live_ols_returns_synonyms_under_our_fieldlist(db):
    """The mocked tests prove our code path; only this proves OLS cooperates.

    `synonym` is the field OLS4 actually serves — `obo_synonym` and `synonyms` are
    dropped silently, and `related_synonyms` is a strict subset. If OLS ever stops
    returning it, every search-warmed row goes synonym-blind again and no unit test
    would notice, because they all supply the field themselves.
    """
    from ontomcp.core.ols_client import OLSClient

    async with OLSClient() as cli:
        results = await cli.search("acetylsalicylic acid", ["CHEBI"], limit=5)

    if results and results[0].get("error"):
        pytest.skip(f"OLS unavailable: {results[0]}")

    aspirin = next((r for r in results if r["curie"] == "CHEBI:15365"), None)
    assert aspirin is not None, "CHEBI:15365 should be in the top 5 for its own label"
    assert aspirin["synonyms"], "OLS returned no synonyms — the fieldList is not being honoured"
    assert any("aspirin" == s.lower() for s in aspirin["synonyms"])


@pytest.mark.integration
async def test_a_live_search_warms_the_cache_for_a_synonym_query(db):
    """End-to-end against the real service: search once, then query by synonym."""
    from ontomcp.core.ols_client import OLSClient

    async with OLSClient() as cli:
        payload, _ = await search_terms(
            "acetylsalicylic acid", ontologies=["CHEBI"], db_path=db, client=cli
        )
    if not isinstance(payload, dict):
        pytest.skip(f"OLS unavailable: {payload}")

    # The label contains no "aspirin"; only the cached synonyms can match it.
    hits = [h["curie"] for h in cache.fts_search(db, "aspirin", ["CHEBI"], 10)]
    assert "CHEBI:15365" in hits
