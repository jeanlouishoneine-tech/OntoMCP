"""FastMCP server exposing the 10 OntoMCP tools to any MCP client.

Transport is client-agnostic: stdio (default — Claude Desktop / Claude Code) or
SSE (GPT, Codex CLI, remote clients), selected at startup by ``ONTOMCP_TRANSPORT``.
See ``run()`` below.

This is a thin wrapper: every tool delegates to the matching function in
``ontomcp.core.tools``. No business logic lives here (project rule — logic
belongs in ``core/``). The docstrings below are the contract the client reads to
decide which tool to call, so they describe *when* to use each tool.

The cache DB is initialised once and a single shared ``OLSClient`` is held for
the process lifetime, mirroring the FastAPI lifespan in ``api/main.py``.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP

from ontomcp.core import cache, config, tools
from ontomcp.core.logging import configure_logging
from ontomcp.core.ols_client import OLSClient

logger = logging.getLogger("ontomcp")

# Process-lifetime shared OLS client, created in the lifespan below. Tests may
# replace this directly to inject a mock-transport client.
_client: OLSClient | None = None

# Resolved at lifespan start so a late-set ONTOMCP_DB_PATH (from --db-path) wins.
# Falls back to the import-time config default until then.
DB_PATH: Path = config.DB_PATH


def _resolve_db_path() -> Path:
    """Re-read ONTOMCP_DB_PATH if set; otherwise keep the current module DB_PATH.

    Honoring an env var lets ``--db-path`` take effect at boot, while leaving the
    existing value untouched preserves test monkeypatching of ``DB_PATH``.
    """
    raw = os.environ.get("ONTOMCP_DB_PATH")
    return Path(raw).expanduser() if raw else DB_PATH


@asynccontextmanager
async def _lifespan(server: FastMCP):
    """Init the cache schema and hold one shared OLS client for the process."""
    global _client, DB_PATH
    configure_logging()
    DB_PATH = _resolve_db_path()
    cache.init_db(DB_PATH)
    logger.info("OntoMCP MCP server ready (db=%s)", DB_PATH)
    _client = OLSClient()
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


mcp = FastMCP("OntoMCP", lifespan=_lifespan)


@mcp.tool()
async def search_terms(query: str, ontologies: list[str] | None = None, limit: int = 10):
    """
    Search for ontology terms by free text across biomedical ontologies.

    Use when a scientist mentions a biological concept, disease, cell type,
    chemical, or anatomical structure and you need the canonical ontology ID
    (CURIE) and definition. Searches GO, MONDO, HPO, ChEBI, UBERON, CL, EFO, and
    MeSH by default.

    Args:
        query: Free-text search, e.g. "cell death", "lung cancer", "HEK293".
        ontologies: Restrict the search, e.g. ["GO", "MONDO"]. None searches all 8.
        limit: Max results to return (default 10).
    """
    result, _ = await tools.search_terms(query, ontologies, limit, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def get_term(curie: str):
    """
    Fetch the full record for one ontology term by its CURIE.

    Use when you already have a CURIE (e.g. from search_terms) and need its
    label, definition, synonyms, cross-references, and obsolescence status.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "GO:0008219", "MONDO:0005233".
    """
    result, _ = await tools.get_term(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def find_synonyms(curie: str):
    """
    List the synonyms of an ontology term, grouped by relation type.

    Use when a user's wording may not match the canonical label and you want
    alternative names — exact, related, narrow, and broad synonyms.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "CHEBI:15377".
    """
    result, _ = await tools.find_synonyms(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def validate_term(curie: str):
    """
    Check whether a CURIE is current, obsolete, or has been replaced.

    Call before trusting any CURIE a user pasted or that came from an older
    document, to confirm it isn't deprecated. This always checks the live
    ontology source (never a cache) so the deprecation status is never stale.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "GO:0008219".
    """
    result, _ = await tools.validate_term(curie, client=_client)
    return result


@mcp.tool()
async def get_parents(curie: str):
    """
    Get the DIRECT parent (one step broader) terms of an ontology term.

    Use when you need the immediate "is a kind of" parent(s) — exactly one hop up,
    not the whole chain to the root. Each result is a true direct ``is_a`` edge.
    For the full set of broader terms at any distance, use get_ancestors instead.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "CL:0000236".
    """
    result, _ = await tools.get_parents(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def get_children(curie: str):
    """
    Get the DIRECT child (one step narrower) terms of an ontology term.

    Use when you need the immediate subtypes — exactly one hop down. Each result
    is a true direct ``is_a`` edge. For every narrower term at any distance, use
    get_descendants instead. Capped at 50 nodes.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "MONDO:0005233".
    """
    result, _ = await tools.get_children(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def get_ancestors(curie: str):
    """
    Get ALL ancestor (broader) terms of an ontology term, at any distance.

    Use to find the general categories above a term — e.g. to learn that a
    specific cell type is ultimately a kind of leukocyte, or a disease belongs to
    a broader disease family. Returns the full transitive set of broader terms
    (not just the immediate parent — use get_parents for the one-hop relation).

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "CL:0000236".
    """
    result, _ = await tools.get_ancestors(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
async def get_descendants(curie: str):
    """
    Get ALL descendant (narrower) terms of an ontology term, at any distance.

    Use to enumerate the more specific subtypes below a term — e.g. every kind of
    a given cell type or every subtype of a disease. Returns the full transitive
    set (use get_children for just the immediate subtypes). Capped at 50 nodes
    because broad terms can have thousands of descendants.

    Args:
        curie: Ontology ID with uppercase prefix, e.g. "MONDO:0005233".
    """
    result, _ = await tools.get_descendants(curie, db_path=DB_PATH, client=_client)
    return result


@mcp.tool()
def suggest_ontology(context: str):
    """
    Recommend which ontologies fit a research context, with rationale.

    Use at the start of an annotation task when the user describes their data or
    domain but hasn't picked an ontology — e.g. "single-cell RNA-seq of immune
    cells" or "adverse drug events". Returns ranked ontologies with example
    terms. Pure local reasoning — no lookup is performed.

    Args:
        context: A short description of the research domain or dataset.
    """
    result, _ = tools.suggest_ontology(context)
    return result


@mcp.tool()
async def map_across_ontologies(curie: str, target_ontology: str):
    """
    Find the equivalent term for a CURIE in a different ontology.

    Use to translate a term from one ontology into another — e.g. mapping a MeSH
    disease to MONDO, or an EFO trait to its GO/HPO counterpart. Prefers exact
    cross-references; falls back to fuzzy label matching.

    Args:
        curie: Source ontology ID with uppercase prefix, e.g. "MESH:D008175".
        target_ontology: Target ontology code, e.g. "MONDO", "HPO", "CHEBI".
    """
    result, _ = await tools.map_across_ontologies(
        curie, target_ontology, db_path=DB_PATH, client=_client
    )
    return result


@mcp.tool()
async def bulk_annotate(terms: list[str], ontology_hint: str | None = None, threshold: float = 0.8):
    """
    Map a list of free-text strings to their best-matching ontology terms.

    Use to annotate a column of labels (e.g. cell-type names, disease strings)
    in one call. Returns a best match plus alternatives for each input. Hard
    limit of 500 inputs; a warning is returned above 100.

    Args:
        terms: The strings to annotate, e.g. ["T cell", "B cell", "macrophage"].
        ontology_hint: Restrict matching to one ontology code, e.g. "CL". None searches all.
        threshold: Minimum fuzzy-match score (0–1) for a confident best match (default 0.8).
    """
    result, _ = await tools.bulk_annotate(
        terms, ontology_hint, threshold, db_path=DB_PATH, client=_client
    )
    return result


@mcp.tool()
async def get_term_graph(curie: str, include_siblings: bool = True):
    """
    Build a small relationship graph around a term, for visualization.

    Use when the user asks to *see*, *visualize*, or understand the
    *relationships* around a term — its ancestors, descendants, and siblings.
    Returns nodes (with roles) and edges suitable for rendering. Capped at 40
    nodes; descendants are trimmed first when over the limit.

    Args:
        curie: Focus term's ontology ID with uppercase prefix, e.g. "GO:0008219".
        include_siblings: Whether to include sibling terms (default True).
    """
    result, _ = await tools.get_term_graph(
        curie,
        include_siblings,
        db_path=DB_PATH,
        client=_client,
    )
    return result


def run() -> None:
    """Entrypoint for the `ontomcp-mcp` script.

    Transport is selected by ``ONTOMCP_TRANSPORT`` (default ``stdio`` for Claude
    Desktop / Claude Code; set ``sse`` for GPT, Codex CLI, and remote MCP clients).
    In SSE mode the bind address/port come from ``ONTOMCP_MCP_HOST`` (default
    ``127.0.0.1``) and ``ONTOMCP_MCP_PORT`` (default ``8001``).

    ``--db-path`` (flag) overrides ``ONTOMCP_DB_PATH`` (env); it is exported so
    the lifespan resolves it when the server boots.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="ontomcp-mcp", description="OntoMCP MCP server")
    parser.add_argument("--db-path", default=None, help="SQLite cache file path")
    args = parser.parse_args()

    if args.db_path:
        os.environ["ONTOMCP_DB_PATH"] = args.db_path

    transport = os.getenv("ONTOMCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        host = os.getenv("ONTOMCP_MCP_HOST", "127.0.0.1")
        port = int(os.getenv("ONTOMCP_MCP_PORT", "8001"))
        mcp.run(transport="sse", host=host, port=port)
    else:
        mcp.run()  # stdio — unchanged default for Claude
