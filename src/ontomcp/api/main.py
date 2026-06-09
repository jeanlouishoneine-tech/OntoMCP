"""FastAPI app factory and uvicorn entrypoint.

Thin HTTP surface over the core tools (project CLAUDE.md: no business logic here).
The cache DB is initialised once on startup and a single OLS client is shared
across all requests via ``app.state``.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ontomcp.api.routes import bulk, graph, health, search, suggest, terms
from ontomcp.core import cache, config
from ontomcp.core.logging import configure_logging
from ontomcp.core.ols_client import OLSClient

logger = logging.getLogger("ontomcp")


def _resolve_db_path() -> Path:
    """Re-read the DB path from the env so a late-set ONTOMCP_DB_PATH is honored.

    The CLI sets the env var before the server boots; reading it here (rather than
    trusting the import-time ``config.DB_PATH``) lets ``--db-path`` take effect.
    """
    raw = os.environ.get("ONTOMCP_DB_PATH")
    return Path(raw).expanduser() if raw else config.DB_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    db_path = _resolve_db_path()
    app.state.db_path = db_path
    cache.init_db(db_path)
    logger.info("OntoMCP API ready (db=%s)", db_path)
    app.state.ols_client = OLSClient()
    try:
        yield
    finally:
        await app.state.ols_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="OntoMCP API", lifespan=lifespan)

    # Allow localhost origins so the Jupyter extension (Phase 7) can call the API.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (health, search, terms, graph, bulk, suggest):
        app.include_router(module.router)

    return app


app = create_app()


def run() -> None:
    """uvicorn entrypoint for the `ontomcp-api` script.

    Precedence for each setting: CLI flag > environment variable > default.
    ``--db-path`` is exported to ``ONTOMCP_DB_PATH`` so the app's lifespan (and
    thus the cache) picks it up when the server boots.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ontomcp-api", description="OntoMCP HTTP API server")
    parser.add_argument("--host", default=os.environ.get("ONTOMCP_API_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ONTOMCP_API_PORT", "8000")),
    )
    parser.add_argument("--db-path", default=None, help="SQLite cache file path")
    args = parser.parse_args()

    if args.db_path:
        os.environ["ONTOMCP_DB_PATH"] = args.db_path

    configure_logging()
    logger.info("Starting OntoMCP API on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)
