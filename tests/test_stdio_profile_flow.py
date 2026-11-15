"""End to end over stdio — how Claude Desktop and Codex launch the server.

Spawns the real MCP server as a subprocess and drives the whole profile flow
through it: save, view, edit, undo, with the client confirming in-app. Offline:
the launcher injects a MockTransport OLS client before ``mcp.run()``, the same
injection point the in-memory tests use.
"""

import os
import sys

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StdioTransport

from ontomcp.core import profile as profile_mod
from ontomcp.core import registry

LAUNCHER = """
import httpx
from ontomcp.core import config
from ontomcp.core.ols_client import OLSClient
from ontomcp.mcp_server import server

def handler(request):
    if "/terms" in str(request.url):
        return httpx.Response(200, json={"_embedded": {"terms": []}})
    return httpx.Response(200, json={"version": "2026-07-26"})

server._client = OLSClient(client=httpx.AsyncClient(
    base_url=config.OLS_BASE_URL, transport=httpx.MockTransport(handler)))
server.mcp.run()
"""


@pytest.fixture
def stdio_env(tmp_path, monkeypatch):
    profiles = str(tmp_path / "profiles")
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", profiles)
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ONTOMCP_PROFILE", "ONTOMCP_ONTOLOGIES", "ONTOMCP_REQUIRE_CONFIRMATION"}
    }
    env.update(ONTOMCP_PROFILE_DIR=profiles, ONTOMCP_DB_PATH=str(tmp_path / "cache.db"))
    return env


async def test_profile_flow_over_stdio(stdio_env):
    seen: list[str] = []

    async def accept(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept")

    transport = StdioTransport(command=sys.executable, args=["-c", LAUNCHER], env=stdio_env)
    async with Client(transport, elicitation_handler=accept) as client:

        async def call(tool, args):
            result = await client.call_tool(tool, args)
            return result.data

        saved = await call("save_profile", {"name": "plant", "ontologies": ["PO", "TO", "ENVO"]})
        assert (saved["status"], saved["confirmation"]) == ("saved", "elicitation")
        assert registry.current().profile == "plant"

        shown = await call("get_profile", {})
        assert {m["ontology"] for m in shown["members"]} == {"PO", "TO", "ENVO"}

        edited = await call("update_profile", {"add": ["NCBITaxon"], "remove": ["ENVO"]})
        assert edited["status"] == "saved"
        assert "Adding:\n  NCBITAXON: " in seen[-1] and "Removing:\n  ENVO: " in seen[-1]
        assert set(profile_mod.current_ontologies("plant")) == {"PO", "TO", "NCBITAXON"}

        undone = await call("update_profile", {"reset": "previous"})
        assert undone["status"] == "restored"
        assert set(profile_mod.current_ontologies("plant")) == {"PO", "TO", "ENVO"}

    assert len(seen) == 3  # one in-client confirmation per write, none for the read
