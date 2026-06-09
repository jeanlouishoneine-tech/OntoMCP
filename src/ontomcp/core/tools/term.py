"""term tools: get_term (cache-first), find_synonyms, validate_term (always live)."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from ontomcp.core import cache
from ontomcp.core.config import DB_PATH
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, ols_client, safe_normalize_curie

logger = logging.getLogger("ontomcp")

# Subset names that mark a term as unsuitable for annotation. Matched
# case-insensitively as a substring so ``gocheck_do_not_annotate`` and any
# ontology-specific ``*do_not_annotate*`` variant both trigger the warning.
_DO_NOT_ANNOTATE_MARKERS = ("do_not_annotate",)


async def _ontology_version(ontology: str, db_path: Path, cli: OLSClient) -> str | None:
    """Return an ontology's source version, cache-first (fetched once, then reused).

    Version strings change only on ontology releases, so a cached value is reused
    indefinitely rather than aged out with the 7-day term TTL. Best-effort: a fetch
    failure simply yields None and is not cached.
    """
    cached = cache.get_ontology_version(db_path, ontology)
    if cached is not None:
        return cached
    version = await cli.fetch_ontology_version(ontology)
    if version is not None:
        cache.put_ontology_version(db_path, ontology, version)
    return version


def _term_warnings(term: dict) -> list[str]:
    """Build human-readable, actionable warnings about how a term should be used.

    Surfaces the two ways a grounding can quietly go wrong: handing back an
    obsolete CURIE, or a term flagged by its ontology as not-for-annotation.
    """
    warnings: list[str] = []
    if term.get("is_obsolete"):
        msg = "Term is obsolete and should not be used."
        if term.get("replaced_by"):
            msg += f" Replaced by {term['replaced_by']}."
        elif term.get("consider"):
            msg += f" Consider instead: {', '.join(term['consider'])}."
        warnings.append(msg)
    for subset in term.get("subsets") or []:
        if any(marker in subset.lower() for marker in _DO_NOT_ANNOTATE_MARKERS):
            warnings.append(
                f"Term is in the '{subset}' subset: it is too high-level to use for "
                "annotation; pick a more specific descendant."
            )
            break
    return warnings


async def get_term(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
    """Return a full term record, cache-first with a 7-day TTL.

    Returns ``(term, cache_hit)``. On a fresh-cache hit, ``cache_hit`` is True.
    Otherwise fetches from OLS, writes it back, and returns the re-read cached
    record so the shape is identical either way. The record carries
    ``consider``/``subsets``/``definition_sources`` lists, ``has_children``/
    ``is_leaf`` flags, and a computed ``warnings`` list (obsolete or
    do-not-annotate guidance). Propagates OLS error/not_found dicts with
    ``cache_hit=False``.
    """
    norm, err = safe_normalize_curie(curie)
    if norm is None:
        return err or {}, False

    ontology = norm.split(":", 1)[0]

    cached = cache.get_term_if_fresh(db_path, norm)
    if cached is not None:
        logger.debug("get_term cache hit: %s", norm)
        cached["warnings"] = _term_warnings(cached)
        # A cache hit stays offline: report the ontology version only if already
        # cached (it is captured on the miss path below), never a fresh OLS call.
        cached["ontology_version"] = cache.get_ontology_version(db_path, ontology)
        return cached, True

    logger.info("get_term cache miss, fetching OLS: %s", norm)
    async with ols_client(client) as cli:
        term = await cli.fetch_term(norm)
        if is_error(term):
            return term, False

        cache.put_term(db_path, term)
        stored = cache.get_term(db_path, norm)
        if stored is None:
            return {"error": "cache_write_failed", "curie": norm}, False
        # Already online for the term fetch — capture the ontology version too
        # (cache-first, so only the first miss per ontology adds a request).
        stored["ontology_version"] = await _ontology_version(ontology, db_path, cli)
    stored["warnings"] = _term_warnings(stored)
    return stored, False


async def find_synonyms(
    curie: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
    """Return ``({exact, related, narrow, broad}, cache_hit)`` for a term.

    Delegates to get_term and inherits its cache-hit status; propagates its error
    dict on failure.
    """
    term, cache_hit = await get_term(curie, db_path=db_path, client=client)
    if is_error(term):
        return term, cache_hit
    return term["synonyms"], cache_hit


async def validate_term(
    curie: str,
    *,
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
    """Check a term's current/obsolete status — always live, never cached.

    Returns ``(result, False)`` where result is
    ``{curie, is_current, is_obsolete, replaced_by, consider, checked_at}``.
    ``consider`` lists suggested alternative CURIEs for an obsolete term that has
    no single replacement (empty otherwise), so the answer is actionable.
    ``cache_hit`` is always False: deprecation status must never be served stale
    (project CLAUDE.md), so this tool takes no db_path and never reads or writes
    the cache.
    """
    norm, err = safe_normalize_curie(curie)
    checked_at = datetime.now(UTC).isoformat()
    if norm is None:
        return {**(err or {}), "checked_at": checked_at}, False

    logger.info("validate_term live fetch: %s", norm)
    async with ols_client(client) as cli:
        term = await cli.fetch_term(norm)

    if is_error(term):
        return {**term, "curie": norm, "checked_at": checked_at}, False

    is_obsolete = bool(term.get("is_obsolete"))
    return {
        "curie": norm,
        "is_current": not is_obsolete,
        "is_obsolete": is_obsolete,
        "replaced_by": term.get("replaced_by"),
        "consider": term.get("consider") or [],
        "checked_at": checked_at,
    }, False
