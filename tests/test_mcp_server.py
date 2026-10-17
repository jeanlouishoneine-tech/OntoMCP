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
    "get_profile_setup_guidance",
    "map_across_ontologies",
    "bulk_annotate",
    "get_term_graph",
    "save_profile",
}


def test_tool_surface_is_thirteen():
    """The budget, asserted. 12 before Phase B; save_profile is the one addition,
    paid for by deleting the keyword matcher rather than by adding a thirteenth
    concept. A new tool must be a deliberate change to this number."""
    assert len(EXPECTED_TOOLS) == 13


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
    DB and override the server's shared ``_client`` with a mocked one *before*
    the client context is entered. The lifespan only constructs a client when
    none is already set, so this pre-set mock survives lifespan startup instead
    of being clobbered by it.
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
    assert any(hit["curie"] == "GO:0008219" for hit in data["results"])
    # The meta block rides on search_terms only, and always carries these three.
    assert set(data["meta"]) >= {"source", "profile", "scope"}


async def test_validate_term_hits_live(mcp_client):
    term = {"obo_id": "GO:0008219", "label": "cell death", "is_obsolete": False}
    handler = lambda r: _terms_response([term])  # noqa: E731
    async with mcp_client(handler) as client:
        result = await client.call_tool("validate_term", {"curie": "GO:0008219"})
    data = result.data
    assert data["curie"] == "GO:0008219"
    assert data["is_current"] is True
    assert data["is_obsolete"] is False


async def test_profile_setup_guidance_is_offline_and_takes_no_arguments(mcp_client):
    # Pure logic — no OLS call. Handler raises if hit, proving no network.
    def handler(request):
        raise AssertionError("get_profile_setup_guidance must not call OLS")

    async with mcp_client(handler) as client:
        result = await client.call_tool("get_profile_setup_guidance", {})
    payload = _tool_payload(result)
    assert payload["grounding_list"]["CL"] == "Cell Ontology"


async def test_no_tool_accepts_a_research_description(mcp_client):
    # The objective's one prohibited behaviour: inferring ontologies from prose.
    async with mcp_client(lambda r: None) as client:
        tools = await client.list_tools()
    for tool in tools:
        params = set((tool.inputSchema or {}).get("properties") or {})
        assert "context" not in params, f"{tool.name} still accepts a research description"


# --- S-7: the setup prompt, over the wire ----------------------------------


async def test_setup_prompt_is_listed_over_the_protocol(mcp_client):
    """S-7, closed as far as our side of the wire reaches.

    Whether Claude Desktop *renders* a prompt is the client's business and cannot
    be asserted here. What can be asserted is the server contract it reads —
    `prompts/list` — because that is where any bug of ours would actually live.
    """
    async with mcp_client(lambda r: None) as client:
        prompts = await client.list_prompts()

    by_name = {p.name: p for p in prompts}
    assert "setup-research-profile" in by_name
    assert by_name["setup-research-profile"].description, "a prompt with no description is unusable"


async def test_setup_prompt_renders_the_anti_hallucination_gate(mcp_client):
    """`prompts/get` must return content that actually instructs the flow.

    A prompt that lists but renders empty is worse than no prompt: the user picks
    it and nothing happens. The human-approval step is the one instruction that
    must survive, because an index existence check proves PO is a real prefix, not
    that the user meant Plant Ontology.
    """
    async with mcp_client(lambda r: None) as client:
        result = await client.get_prompt("setup-research-profile")

    assert result.messages, "prompts/get returned no messages"
    text = " ".join(
        m.content.text for m in result.messages if getattr(m.content, "text", None)
    ).lower()
    assert text.strip()
    assert "get_profile_setup_guidance" in text
    assert "save_profile" in text
    assert "title" in text, "the model must be told to show titles, not just prefixes"
    assert "approve" in text or "yes" in text, "the human-approval gate must survive"


async def test_the_prompt_costs_no_tool_schema_budget(mcp_client):
    """The prompt is why the setup flow is discoverable without a 14th tool."""
    async with mcp_client(lambda r: None) as client:
        tools = await client.list_tools()
        prompts = await client.list_prompts()

    assert len(tools) == 13
    assert len(prompts) >= 1
    assert not any(t.name == "setup-research-profile" for t in tools)
