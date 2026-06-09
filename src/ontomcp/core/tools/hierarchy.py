"""hierarchy tools over the OLS graph.

Two distinct kinds of query, kept honest about what they return:

- ``get_parents`` / ``get_children`` use OLS ``/parents`` and ``/children``: the
  DIRECT (one-hop) ``subClassOf`` neighbours. These are true ``is_a`` edges, so
  they are the source of truth for the ``relationships`` table and graph building.
  They report ``depth=1``.
- ``get_ancestors`` / ``get_descendants`` use OLS ``/ancestors`` and
  ``/hierarchicalDescendants``: the full TRANSITIVE closure, flattened, mixing
  ``is_a`` and ``part_of``. OLS gives no per-node distance, so they report
  ``depth="transitive"`` and are NOT written to the relationships table — recording
  a transitive pair as a direct edge would fabricate the graph topology.
"""

import logging
from pathlib import Path

from ontomcp.core import cache
from ontomcp.core.config import DB_PATH, DESCENDANTS_CAP
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, ols_client, safe_normalize_curie

logger = logging.getLogger("ontomcp")


def _shape(nodes: list[dict], depth) -> list[dict]:
    """Project hierarchy nodes into the public ``{curie, label, depth, rel_type}`` shape."""
    return [
        {
            "curie": n["curie"],
            "label": n.get("label"),
            "depth": depth,
            "rel_type": n.get("rel_type", "is_a"),
        }
        for n in nodes
    ]


async def get_parents(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Return DIRECT parent (one-hop broader) terms of a CURIE.

    Each parent is stored as a true ``(parent, focus, "is_a")`` edge in the cache.
    Returns ``([{curie, label, depth, rel_type}, ...], cache_hit)`` (depth always
    1) or an OLS error list. ``cache_hit`` is always False — fetched live.
    """
    norm, err = safe_normalize_curie(curie)
    if norm is None:
        return [err or {}], False

    logger.info("get_parents fetching OLS: %s", norm)
    async with ols_client(client) as cli:
        nodes = await cli.fetch_parents(norm)
    if is_error(nodes):
        return nodes, False

    cache.put_relationships(db_path, [(n["curie"], norm, n.get("rel_type", "is_a")) for n in nodes])
    return _shape(nodes, depth=1), False


async def get_children(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Return DIRECT child (one-hop narrower) terms of a CURIE, capped at 50 nodes.

    Each child is stored as a true ``(focus, child, "is_a")`` edge. Returns
    ``([{curie, label, depth, rel_type}, ...], cache_hit)`` (depth always 1) or an
    OLS error list. ``cache_hit`` is always False — fetched live.
    """
    norm, err = safe_normalize_curie(curie)
    if norm is None:
        return [err or {}], False

    logger.info("get_children fetching OLS: %s", norm)
    async with ols_client(client) as cli:
        nodes = await cli.fetch_children(norm)
    if is_error(nodes):
        return nodes, False

    # Cap before persisting so the relationships table never holds edges we won't
    # surface — broad terms can have many direct children.
    nodes = nodes[:DESCENDANTS_CAP]
    cache.put_relationships(db_path, [(norm, n["curie"], n.get("rel_type", "is_a")) for n in nodes])
    return _shape(nodes, depth=1), False


async def get_ancestors(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Return the TRANSITIVE set of ancestor (broader) terms of a CURIE.

    This is the full ancestor closure (every broader term, any distance), flattened
    by OLS with no per-node distance, so ``depth`` is ``"transitive"`` and these are
    NOT written as direct edges (use ``get_parents`` for true one-hop edges).
    Returns ``([{curie, label, depth, rel_type}, ...], cache_hit)`` or an OLS error
    list. ``cache_hit`` is always False — fetched live.
    """
    norm, err = safe_normalize_curie(curie)
    if norm is None:
        return [err or {}], False

    logger.info("get_ancestors fetching OLS (transitive): %s", norm)
    async with ols_client(client) as cli:
        nodes = await cli.fetch_ancestors(norm)
    if is_error(nodes):
        return nodes, False

    return _shape(nodes, depth="transitive"), False


async def get_descendants(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Return the TRANSITIVE set of descendant (narrower) terms, capped at 50 nodes.

    This is the full descendant closure (every narrower term, any distance),
    flattened by OLS with no per-node distance, so ``depth`` is ``"transitive"`` and
    these are NOT written as direct edges (use ``get_children`` for true one-hop
    edges). The hard cap (DESCENDANTS_CAP) protects against terms with thousands of
    descendants. Returns ``([{curie, label, depth, rel_type}, ...], cache_hit)`` or
    an OLS error list. ``cache_hit`` is always False — fetched live.
    """
    norm, err = safe_normalize_curie(curie)
    if norm is None:
        return [err or {}], False

    logger.info("get_descendants fetching OLS (transitive): %s", norm)
    async with ols_client(client) as cli:
        nodes = await cli.fetch_descendants(norm)
    if is_error(nodes):
        return nodes, False

    nodes = nodes[:DESCENDANTS_CAP]
    return _shape(nodes, depth="transitive"), False
