"""bulk_annotate: map many free-text strings to ontology terms concurrently."""

import asyncio
import logging
from pathlib import Path

from rapidfuzz import fuzz

from ontomcp.core import registry
from ontomcp.core.config import BULK_MAX, BULK_WARN, DB_PATH
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, normalize_ontologies, ols_client
from ontomcp.core.tools.search import search_terms

logger = logging.getLogger("ontomcp")

_MAX_ALTERNATIVES = 3


def _full_profile() -> tuple[str, ...] | None:
    """Default scope UNION named-only. See the scope rationale in bulk_annotate.

    Returns None (not an empty tuple) when the registry can't be resolved, so
    callers passing this straight into normalize_ontologies's ``scope=`` fail
    open instead of rejecting every hint as out-of-profile.
    """
    try:
        return registry.current().all_members
    except Exception:
        return None


# Cap simultaneous in-flight OLS searches so a large batch can't fan out into
# hundreds of concurrent requests (OLS etiquette + event-loop pressure).
_MAX_CONCURRENT_SEARCHES = 10


def _annotate_one(text: str, candidates: list[dict], threshold: float) -> dict:
    """Score search candidates for one input string and pick the best match."""
    if is_error(candidates):
        # A failed search must never read the same as a genuine zero-hit search:
        # collapsing them turned a transient OLS outage into "this term does not
        # exist" for whichever rows of the batch happened to hit it.
        error = candidates[0] if isinstance(candidates, list) else candidates
        logger.warning("bulk_annotate: search failed for %r: %s", text, error.get("error"))
        return {
            "input": text,
            "best_match": None,
            "alternatives": [],
            "error": error.get("error"),
        }
    if not candidates:
        return {"input": text, "best_match": None, "alternatives": []}

    scored = []
    for cand in candidates:
        score = fuzz.token_sort_ratio(text, cand.get("label") or "") / 100.0
        scored.append(
            {
                "curie": cand["curie"],
                "label": cand.get("label"),
                "score": round(score, 3),
                # 500 rows is the highest-volume surface in the product, and it
                # was handing back obsolete CURIEs as best_match with no flag.
                "is_obsolete": bool(cand.get("is_obsolete", False)),
            }
        )
    scored.sort(key=lambda c: c["score"], reverse=True)

    top = scored[0]
    best = top if top["score"] >= threshold else None
    # When nothing clears the threshold, the top candidate must still be shown.
    # Slicing from index 1 regardless hid the single most useful row behind
    # best_match: null — so a 0.79 near-miss looked like no match at all.
    alternatives = scored[1:] if best else scored[:]
    return {
        "input": text,
        "best_match": best,
        "alternatives": alternatives[:_MAX_ALTERNATIVES],
    }


async def bulk_annotate(
    terms: list[str],
    ontology_hint: str | None = None,
    threshold: float = 0.8,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict | list, bool]:
    """Annotate a list of strings with their best-matching ontology terms.

    Returns ``(payload, cache_hit)`` where payload is
    ``{results: [{input, best_match, alternatives}, ...]}`` plus an optional
    ``warning``. ``cache_hit`` is True only when every per-term search was a cache
    hit. Hard error above 500 inputs; soft warning above 100. Duplicate inputs are
    searched once; searches run concurrently (bounded) against one shared OLS
    client. The ``results`` list still has one entry per input, in input order.
    """
    if len(terms) > BULK_MAX:
        return {"error": "too_many_terms", "count": len(terms), "max": BULK_MAX}, False

    if ontology_hint:
        ontologies, rejected = normalize_ontologies([ontology_hint], scope=_full_profile())
        if rejected:
            # Same error taxonomy as search_terms's rejected-scope return — keep
            # the envelope shape (a list) consistent across tools built on the
            # same normalize_ontologies helper.
            return rejected, False
    else:
        # The FULL profile, default union named-only — not the default scope. A
        # spreadsheet with a species column must not silently fail under a profile
        # that quarantines NCBITaxon, which is precisely what bulk annotation is for.
        ontologies = list(_full_profile() or ())

    distinct = list(dict.fromkeys(terms))  # preserve order, drop duplicates
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SEARCHES)

    async with ols_client(client) as cli:

        async def _search(text: str) -> tuple[list[dict], bool]:
            async with semaphore:
                payload, hit = await search_terms(
                    text, ontologies, limit=5, db_path=db_path, client=cli
                )
                # search_terms returns {results, meta}; bulk wants the rows. The
                # meta block is deliberately not propagated: 500 copies of the
                # same scope statement is exactly the context tax it exists to avoid.
                rows = payload["results"] if isinstance(payload, dict) else payload
                return rows, hit

        search_results = await asyncio.gather(*(_search(t) for t in distinct))

    by_term = dict(zip(distinct, search_results))
    cache_hit = bool(by_term) and all(hit for _, hit in by_term.values())

    results = [_annotate_one(text, by_term[text][0], threshold) for text in terms]

    payload: dict = {"results": results}
    if len(terms) > BULK_WARN:
        payload["warning"] = (
            f"{len(terms)} terms exceeds the recommended {BULK_WARN}; this may be slow."
        )
    return payload, cache_hit
