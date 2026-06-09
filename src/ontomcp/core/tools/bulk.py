"""bulk_annotate: map many free-text strings to ontology terms concurrently."""

import asyncio
from pathlib import Path

from rapidfuzz import fuzz

from ontomcp.core.config import BULK_MAX, BULK_WARN, DB_PATH
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, ols_client
from ontomcp.core.tools.search import search_terms

_MAX_ALTERNATIVES = 3
# Cap simultaneous in-flight OLS searches so a large batch can't fan out into
# hundreds of concurrent requests (OLS etiquette + event-loop pressure).
_MAX_CONCURRENT_SEARCHES = 10


def _annotate_one(text: str, candidates: list[dict], threshold: float) -> dict:
    """Score search candidates for one input string and pick the best match."""
    if is_error(candidates) or not candidates:
        return {"input": text, "best_match": None, "alternatives": []}

    scored = []
    for cand in candidates:
        score = fuzz.token_sort_ratio(text, cand.get("label") or "") / 100.0
        scored.append(
            {"curie": cand["curie"], "label": cand.get("label"), "score": round(score, 3)}
        )
    scored.sort(key=lambda c: c["score"], reverse=True)

    top = scored[0]
    best = top if top["score"] >= threshold else None
    alternatives = scored[1 : 1 + _MAX_ALTERNATIVES]
    return {"input": text, "best_match": best, "alternatives": alternatives}


async def bulk_annotate(
    terms: list[str],
    ontology_hint: str | None = None,
    threshold: float = 0.8,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
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

    ontologies = [ontology_hint] if ontology_hint else None
    distinct = list(dict.fromkeys(terms))  # preserve order, drop duplicates
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SEARCHES)

    async with ols_client(client) as cli:

        async def _search(text: str) -> tuple[list[dict], bool]:
            async with semaphore:
                return await search_terms(text, ontologies, limit=5, db_path=db_path, client=cli)

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
