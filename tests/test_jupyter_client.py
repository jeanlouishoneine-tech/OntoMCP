"""Unit tests for the synchronous Jupyter HTTP client. No network.

The client is driven via ``httpx.MockTransport`` (sync), which replays the API's
response envelope. This verifies request construction, ``data`` extraction, tool
-error pass-through, CURIE path-encoding, and the unreachable-server path —
without a live server. (httpx's ASGITransport is async-only, so the in-process
ASGI app can't back a sync client; the route layer is covered by test_api.py.)
"""

import httpx
import pytest

from ontomcp import __version__
from ontomcp.jupyter_ext.client import OntoMCPClient, OntoMCPConnectionError


def _envelope(data):
    return {"data": data, "cached": None, "ontomcp_version": __version__}


def _make_client(handler):
    """OntoMCPClient backed by a MockTransport handler."""
    return OntoMCPClient(transport=httpx.MockTransport(handler))


def test_health():
    data = {"status": "ok", "version": __version__, "ontologies": ["GO"] * 8}
    with _make_client(lambda r: httpx.Response(200, json=_envelope(data))) as c:
        result = c.health()
    assert result["status"] == "ok"
    assert len(result["ontologies"]) == 8


def test_search_happy_path():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content
        docs = [{"curie": "GO:0008219", "label": "cell death", "ontology": "GO"}]
        return httpx.Response(200, json=_envelope(docs))

    with _make_client(handler) as c:
        result = c.search("cell death", ontologies=["GO"], limit=5)

    assert result[0]["curie"] == "GO:0008219"
    assert captured["url"].endswith("/search")
    assert b"cell death" in captured["body"]


def test_tool_error_is_returned_not_raised():
    """A 4xx with an error dict inside the envelope is returned, not raised."""
    err = {"error": "bad_curie", "detail": "nope", "curie": "x"}
    with _make_client(lambda r: httpx.Response(400, json=_envelope(err))) as c:
        result = c.get_term("not-a-curie")
    assert result["error"] == "bad_curie"


def test_graph_returns_nodes_and_edges():
    data = {
        "nodes": [
            {"curie": "GO:0008219", "label": "cell death", "ontology": "GO", "role": "focus"}
        ],
        "edges": [{"source": "GO:0008219", "target": "GO:0008150", "rel_type": "is_a"}],
        "focus_curie": "GO:0008219",
    }
    with _make_client(lambda r: httpx.Response(200, json=_envelope(data))) as c:
        result = c.graph("GO:0008219")
    assert result["focus_curie"] == "GO:0008219"
    assert result["nodes"][0]["role"] == "focus"
    assert result["edges"][0]["rel_type"] == "is_a"


def test_bulk_shape():
    data = {"results": [{"input": "B cell", "best_match": None, "alternatives": []}]}
    with _make_client(lambda r: httpx.Response(200, json=_envelope(data))) as c:
        result = c.bulk(["B cell"], ontology_hint="CL")
    assert "results" in result


def test_curie_is_path_encoded():
    captured = {}

    def handler(request):
        captured["raw_path"] = request.url.raw_path
        return httpx.Response(200, json=_envelope({}))

    with _make_client(handler) as c:
        c.get_term("GO:0008219")
    assert b"GO%3A0008219" in captured["raw_path"]


def test_unreachable_server_raises_connection_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with _make_client(handler) as c:
        with pytest.raises(OntoMCPConnectionError, match="ontomcp-api"):
            c.health()


def test_missing_envelope_raises_runtime_error():
    with _make_client(lambda r: httpx.Response(200, json={"oops": 1})) as c:
        with pytest.raises(RuntimeError, match="envelope"):
            c.health()
