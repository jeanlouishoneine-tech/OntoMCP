"""search_terms tool: FTS-first, OLS fallback, write results back to cache."""

import logging
from pathlib import Path

from ontomcp.core import cache
from ontomcp.core.config import DB_PATH, SEARCH_LIMIT_MAX
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, normalize_ontologies, ols_client

logger = logging.getLogger("ontomcp")


async def search_terms(
    query: str,
    ontologies: list[str] | None = None,
    limit: int = 10,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Search ontology terms by free text. Cache (FTS5) first, OLS on a miss.

    Returns ``(results, cache_hit)`` where results is
    ``[{curie, label, ontology, definition, score}, ...]`` and ``cache_hit`` is
    True when served from the FTS cache. On an OLS failure, returns the client's
    structured error list with ``cache_hit=False``. ``limit`` is clamped to
    ``[1, SEARCH_LIMIT_MAX]`` to keep payloads small.
    """
    onts = normalize_ontologies(ontologies)
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    hits = cache.fts_search(db_path, query, onts, limit)
    if hits:
        logger.debug("search_terms cache hit: %r (%d results)", query, len(hits))
        return hits, True

    logger.info("search_terms cache miss, fetching OLS: %r", query)
    async with ols_client(client) as cli:
        results = await cli.search(query, onts, limit)
    if is_error(results):
        return results, False

    for term in results:
        if not term.get("curie") or not term.get("label"):
            continue
        # Persist enough for future FTS hits; full record fills in via get_term.
        cache.put_term(
            db_path,
            {
                "curie": term["curie"],
                "ontology": term["ontology"],
                "label": term["label"],
                "definition": term.get("definition"),
            },
        )
    return results, False
