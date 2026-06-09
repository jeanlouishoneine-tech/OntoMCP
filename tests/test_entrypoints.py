"""Unit tests for the `run()` entrypoints in both servers.

The actual server-start calls (mcp.run / uvicorn.run) are mocked so nothing
binds a port; we assert the transport/flag/env selection logic only.
"""

import sys

import pytest

from ontomcp.api import main as api_main
from ontomcp.mcp_server import server as mcp_server


@pytest.fixture
def no_argv(monkeypatch):
    """Run with a bare argv so argparse sees no flags."""
    monkeypatch.setattr(sys, "argv", ["prog"])


# --- MCP server run() ------------------------------------------------------


def test_mcp_run_defaults_to_stdio(monkeypatch, no_argv):
    monkeypatch.delenv("ONTOMCP_TRANSPORT", raising=False)
    calls = {}
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.update(started=True, kwargs=kw))

    mcp_server.run()
    assert calls["started"] is True
    assert calls["kwargs"] == {}  # plain stdio, no transport kwargs


def test_mcp_run_sse_uses_host_and_port(monkeypatch, no_argv):
    monkeypatch.setenv("ONTOMCP_TRANSPORT", "sse")
    monkeypatch.setenv("ONTOMCP_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("ONTOMCP_MCP_PORT", "9999")
    calls = {}
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.update(kw))

    mcp_server.run()
    assert calls == {"transport": "sse", "host": "0.0.0.0", "port": 9999}


def test_mcp_run_db_path_flag_sets_env(monkeypatch):
    monkeypatch.delenv("ONTOMCP_TRANSPORT", raising=False)
    monkeypatch.delenv("ONTOMCP_DB_PATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog", "--db-path", "/tmp/x.db"])
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: None)

    mcp_server.run()
    import os

    assert os.environ["ONTOMCP_DB_PATH"] == "/tmp/x.db"


# --- API server run() ------------------------------------------------------


def test_api_run_uses_defaults(monkeypatch, no_argv):
    monkeypatch.delenv("ONTOMCP_API_HOST", raising=False)
    monkeypatch.delenv("ONTOMCP_API_PORT", raising=False)
    calls = {}
    # run() does `import uvicorn` internally, so patch the module in sys.modules.
    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn(calls))

    api_main.run()
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8000


def test_api_run_flag_beats_env(monkeypatch):
    monkeypatch.setenv("ONTOMCP_API_PORT", "7000")
    monkeypatch.setattr(sys, "argv", ["prog", "--port", "8123"])
    calls = {}
    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn(calls))

    api_main.run()
    assert calls["port"] == 8123  # CLI flag wins over env var


class _FakeUvicorn:
    """Stand-in for the uvicorn module imported inside api_main.run()."""

    def __init__(self, sink: dict):
        self._sink = sink

    def run(self, app, host, port):
        self._sink.update(app=app, host=host, port=port)
