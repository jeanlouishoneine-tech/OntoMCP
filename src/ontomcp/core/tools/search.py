"""search_terms: cache-first search, with the scope and provenance the model needs.

Three things happen here that did not before, and each closes a defect:

1. **A coverage ledger decides cache hits**, not "FTS returned something". The old
   ``if hits: return hits`` served a three-row partial answer as complete for seven
   days.
2. **Over-fetch before the minting-prefix filter**, so the filter cannot shrink a
   result set below the requested limit, and ``meta.filtered_out`` names what it
   dropped. Silence was the filter's real problem, not the filtering.
3. **A meta block rides on this tool only.** A model plans from append-only
   context, so scope must be re-stated or it drifts — but that argument is about
   *search*. The other twelve tools resolve a CURIE the caller already holds and
   have no scope to misreport, so making them all carry it would be a context tax
   paid on every call forever.
"""

import logging
from pathlib import Path

from ontomcp.core import cache, registry
from ontomcp.core.config import DB_PATH, SEARCH_LIMIT_MAX
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import escape_hatch
from ontomcp.core.tools._common import (
    build_meta,
    is_error,
    normalize_ontologies,
    ols_client,
)

logger = logging.getLogger("ontomcp")

# How far to over-fetch when a post-filter will drop rows. Bounded at 100 so a
# widening query cannot turn one search into an unbounded fetch.
_OVERFETCH_FACTOR = 3
_OVERFETCH_CAP = 100


def _resolved():
    """The active registry, or None if the index is unreadable. Never raises."""
    try:
        return registry.current()
    except Exception:
        logger.warning(
            "search_terms: registry unreadable, falling back to no default scope", exc_info=True
        )
        return None


def _collapse(rows: list[dict]) -> list[dict]:
    """Drop duplicate CURIEs, keeping the best-ranked occurrence. Always lossless.

    OLS returns the same concept once per ontology that imports it, so an
    unfiltered search over a heavy importer returns each concept three to five
    times. This is a genuine dedup and it runs on every path — unlike the
    minting-prefix filter, which narrows and therefore only runs when asked.
    """
    seen: dict[str, dict] = {}
    for row in rows:
        curie = row.get("curie")
        if curie and curie not in seen:
            seen[curie] = row
    return list(seen.values())


def _minting_prefix(row: dict) -> str:
    """The ontology that MINTED this term, not the one that indexed it."""
    curie = row.get("curie") or ""
    return registry.canonical_key(curie.split(":", 1)[0]) if ":" in curie else ""


async def search_terms(
    query: str,
    ontologies: list[str] | None = None,
    limit: int = 10,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict | list, bool]:
    """Search ontology terms by free text. Cache (FTS5) first, OLS on a miss.

    Returns ``({"results": [...], "meta": {...}}, cache_hit)``. Each result is
    ``{curie, label, ontology, definition, is_obsolete, rank, iri}``. ``rank`` is
    the 1-based ordinal position — there is no ``score``, because a value computed
    from result-set size alone carried no query information and made a weak
    three-row cache result and a strong ten-row OLS result both report 1.0.

    On a rejected scope or an OLS failure, returns the structured error list with
    ``cache_hit=False``.
    """
    onts, rejected = normalize_ontologies(ontologies)
    if rejected:
        # A rejected code never widens scope, and never silently narrows it either.
        logger.warning("search_terms: rejected scope %s", rejected)
        return rejected, False

    resolved = _resolved()
    explicit_scope = onts is not None
    if not explicit_scope:
        if resolved is None or (not resolved.all_members and resolved.warnings):
            # A genuinely broken registry (unreadable index, `_resolved()` itself
            # raising, or a profile with zero resolvable members), not a
            # legitimately empty scope. Without this check the search below goes
            # out unscoped (widens to all of OLS4) and the minting-prefix filter
            # then drops every row against an empty `wanted` set — a real
            # failure returned as a success-shaped empty answer. Refuse instead
            # of guessing.
            detail = "; ".join(resolved.warnings) if resolved else "registry unreadable"
            logger.error("search_terms: registry unresolvable, refusing: %s", detail)
            return {
                "error": "registry_unavailable",
                "detail": detail,
                "remediation": "Run: python scripts/refresh_index.py, or ask me to check your profile.",
            }, False
        # F2 — an unfiltered search must go out scoped to the resolved default.
        # The ontology param used to be set only when the list was truthy, so
        # "search my ontologies" actually hit all ~286 of OLS4.
        onts = list(resolved.default_scope)

    limit = max(1, min(limit, SEARCH_LIMIT_MAX))
    query_norm = " ".join(query.lower().split())
    if not query_norm:
        return {"error": "bad_query", "detail": "query must not be empty", "query": query}, False
    key = cache.scope_key(onts, resolved.frozen_at if resolved else None)

    hits = cache.fts_search(db_path, query, onts, limit, query_norm=query_norm, scope_key=key)
    if cache.query_is_covered(db_path, query_norm, key, limit, len(hits)):
        logger.debug("search_terms cache hit: %r (%d rows)", query, len(hits))
        return _envelope(_collapse(hits)[:limit], resolved, onts, "cache", []), True

    logger.info("search_terms cache miss, fetching OLS: %r", query)
    # Over-fetch unconditionally: the minting-prefix filter below now narrows on
    # every path, and _collapse()'s dedup also runs on every path (OLS returns
    # the same concept once per importing ontology), so an unscoped search can
    # shrink below `limit` from dedup and filtering alone if not over-fetched.
    rows = min(limit * _OVERFETCH_FACTOR, _OVERFETCH_CAP)
    async with ols_client(client) as cli:
        results = await cli.search(query, onts, limit, rows=rows)
    if is_error(results):
        logger.warning("search_terms: OLS search failed: %s", results)
        return results, False

    # Collapse first — lossless, and it must happen before the write-back so the
    # cache is not warmed with duplicates.
    results = _collapse(results)

    # Write back BEFORE filtering: an EFO-scoped search that surfaces a MONDO-minted
    # term should still warm that term, even though this response will not show it.
    cache.put_search_stub(db_path, results)

    # Synonyms were fetched for the cache, not for the caller. Dropping them here
    # is the whole reason it is safe to request them: CHEBI:15365 alone carries 21,
    # so ten rows would add several hundred strings to a payload the model has no
    # use for. The cache keeps them; the response does not carry them.
    for row in results:
        row.pop("synonyms", None)

    # Runs on every path, not just an explicitly-scoped one: OLS returns
    # foreign-minted imports on the default scope too, and a default-scope search
    # must not silently widen past what meta.scope claims (see the coverage-ledger
    # note above — a filtered response's ontology column must also agree with
    # what the next read's scope filter looks for, or the cache never converges).
    filtered_out: list[str] = []
    wanted = set(onts)
    kept = []
    for row in results:
        prefix = _minting_prefix(row)
        if prefix in wanted:
            kept.append(row)
        elif prefix and prefix not in filtered_out:
            filtered_out.append(prefix)
    results = kept

    results = results[:limit]
    for position, row in enumerate(results, start=1):
        row["rank"] = position

    # A zero-result answer is not coverage: recording it would let
    # query_is_covered() serve "source": "cache" for a query the cache holds
    # nothing for, and would suppress the escape hatch below (it only fires on
    # this OLS branch) for the full cache TTL after the first genuinely-empty call.
    if results:
        cache.record_query(
            db_path, query_norm, key, limit, len(results), [row["curie"] for row in results]
        )
    envelope = _envelope(results, resolved, onts, "ols", filtered_out)

    if not results and resolved and escape_hatch.enabled():
        await _offer_a_way_out(query, envelope, resolved, explicit_scope, client)
    return envelope, False


async def _offer_a_way_out(query, envelope, resolved, explicit_scope, client) -> None:
    """Attach escape-hatch guidance to an empty result. Mutates ``envelope['meta']``.

    Only on genuinely zero results, and only when the user did not name a scope
    themselves — if they asked for CHEBI specifically, "try FOODON" is not an
    answer to their question.
    """
    if explicit_scope:
        return

    default_scope = set(resolved.default_scope)
    notices: list[str] = []

    async with ols_client(client) as cli:
        # Stage 1: re-run over default ∪ named_only and partition locally. One
        # extra call only when the profile actually holds something back.
        if resolved.named_only:
            widened = await cli.search(query, list(resolved.all_members), 20)
            if not is_error(widened):
                _, held = escape_hatch.partition(
                    _collapse(widened), default_scope, set(resolved.named_only)
                )
                if held:
                    notices.append(
                        escape_hatch.stage_one(query, held, list(resolved.default_scope))
                    )
                    envelope["meta"]["notice"] = list(envelope["meta"].get("notice", [])) + notices
                    return  # Stage 1 answered; Stage 2 never speaks.

        # Stage 2: one call, exact per-ontology counts, no terms.
        counts = await cli.search_facets(query)
    if not counts:
        logger.info("escape hatch: no facet coverage anywhere for %r", query)
        return
    try:
        index = registry.load_index()
    except Exception:
        return
    suggestion = escape_hatch.stage_two(query, counts, set(resolved.all_members), index)
    if suggestion:
        envelope["meta"]["elsewhere"] = suggestion


def _envelope(results, resolved, scope, source, filtered_out) -> dict:
    """Attach the meta block. One helper builds it; nothing else constructs this dict."""
    return {
        "results": results,
        "meta": build_meta(resolved, scope, source=source, filtered_out=filtered_out),
    }
