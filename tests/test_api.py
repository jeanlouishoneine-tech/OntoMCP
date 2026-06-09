"""FastAPI route tests. OLS is mocked — no network (CLAUDE.md).

The app is driven in-process via httpx ASGITransport (no live server). Each test
overrides the app's shared OLS client with a MockTransport-backed one and points
the routes' DB_PATH at an isolated temp DB.
"""

import httpx
import pytest

from ontomcp import __version__
from ontomcp.api.main import create_app
from ontomcp.api.responses import get_db_path
from ontomcp.core import cache, config
from ontomcp.core.ols_client import OLSClient


def _client(handler) -> OLSClient:
    """OLSClient wired to a MockTransport handler (no network)."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=config.OLS_BASE_URL,
        transport=transport,
        headers={"User-Agent": config.USER_AGENT},
    )
    return OLSClient(client=http)


def _search_response(docs):
    return httpx.Response(200, json={"response": {"docs": docs}})


def _terms_response(terms):
    return httpx.Response(200, json={"_embedded": {"terms": terms}})


def _term_doc(obo_id="GO:0008219", label="cell death", desc="a process", obsolete=False):
    return {
        "obo_id": obo_id,
        "label": label,
        "description": [desc],
        "is_obsolete": obsolete,
    }


def _hier_response(curies):
    return _terms_response([{"obo_id": c, "label": c} for c in curies])


@pytest.fixture
def api(tmp_db_path):
    """Return ``make_http(handler)`` bound to the ASGI app on an isolated temp DB.

    The returned factory sets the app's shared OLS client to one using ``handler``
    for all outbound OLS calls, then yields an httpx client bound to the ASGI app.
    Routes read the cache path via the ``get_db_path`` dependency, which we
    override to point at the temp DB.
    """
    cache.init_db(tmp_db_path)

    app = create_app()
    app.dependency_overrides[get_db_path] = lambda: tmp_db_path

    def make_http(handler) -> httpx.AsyncClient:
        app.state.ols_client = _client(handler)
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return make_http


def _assert_envelope(body):
    assert set(body) == {"data", "cached", "ontomcp_version"}
    assert body["cached"] in (True, False, None)
    assert body["ontomcp_version"] == __version__


# --- health ----------------------------------------------------------------


async def test_health(api):
    async with api(lambda r: httpx.Response(200)) as http:
        resp = await http.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["data"]["status"] == "ok"
    assert len(body["data"]["ontologies"]) == len(config.ONTOLOGIES)
    # ontology_versions is cache-only and empty until a term is fetched.
    assert body["data"]["ontology_versions"] == {}


# --- search ----------------------------------------------------------------


async def test_search(api):
    def handler(r):
        return _search_response([_term_doc(desc="end of cell life")])

    async with api(handler) as http:
        resp = await http.post("/search", json={"query": "cell death"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["data"][0]["curie"] == "GO:0008219"


async def test_search_ols_failure_502(api):
    async with api(lambda r: httpx.Response(500)) as http:
        resp = await http.post("/search", json={"query": "x"})
    assert resp.status_code == 502
    assert resp.json()["data"][0]["error"] == "search_failed"


# --- terms -----------------------------------------------------------------


async def test_get_term(api):
    async with api(lambda r: _terms_response([_term_doc()])) as http:
        resp = await http.get("/term/GO:0008219")
    assert resp.status_code == 200
    assert resp.json()["data"]["label"] == "cell death"


async def test_get_term_cached_flag(api):
    # First call fetches from OLS (cached=false); second is served from the cache
    # (cached=true). /health carries cached=null (not applicable).
    async with api(lambda r: _terms_response([_term_doc()])) as http:
        first = await http.get("/term/GO:0008219")
        second = await http.get("/term/GO:0008219")
        health = await http.get("/health")
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert health.json()["cached"] is None


async def test_get_term_bad_curie_400(api):
    async with api(lambda r: httpx.Response(200)) as http:
        resp = await http.get("/term/not-a-curie")
    assert resp.status_code == 400
    assert resp.json()["data"]["error"] == "bad_curie"


async def test_get_term_not_found_404(api):
    async with api(lambda r: httpx.Response(404)) as http:
        resp = await http.get("/term/GO:9999999")
    assert resp.status_code == 404
    assert resp.json()["data"]["error"] == "not_found"


async def test_validate(api):
    async with api(lambda r: _terms_response([_term_doc(obsolete=True)])) as http:
        resp = await http.get("/term/GO:0008219/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["is_obsolete"] is True
    assert body["data"]["is_current"] is False


async def test_ancestors(api):
    def handler(r):
        return _hier_response(["GO:0008150", "GO:0008152"])

    async with api(handler) as http:
        resp = await http.get("/term/GO:0008219/ancestors")
    assert resp.status_code == 200
    assert {a["curie"] for a in resp.json()["data"]} == {"GO:0008150", "GO:0008152"}


async def test_descendants_capped(api):
    many = [f"GO:{i:07d}" for i in range(60)]
    async with api(lambda r: _hier_response(many)) as http:
        resp = await http.get("/term/GO:0008150/descendants")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == config.DESCENDANTS_CAP


async def test_parents_direct(api):
    async with api(lambda r: _hier_response(["GO:0012501"])) as http:
        resp = await http.get("/term/GO:0006915/parents")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [p["curie"] for p in data] == ["GO:0012501"]
    assert data[0]["rel_type"] == "is_a"
    assert data[0]["depth"] == 1


async def test_children_capped(api):
    many = [f"GO:{i:07d}" for i in range(60)]
    async with api(lambda r: _hier_response(many)) as http:
        resp = await http.get("/term/GO:0008150/children")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == config.DESCENDANTS_CAP


# --- graph -----------------------------------------------------------------


async def test_graph(api):
    # One handler serves both the focus get_term fetch and the hierarchy fetches:
    # all parse from _embedded.terms, and a valid obo_id/label satisfies get_term.
    def handler(r):
        return _terms_response(
            [_term_doc(), {"obo_id": "GO:0008150", "label": "biological process"}]
        )

    async with api(handler) as http:
        resp = await http.get("/graph/GO:0008219")
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["data"]["focus_curie"] == "GO:0008219"


# --- bulk ------------------------------------------------------------------


async def test_bulk(api):
    def handler(r):
        return _search_response([_term_doc(label="cell death")])

    async with api(handler) as http:
        resp = await http.post("/bulk", json={"terms": ["cell death"]})
    assert resp.status_code == 200
    assert "results" in resp.json()["data"]


async def test_bulk_too_many_400(api):
    too_many = [f"t{i}" for i in range(config.BULK_MAX + 1)]
    async with api(lambda r: httpx.Response(200)) as http:
        resp = await http.post("/bulk", json={"terms": too_many})
    assert resp.status_code == 400
    assert resp.json()["data"]["error"] == "too_many_terms"


# --- suggest ---------------------------------------------------------------


async def test_suggest(api):
    async with api(lambda r: httpx.Response(200)) as http:
        resp = await http.post("/suggest", json={"context": "single cell RNA data"})
    assert resp.status_code == 200
    assert resp.json()["data"][0]["ontology"] == "CL"
