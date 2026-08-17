"""FastMCP server exposing the 12 OntoMCP tools to any MCP client.

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
    # Only construct a client if none is already set — tests inject a mock-transport
    # client into `_client` before the lifespan runs, and this must not clobber it.
    owns_client = _client is None
    if owns_client:
        _client = OLSClient()
    try:
        yield
    finally:
        if owns_client:
            await _client.aclose()
            _client = None


mcp = FastMCP("OntoMCP", lifespan=_lifespan)


@mcp.tool()
async def search_terms(query: str, ontologies: list[str] | None = None, limit: int = 10):
    """
    Search for ontology terms by free text across biomedical ontologies.

    Use when a scientist mentions a biological concept, disease, cell type,
    chemical, or anatomical structure and you need the canonical ontology ID
    (CURIE) and definition. By default this searches the user's active research
    profile (or a broad built-in default set if no profile is configured) — you
    do not need to name ontologies yourself unless the user wants to restrict
    the search.

    The result carries a ``meta`` block alongside ``results``: read it and
    surface anything under ``meta.notice`` (e.g. profile scope suggestions),
    ``meta.held_back`` (profile members that exist but are excluded by default),
    or ``meta.filtered_out`` (ontologies that had matches but were dropped
    because they were minted outside the searched scope) to the user rather than
    silently dropping them.

    Args:
        query: Free-text search, e.g. "cell death", "lung cancer", "HEK293".
        ontologies: Restrict the search, e.g. ["GO", "MONDO"]. None searches the
            active profile's default scope.
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
async def get_profile_setup_guidance():
    """
    Get the rules and the valid-ontology list needed to author a research profile.

    Call this ONLY when the user is setting up or editing their research profile
    — not to pick ontologies for an ordinary search. It takes no arguments and
    performs no matching: it returns the curation policy, every valid ontology
    prefix with its title, and the profile rules. YOU propose 5-8 ontologies for
    the user's work using that list, show each proposal's prefix and title, and
    get an explicit yes before calling save_profile.

    For ordinary searching, just call search_terms — the user's profile already
    determines what is searched.
    """
    result, _ = tools.get_profile_setup_guidance()
    return result


@mcp.tool()
async def map_across_ontologies(curie: str, target_ontology: str):
    """
    Find the equivalent term for a CURIE in a different ontology.

    Use to translate a term from one ontology into another — e.g. mapping a MeSH
    disease to MONDO, or an EFO trait to its GO/HPO counterpart. Prefers exact
    cross-references; falls back to fuzzy label matching. ``target_ontology``
    must be a member of the user's active research profile — if it is not, this
    returns an ``out_of_profile`` error naming how to add it.

    Args:
        curie: Source ontology ID with uppercase prefix, e.g. "MESH:D008175".
        target_ontology: Target ontology code, e.g. "MONDO", "HPO", "CHEBI". Must
            be in the active profile.
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
        ontology_hint: Restrict matching to one ontology code, e.g. "CL". None
            searches every member of the active profile.
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
    nodes; descendants are trimmed first when over the limit. If a ``degraded``
    field is present, the parent or child lookup failed and the graph is
    partial — tell the user rather than presenting it as complete.

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


@mcp.tool()
async def save_profile(
    name: str,
    ontologies: list[str],
    force_default: list[str] | None = None,
    description: str = "",
):
    """
    Create or update the user's research profile, then freeze it.

    Call this ONLY after get_profile_setup_guidance, and ONLY once the human has
    explicitly approved the ontology list — show them each prefix WITH its title
    first. Validates every prefix, records each ontology's version and minting
    prefix, and takes effect immediately with no restart.

    Args:
        name: Profile name, lowercase and hyphenated, e.g. "plant-breeding".
        ontologies: 5-8 ontology prefixes (hard cap 12), e.g. ["PO", "TO", "GO"].
        force_default: Rare. Prefixes to search by default even though they are
            normally held back for noise, e.g. ["NCBITAXON"] for a taxonomy-heavy
            domain.
        description: One line on what this profile is for.
    """
    result, _ = await tools.save_profile(
        name,
        ontologies,
        force_default=force_default,
        description=description,
        client=_client,
    )
    return result


@mcp.prompt("setup-research-profile")
def setup_research_profile() -> str:
    """Set up a research profile so ontology searches match this user's domain."""
    return (
        "Help me set up my OntoMCP research profile.\n\n"
        "1. Ask me what I work on, in one or two sentences.\n"
        "2. Call get_profile_setup_guidance to get the rules and the list of valid "
        "ontologies.\n"
        "3. Propose 5-8 ontologies. For EACH one, show me the prefix AND its full "
        "title, and say in a few words why it fits my work.\n"
        "4. Wait for my explicit yes. Do not call save_profile before I approve the "
        "list — an ontology that exists is not necessarily the one I meant.\n"
        "5. Call save_profile with the approved list."
    )


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
