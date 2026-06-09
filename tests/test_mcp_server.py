"""FastMCP server tests. OLS is mocked — no network (CLAUDE.md).

The server is driven in-process via FastMCP's in-memory ``Client`` (no stdio
transport). Each test injects a MockTransport-backed OLS client as the server's
shared client and points its DB_PATH at an isolated temp DB.
"""

import json

import httpx
import pytest
from fastmcp import Client

from ontomcp.core import cache, config
from ontomcp.core.ols_client import OLSClient
from ontomcp.mcp_server import server


# The 10 tools that must be registered (plan.md §"The 10 Tools").
def _tool_payload(result):
    """Return a tool's payload.

    FastMCP exposes dict returns via ``result.data``; list returns arrive only
    as JSON text in the content block, so fall back to parsing that.
    """
    if result.data is not None:
        return result.data
    return json.loads(result.content[0].text)


EXPECTED_TOOLS = {
    "search_terms",
    "get_term",
    "find_synonyms",
    "validate_term",
    "get_parents",
    "get_children",
    "get_ancestors",
    "get_descendants",
    "suggest_ontology",
    "map_across_ontologies",
    "bulk_annotate",
    "get_term_graph",
}


def _ols_client(handler) -> OLSClient:
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


@pytest.fixture
def mcp_client(tmp_db_path, monkeypatch):
    """Return ``make_client(handler)`` bound to the server on an isolated temp DB.

    The lifespan would create a real OLSClient, so we point DB_PATH at the temp
    DB and override the server's shared ``_client`` with a mocked one *inside*
    the client context (after the lifespan has run).
    """
    cache.init_db(tmp_db_path)
    monkeypatch.setattr(server, "DB_PATH", tmp_db_path)

    def make_client(handler) -> Client:
        client = Client(server.mcp)
        # Patch the shared OLS client used by every tool call.
        monkeypatch.setattr(server, "_client", _ols_client(handler))
        return client

    return make_client


async def test_all_tools_registered(mcp_client):
    async with mcp_client(lambda r: httpx.Response(200)) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_search_terms_returns_mocked_hit(mcp_client):
    docs = [
        {
            "obo_id": "GO:0008219",
            "label": "cell death",
            "ontology_prefix": "GO",
            "description": ["a process"],
        }
    ]
    handler = lambda r: _search_response(docs)  # noqa: E731
    async with mcp_client(handler) as client:
        result = await client.call_tool("search_terms", {"query": "cell death"})
    data = _tool_payload(result)
    assert any(hit["curie"] == "GO:0008219" for hit in data)


async def test_validate_term_hits_live(mcp_client):
    term = {"obo_id": "GO:0008219", "label": "cell death", "is_obsolete": False}
    handler = lambda r: _terms_response([term])  # noqa: E731
    async with mcp_client(handler) as client:
        result = await client.call_tool("validate_term", {"curie": "GO:0008219"})
    data = result.data
    assert data["curie"] == "GO:0008219"
    assert data["is_current"] is True
    assert data["is_obsolete"] is False


async def test_suggest_ontology_is_offline(mcp_client):
    # Pure logic — no OLS call. Handler raises if hit, proving no network.
    def handler(request):
        raise AssertionError("suggest_ontology must not call OLS")

    async with mcp_client(handler) as client:
        result = await client.call_tool(
            "suggest_ontology", {"context": "single-cell RNA-seq of immune cells"}
        )
    codes = {entry["ontology"] for entry in _tool_payload(result)}
    assert "CL" in codes
