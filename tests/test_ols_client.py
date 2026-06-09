"""Unit tests for the OLS client. httpx is mocked — no network (CLAUDE.md)."""

import httpx
import pytest

from ontomcp.core import config, ols_client
from ontomcp.core.ols_client import (
    OLSClient,
    _parse_term,
    curie_to_iri,
    double_encode_iri,
    normalize_curie,
)


async def _noop_sleep(_seconds):
    return None


def _client(handler) -> OLSClient:
    """OLSClient wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=config.OLS_BASE_URL,
        transport=transport,
        headers={"User-Agent": config.USER_AGENT},
    )
    return OLSClient(client=http)


# --- CURIE / IRI helpers ---------------------------------------------------


def test_normalize_curie_strips_obo_and_uppercases():
    assert normalize_curie("obo:go_0008219") == "GO:0008219"
    assert normalize_curie("go:0008219") == "GO:0008219"
    assert normalize_curie("GO_0008219") == "GO:0008219"


def test_normalize_curie_maps_hp_to_hpo():
    assert normalize_curie("HP:0000001") == "HPO:0000001"
    assert normalize_curie("hp_0000001") == "HPO:0000001"


def test_normalize_curie_rejects_non_curie():
    with pytest.raises(ValueError):
        normalize_curie("plainstring")


@pytest.mark.parametrize("bad", [":", "GO:", ":0008219", "_", "GO_", "_123", "obo:", ""])
def test_normalize_curie_rejects_empty_prefix_or_local_id(bad):
    # A separator alone is not enough — both a prefix and a local id are required.
    with pytest.raises(ValueError):
        normalize_curie(bad)


def test_curie_to_iri_uses_templates():
    assert curie_to_iri("GO:0008219") == "http://purl.obolibrary.org/obo/GO_0008219"
    # HPO maps back to the HP_ template.
    assert curie_to_iri("HP:0000001") == "http://purl.obolibrary.org/obo/HP_0000001"
    # EFO and MeSH use special hosts.
    assert curie_to_iri("EFO:0000001") == "http://www.ebi.ac.uk/efo/EFO_0000001"
    assert curie_to_iri("MESH:D000001") == "http://id.nlm.nih.gov/mesh/D000001"


def test_curie_to_iri_unknown_prefix():
    with pytest.raises(ValueError):
        curie_to_iri("ZZZ:0001")


def test_double_encode_iri_encodes_twice():
    encoded = double_encode_iri("http://purl.obolibrary.org/obo/GO_0008219")
    # ':' -> '%3A' -> '%253A'; '/' -> '%2F' -> '%252F'
    assert "%253A" in encoded
    assert "%252F" in encoded
    assert ":" not in encoded
    assert "/" not in encoded


# --- Parsing ---------------------------------------------------------------


def test_parse_term_full_shape():
    obj = {
        "obo_id": "GO:0008219",
        "label": "cell death",
        "description": ["any biological process that ends life"],
        "is_obsolete": False,
        "obo_synonym": [
            {"name": "cell killing", "type": "hasExactSynonym"},
            {"name": "apoptosis-ish", "type": "hasRelatedSynonym"},
            {"name": "necrosis-ish", "type": "hasNarrowSynonym"},
            {"name": "death", "type": "hasBroadSynonym"},
        ],
    }
    term = _parse_term(obj)
    assert term["curie"] == "GO:0008219"
    assert term["ontology"] == "GO"
    assert term["label"] == "cell death"
    assert term["definition"] == "any biological process that ends life"
    assert term["is_obsolete"] == 0
    assert term["replaced_by"] is None
    assert term["synonyms"] == {
        "exact": ["cell killing"],
        "related": ["apoptosis-ish"],
        "narrow": ["necrosis-ish"],
        "broad": ["death"],
    }
    assert term["raw_json"] is obj


def test_parse_term_obsolete_with_replacement():
    obj = {
        "obo_id": "GO:0000001",
        "label": "old term",
        "is_obsolete": True,
        "term_replaced_by": ["GO:0008219"],
    }
    term = _parse_term(obj)
    assert term["is_obsolete"] == 1
    assert term["replaced_by"] == "GO:0008219"


def test_parse_term_replaced_by_iri_form():
    # OLS sometimes returns the replacement as a full IRI, not a CURIE.
    obj = {
        "obo_id": "GO:0006917",
        "label": "induction of apoptosis",
        "is_obsolete": True,
        "term_replaced_by": ["http://purl.obolibrary.org/obo/GO_0006915"],
    }
    term = _parse_term(obj)
    assert term["replaced_by"] == "GO:0006915"


def test_parse_term_consider_from_annotation():
    obj = {
        "obo_id": "GO:0000002",
        "label": "obsolete with no single replacement",
        "is_obsolete": True,
        "annotation": {
            "consider": [
                "http://purl.obolibrary.org/obo/GO_0008219",
                "GO:0006915",
            ]
        },
    }
    term = _parse_term(obj)
    assert term["replaced_by"] is None
    assert term["consider"] == ["GO:0008219", "GO:0006915"]


def test_parse_term_subsets_and_leaf_flags():
    obj = {
        "obo_id": "GO:0008219",
        "label": "cell death",
        "in_subset": ["gocheck_do_not_annotate"],
        "has_children": True,
        "obo_definition_citation": [
            {"definition": "...", "oboXrefs": [{"database": "GOC", "id": "GOC:mtg"}]}
        ],
    }
    term = _parse_term(obj)
    assert term["subsets"] == ["gocheck_do_not_annotate"]
    assert term["has_children"] is True
    assert term["is_leaf"] is False
    assert "GOC:mtg" in term["definition_sources"]


def test_parse_definition_sources_composes_prefix():
    # OLS nests citations as {database, id} with an unprefixed id; compose database:id.
    obj = {
        "obo_id": "GO:0008219",
        "label": "cell death",
        "obo_definition_citation": [
            {
                "definition": "...",
                "oboXrefs": [
                    {"database": "PMID", "id": "25236395"},
                    {"database": "GOC", "id": "GOC:mah"},  # already prefixed, kept as-is
                ],
            }
        ],
    }
    term = _parse_term(obj)
    assert "PMID:25236395" in term["definition_sources"]
    assert "GOC:mah" in term["definition_sources"]


def test_parse_term_optional_flags_absent():
    # When OLS omits the structural flags, they stay None (not a false-y guess).
    term = _parse_term({"obo_id": "GO:0008219", "label": "cell death"})
    assert term["has_children"] is None
    assert term["is_leaf"] is None
    assert term["consider"] == []
    assert term["subsets"] == []
    assert term["definition_sources"] == []


# --- Network methods (mocked) ----------------------------------------------


async def test_search_parses_docs_and_filters_ontology():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        body = {
            "response": {
                "docs": [
                    {
                        "obo_id": "GO:0008219",
                        "label": "cell death",
                        "description": ["ends life"],
                        "score": 9.5,  # raw Solr score — must NOT be passed through
                    },
                    {
                        "obo_id": "GO:0070265",
                        "label": "necrotic cell death",
                        "description": ["a form of cell death"],
                        "score": 4.2,
                    },
                ]
            }
        }
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        results = await c.search("cell death", ontologies=["GO", "HPO"], limit=5)

    # Score is rank-normalized (0–1, best first), not the raw Solr score.
    assert [r["curie"] for r in results] == ["GO:0008219", "GO:0070265"]
    assert results[0]["score"] == 1.0
    assert results[1]["score"] == 0.5
    assert results[0]["label"] == "cell death"
    assert results[0]["definition"] == "ends life"
    assert "ontology=go%2Chp" in seen["url"] or "ontology=go,hp" in seen["url"]
    assert "rows=5" in seen["url"]


async def test_fetch_term_parses_embedded():
    def handler(request):
        assert request.url.path == "/ols4/api/ontologies/go/terms"
        assert request.url.params["obo_id"] == "GO:0008219"
        body = {"_embedded": {"terms": [{"obo_id": "GO:0008219", "label": "cell death"}]}}
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        term = await c.fetch_term("go:0008219")
    assert term["curie"] == "GO:0008219"
    assert term["label"] == "cell death"


async def test_fetch_term_not_found():
    def handler(request):
        return httpx.Response(404)

    async with _client(handler) as c:
        result = await c.fetch_term("GO:9999999")
    assert result == {"error": "not_found", "curie": "GO:9999999"}


async def test_fetch_parents_is_direct_and_tagged_is_a():
    seen = {}

    def handler(request):
        seen["target"] = request.url.raw_path.decode()
        body = {
            "_embedded": {"terms": [{"obo_id": "GO:0012501", "label": "programmed cell death"}]}
        }
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        nodes = await c.fetch_parents("GO:0006915")

    # Direct parents are true subClassOf edges: tagged is_a.
    assert nodes == [{"curie": "GO:0012501", "label": "programmed cell death", "rel_type": "is_a"}]
    assert "%253A" in seen["target"]  # double-encoded ':' on the wire
    assert seen["target"].endswith("/parents")


async def test_fetch_ontology_version_root_field():
    def handler(request):
        assert request.url.path.endswith("/ontologies/go")
        return httpx.Response(200, json={"version": "2026-05-19", "config": {}})

    async with _client(handler) as c:
        version = await c.fetch_ontology_version("GO")
    assert version == "2026-05-19"


async def test_fetch_ontology_version_falls_back_to_config():
    def handler(request):
        return httpx.Response(200, json={"config": {"version": "2026-04-01"}})

    async with _client(handler) as c:
        version = await c.fetch_ontology_version("MONDO")
    assert version == "2026-04-01"


async def test_fetch_ontology_version_none_on_failure():
    async with _client(lambda r: httpx.Response(500)) as c:
        version = await c.fetch_ontology_version("GO")
    assert version is None


async def test_fetch_children_uses_correct_endpoint():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"_embedded": {"terms": []}})

    async with _client(handler) as c:
        await c.fetch_children("GO:0008219")
    assert seen["path"].endswith("/children")


async def test_fetch_ancestors_is_transitive_not_is_a():
    seen = {}

    def handler(request):
        seen["target"] = request.url.raw_path.decode()
        body = {"_embedded": {"terms": [{"obo_id": "GO:0008150", "label": "biological_process"}]}}
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        nodes = await c.fetch_ancestors("GO:0008219")

    # The transitive endpoint must NOT claim a direct is_a edge — tag "ancestor".
    assert nodes == [{"curie": "GO:0008150", "label": "biological_process", "rel_type": "ancestor"}]
    assert "%253A" in seen["target"]  # double-encoded ':' on the wire
    assert seen["target"].endswith("/ancestors")


async def test_fetch_descendants_uses_correct_endpoint():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        body = {"_embedded": {"terms": [{"obo_id": "GO:0070265", "label": "necrotic cell death"}]}}
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        nodes = await c.fetch_descendants("GO:0008219")
    assert seen["path"].endswith("/hierarchicalDescendants")
    assert nodes[0]["rel_type"] == "descendant"


# --- Retry logic -----------------------------------------------------------


async def test_retry_succeeds_after_transient_503(monkeypatch):
    monkeypatch.setattr(ols_client.asyncio, "sleep", _noop_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"_embedded": {"terms": [{"obo_id": "GO:1", "label": "x"}]}}
        )

    async with _client(handler) as c:
        term = await c.fetch_term("GO:1")
    assert calls["n"] == 3
    assert term["curie"] == "GO:1"


async def test_retry_exhausts_on_persistent_429(monkeypatch):
    monkeypatch.setattr(ols_client.asyncio, "sleep", _noop_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429)

    async with _client(handler) as c:
        result = await c.fetch_term("GO:1")
    assert calls["n"] == config.OLS_MAX_RETRIES
    assert result["error"] == "fetch_failed"


async def test_404_is_not_retried(monkeypatch):
    monkeypatch.setattr(ols_client.asyncio, "sleep", _noop_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    async with _client(handler) as c:
        result = await c.fetch_term("GO:9999999")
    assert calls["n"] == 1
    assert result["error"] == "not_found"


async def test_user_agent_header_sent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(
            200, json={"_embedded": {"terms": [{"obo_id": "GO:1", "label": "x"}]}}
        )

    async with _client(handler) as c:
        await c.fetch_term("GO:1")
    assert seen["ua"] == "OntoMCP/0.1.0"


# --- Integration (live network) --------------------------------------------


@pytest.mark.integration
async def test_live_fetch_term():
    async with OLSClient() as c:
        term = await c.fetch_term("GO:0008219")
    assert term["curie"] == "GO:0008219"
    assert term["label"]
    assert set(term["synonyms"]) == {"exact", "related", "narrow", "broad"}
