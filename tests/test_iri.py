"""The IRI resolution chain and its disconfirmation signal — Phase-A gate.

Two properties are under test, and the second is the one that matters:

1. the rungs are tried in evidence order, so an authoritative IRI is never
   displaced by a guess;
2. a *guessed* IRI that misses escalates rather than reporting "no parents" —
   which is what the four hierarchy tools did for every unreachable IRI before
   this (tournament/spike-results.md S-4).
"""

import httpx
import pytest

from ontomcp.core import cache, config, iri, registry
from ontomcp.core.config import IRI_TEMPLATES, ONTOLOGIES
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import get_ancestors, get_parents


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cache.db"
    cache.init_db(path)
    return path


def _resolved(members: dict):
    return registry.resolve({"name": "t", "_derived": {"index_version": "t", "members": members}})


# --- rung order ------------------------------------------------------------


def test_rung_0_covers_every_builtin():
    # The gate clause: correct IRIs for all built-ins through rung 0, so their
    # existing tests stay green and no network is involved.
    for code in ONTOLOGIES:
        best, authoritative = iri.candidates(f"{code}:0000001")[0]
        assert best == IRI_TEMPLATES[code].format(id="0000001")
        assert authoritative is True


def test_rung_3_resolves_an_ontology_outside_the_builtins():
    # PO is not in IRI_TEMPLATES. Before the chain, hierarchy on it returned
    # bad_curie — i.e. ~276 of OLS4's 286 ontologies had no hierarchy at all.
    best, authoritative = iri.candidates("PO:0025034")[0]
    assert best == "http://purl.obolibrary.org/obo/PO_0025034"
    assert authoritative is False  # a guess, and it must say so


def test_rung_1_prefers_the_cached_iri_over_a_guess(db):
    cache.put_term(
        db,
        {
            "curie": "PO:0025034",
            "ontology": "PO",
            "label": "leaf",
            "iri": "http://purl.obolibrary.org/obo/PO_0025034_real",
        },
    )
    best, authoritative = iri.candidates("PO:0025034", db_path=db)[0]
    assert best.endswith("_real")
    assert authoritative is True


def test_rung_1_ignores_an_empty_cached_iri(db):
    # Tested `is not None`, never truthiness — an empty string is a miss.
    cache.put_term(db, {"curie": "PO:1", "ontology": "PO", "label": "x", "iri": ""})
    assert all(not i.endswith("PO_1_real") for i, _ in iri.candidates("PO:1", db_path=db))


def test_rung_2_uses_the_freeze_probed_stem():
    # ORDO mints ORDO:NNN but its IRIs are .../Orphanet_NNN, so a naive prefix
    # substitution is wrong for exactly the ontology a rare-disease profile needs.
    resolved = _resolved({"ORDO": {"base_uri": "http://www.orpha.net/ORDO/Orphanet_"}})
    best, _ = iri.candidates("ORDO:10", registry=resolved)[0]
    assert best == "http://www.orpha.net/ORDO/Orphanet_10"


def test_rung_3_honours_a_probed_minting_prefix():
    resolved = _resolved({"ORDO": {"curie_prefix": "Orphanet"}})
    guesses = [i for i, _ in iri.candidates("ORDO:10", registry=resolved)]
    assert "http://purl.obolibrary.org/obo/Orphanet_10" in guesses


def test_candidates_are_deduped_and_ordered():
    resolved = _resolved({"GO": {"base_uri": "http://purl.obolibrary.org/obo/GO_"}})
    guesses = [i for i, _ in iri.candidates("GO:0008219", registry=resolved)]
    assert guesses == ["http://purl.obolibrary.org/obo/GO_0008219"]


# --- disconfirmation -------------------------------------------------------


def _client(handler):
    return OLSClient(
        client=httpx.AsyncClient(
            base_url=config.OLS_BASE_URL, transport=httpx.MockTransport(handler)
        )
    )


def _terms(*labels):
    return {
        "_embedded": {
            "terms": [{"obo_id": f"GO:{i}", "label": label} for i, label in enumerate(labels)]
        }
    }


async def test_a_guessed_iri_that_404s_escalates_and_retries(db):
    """The core S-4 fix: a wrong guess must not become 'this term has no parents'."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "/parents" in str(request.url):
            # The guessed IRI is unknown; the real one works.
            if "PO_0025034_real" in str(request.url):
                return httpx.Response(200, json=_terms("plant structure"))
            return httpx.Response(404)
        # rung 4: fetch_term by obo_id needs no IRI, which is what rescues us.
        return httpx.Response(
            200,
            json={
                "_embedded": {
                    "terms": [
                        {
                            "obo_id": "PO:0025034",
                            "label": "leaf",
                            "iri": "http://purl.obolibrary.org/obo/PO_0025034_real",
                        }
                    ]
                }
            },
        )

    async with _client(handler) as cli:
        result, _ = await get_parents("PO:0025034", db_path=db, client=cli)

    assert [n["label"] for n in result] == ["plant structure"]
    assert any("obo_id=PO" in c or "obo_id" in c for c in calls), "rung 4 was never reached"
    # and the real IRI is persisted, so the next call resolves at rung 1
    assert cache.get_term(db, "PO:0025034")["iri"].endswith("_real")


async def test_an_authoritative_iri_with_no_nodes_is_believed(db):
    """A leaf is a leaf. Escalating here would cost two round trips on most terms."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"_embedded": {"terms": []}})

    async with _client(handler) as cli:
        result, _ = await get_parents("GO:0008219", db_path=db, client=cli)

    assert result == []
    assert len(calls) == 1, "an authoritative IRI must not trigger an escalation"


async def test_a_stale_rung_0_template_does_not_shadow_a_cached_rung_1_answer(db):
    """rung 0 (hand-written template) and rung 1 (OLS's own cached IRI) are both
    flagged authoritative, but a stale template answering empty must not be
    believed over OLS's own answer for this term when that stronger candidate
    (rung 1) is still waiting to be tried."""
    cache.put_term(
        db,
        {
            "curie": "GO:0008219",
            "ontology": "GO",
            "label": "cell death",
            "iri": "http://purl.obolibrary.org/obo/GO_0008219_real",
        },
    )
    calls = []

    def handler(request):
        url = str(request.url)
        calls.append(url)
        if "GO_0008219_real" in url:
            return httpx.Response(200, json=_terms("biological_process"))
        return httpx.Response(200, json={"_embedded": {"terms": []}})  # the stale template

    async with _client(handler) as cli:
        result, _ = await get_parents("GO:0008219", db_path=db, client=cli)

    assert [n["label"] for n in result] == ["biological_process"]
    assert len(calls) == 2, "must try the cached rung-1 IRI, not stop at the stale template"


async def test_an_unresolvable_term_returns_a_not_found_error(db):
    # Every rung guessed wrong AND the rung-4 escalation itself 404s: OLS has
    # never heard of this term. That is a real disconfirmation, not "no parents".
    def handler(request):
        return httpx.Response(404)

    async with _client(handler) as cli:
        result, _ = await get_ancestors("PO:9999999", db_path=db, client=cli)

    assert result[0]["error"] == "not_found"


async def test_an_ontology_ols4_does_not_serve_is_a_loud_error(db):
    def handler(request):
        raise AssertionError("must not reach the network for an unknown ontology")

    async with _client(handler) as cli:
        result, _ = await get_parents("MOONDO:1", db_path=db, client=cli)

    assert result[0]["error"] == "unknown_ontology"
