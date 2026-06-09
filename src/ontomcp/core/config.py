"""Single source of truth for ontology registry, OLS settings, paths, and caps.

Later phases import these constants — they must never be redefined elsewhere
(see project CLAUDE.md: "no ad-hoc string building").
"""

import os
from pathlib import Path

from ontomcp import __version__

# --- OLS API ---------------------------------------------------------------

OLS_BASE_URL = "https://www.ebi.ac.uk/ols4/api"
USER_AGENT = f"OntoMCP/{__version__}"

# OLS client retry/timeout. Retry only on 429 + 5xx; max 3 attempts.
OLS_TIMEOUT_SECONDS = 10.0
OLS_MAX_RETRIES = 3
OLS_BACKOFF_BASE = 0.5  # seconds; sleep = base * 2 ** (attempt - 1)
OLS_RETRY_STATUS = (429, 500, 502, 503, 504)

# --- Ontology registry (v1) ------------------------------------------------
# Free ontologies served via the EBI OLS4 API. No API key required.

# ``slug`` is the lowercase ontology id OLS uses in URL paths and the search
# filter. It usually equals the lowercased registry key, but not always (HPO ->
# "hp"), so it is stored explicitly here as the single source of truth.
ONTOLOGIES: dict[str, dict[str, str]] = {
    "GO": {"name": "Gene Ontology", "domain": "Gene function, biological processes", "slug": "go"},
    "MONDO": {"name": "Mondo Disease Ontology", "domain": "Disease", "slug": "mondo"},
    "HPO": {"name": "Human Phenotype Ontology", "domain": "Clinical phenotypes", "slug": "hp"},
    "CHEBI": {
        "name": "Chemical Entities of Biological Interest",
        "domain": "Small molecules, drugs",
        "slug": "chebi",
    },
    "UBERON": {
        "name": "Uberon Anatomy Ontology",
        "domain": "Cross-species anatomy",
        "slug": "uberon",
    },
    "CL": {"name": "Cell Ontology", "domain": "Cell types", "slug": "cl"},
    "EFO": {"name": "Experimental Factor Ontology", "domain": "Experimental design", "slug": "efo"},
    "MESH": {"name": "Medical Subject Headings", "domain": "Medical literature", "slug": "mesh"},
    "NCIT": {
        "name": "NCI Thesaurus",
        "domain": "Cancer, drugs, indications (pharma/oncology)",
        "slug": "ncit",
    },
    "DOID": {
        "name": "Human Disease Ontology",
        "domain": "Disease (the primary MONDO mapping target)",
        "slug": "doid",
    },
    "PR": {"name": "Protein Ontology", "domain": "Proteins, complexes, drug targets", "slug": "pr"},
}

# Full OBI IRI templates per ontology. CURIE id fills the {id} slot, then the
# result is double-URL-encoded when used as an OLS path parameter.
IRI_TEMPLATES: dict[str, str] = {
    "GO": "http://purl.obolibrary.org/obo/GO_{id}",
    "MONDO": "http://purl.obolibrary.org/obo/MONDO_{id}",
    "HPO": "http://purl.obolibrary.org/obo/HP_{id}",
    "CHEBI": "http://purl.obolibrary.org/obo/CHEBI_{id}",
    "UBERON": "http://purl.obolibrary.org/obo/UBERON_{id}",
    "CL": "http://purl.obolibrary.org/obo/CL_{id}",
    "EFO": "http://www.ebi.ac.uk/efo/EFO_{id}",
    "MESH": "http://id.nlm.nih.gov/mesh/{id}",
    "NCIT": "http://purl.obolibrary.org/obo/NCIT_{id}",
    "DOID": "http://purl.obolibrary.org/obo/DOID_{id}",
    "PR": "http://purl.obolibrary.org/obo/PR_{id}",
}

# --- Cache -----------------------------------------------------------------

# Override with ONTOMCP_DB_PATH; defaults to a file in the user's home dir.
# Expand ~ in the path (handles env vars like "~/.ontomcp/cache.db").
DB_PATH = Path(
    os.environ.get("ONTOMCP_DB_PATH", Path.home() / ".ontomcp" / "cache.db")
).expanduser()

# Re-fetch any term older than this. validate_term never uses the cache.
CACHE_TTL_DAYS = 7

# SQLite busy timeout (ms). Both servers write through the shared core, so a
# second writer must wait for the WAL write lock rather than fail immediately.
BUSY_TIMEOUT_MS = 5000

# --- Transport -------------------------------------------------------------
# stdio (default) serves Claude Desktop / Claude Code; sse serves GPT, Codex
# CLI, and remote MCP clients. Host/port apply only in sse mode. Default bind
# is loopback — set ONTOMCP_MCP_HOST=0.0.0.0 to expose on the network.

MCP_TRANSPORT = os.environ.get("ONTOMCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("ONTOMCP_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("ONTOMCP_MCP_PORT", "8001"))

# --- Payload caps ----------------------------------------------------------
# Enforced by the tools; keep ontology subgraphs small enough for chat payloads.

DESCENDANTS_CAP = 50  # get_descendants hard cap
GRAPH_NODE_CAP = 40  # get_term_graph hard cap
BULK_WARN = 100  # bulk_annotate: warn above this many input terms
BULK_MAX = 500  # bulk_annotate: hard error above this many input terms
SEARCH_LIMIT_MAX = 100  # search_terms: upper bound on requested result rows


# --- Search relevance score ------------------------------------------------


def rank_score(index: int, total: int) -> float:
    """Map a result's position in relevance order to a consistent 0–1 score.

    Both search paths (cache FTS and live OLS) return results best-first but on
    incomparable native scales (FTS bm25 vs. Solr score). To give callers a single
    comparable signal, ``search_terms`` reports this rank-normalized score instead:
    the top hit scores highest, decreasing monotonically, identical regardless of
    source. It is ordinal — a relative ranking within one result set, not an
    absolute match strength.

    ``index`` is 0-based; ``total`` is the result count. Returns 0.0 if total <= 0.
    """
    if total <= 0:
        return 0.0
    return round((total - index) / total, 3)
