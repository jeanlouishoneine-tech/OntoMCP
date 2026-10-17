"""Resolve a CURIE to its IRI, for the ~286 ontologies OLS4 serves.

The hierarchy endpoints address terms by double-encoded IRI, so every hierarchy
call needs one. Before this module there was exactly one source — the eleven
hand-written ``config.IRI_TEMPLATES`` — and anything outside them raised, which is
why hierarchy on ``PO:0025034`` returned ``bad_curie``.

Five rungs, best evidence first. **Every rung below rung 1 is a guess, so each
carries a disconfirmation signal**: if a guessed IRI yields a 404 or an empty node
list, we do not report "this term has no parents" — we escalate, take the real IRI
off a term fetch, persist it, and retry once.

    rung 0  config.IRI_TEMPLATES        authoritative for the ten built-ins
    rung 1  the cached ``iri`` column   authoritative — OLS told us this
    rung 2  the profile's probed stem   observed at freeze against real terms
    rung 3  the generic OBO purl        a guess, and the workhorse: 76% of OLS4
                                        entries ship no baseUris at all
    rung 4  one fetch_term by obo_id    the ground truth, persisted so rung 1
                                        answers next time

Rung 2 deliberately prefers the freeze-time probe over the index's ``base_uris``:
the index value is absent for 217 of 286 entries and, where present, is not
reliably a minting stem — RO's ``base_uris[0]`` is a BFO term IRI. See
``tournament/spike-results.md`` S-1.
"""

import logging
from pathlib import Path

from ontomcp.core import cache, config
from ontomcp.core.ols_client import OLSClient, normalize_curie

logger = logging.getLogger("ontomcp")

_OBO_PURL = "http://purl.obolibrary.org/obo/{prefix}_{local_id}"


def _from_templates(prefix: str, local_id: str) -> str | None:
    """Rung 0. Unchanged behaviour for the built-ins, so their existing tests stay green."""
    template = config.IRI_TEMPLATES.get(prefix)
    return template.format(id=local_id) if template else None


def _from_cache(db_path: Path | None, curie: str) -> str | None:
    """Rung 1. Tested ``is not None``, never truthiness — an empty string is a miss, not a value."""
    if db_path is None:
        return None
    term = cache.get_term(db_path, curie)
    if term is None:
        return None
    iri = term.get("iri")
    return iri if iri is not None and iri != "" else None


def _from_registry(registry, prefix: str, local_id: str) -> str | None:
    """Rung 2. The stem observed at freeze against a real term of that ontology."""
    if registry is None:
        return None
    member = (registry.members or {}).get(prefix) or {}
    stem = member.get("base_uri")
    return f"{stem}{local_id}" if stem else None


def _generic(prefix: str, local_id: str, registry) -> str:
    """Rung 3. The OBO purl convention, using the ontology's own minting prefix if known.

    The minting prefix matters: ORDO's IRIs are ``.../Orphanet_NNN`` while its
    CURIEs are ``ORDO:NNN``, so a naive prefix substitution is wrong for exactly
    the ontology a rare-disease profile depends on.
    """
    minting = prefix
    if registry is not None:
        member = (registry.members or {}).get(prefix) or {}
        minting = member.get("curie_prefix") or prefix
    return _OBO_PURL.format(prefix=minting, local_id=local_id)


def candidates(curie: str, *, db_path: Path | None = None, registry=None) -> list[tuple[str, bool]]:
    """Ordered ``(iri, authoritative)`` candidates for a CURIE, best evidence first, deduped.

    ``authoritative`` marks rungs 0 and 1 — a hand-verified template, or an IRI OLS
    itself gave us. It is the flag that keeps the disconfirmation cheap: an empty
    node list under an authoritative IRI means the term really is a leaf or a root,
    while under a *guessed* IRI it means we probably built the wrong URL. Without
    the distinction, every ``get_children`` on a leaf — the majority of terms —
    would pay two extra round trips to re-confirm nothing.

    Kept separate from ``fetch_with_disconfirmation`` so the rung order is
    testable without a network client or an event loop.
    """
    normalized = normalize_curie(curie)
    prefix, local_id = normalized.split(":", 1)

    ordered = [
        (_from_templates(prefix, local_id), True),  # rung 0
        (_from_cache(db_path, normalized), True),  # rung 1
        (_from_registry(registry, prefix, local_id), False),  # rung 2
        (_generic(prefix, local_id, registry), False),  # rung 3
    ]
    seen: list[tuple[str, bool]] = []
    known: set[str] = set()
    for iri, authoritative in ordered:
        if iri and iri not in known:
            known.add(iri)
            seen.append((iri, authoritative))
    return seen


def _is_miss(nodes) -> bool:
    """True when the client reported the IRI itself did not resolve."""
    return (
        isinstance(nodes, list)
        and len(nodes) == 1
        and isinstance(nodes[0], dict)
        and nodes[0].get("error") == "iri_miss"
    )


async def fetch_with_disconfirmation(
    curie: str,
    fetch,
    *,
    db_path: Path | None = None,
    registry=None,
    client: OLSClient | None = None,
) -> list[dict]:
    """Call ``fetch(iri)`` down the rung ladder, escalating once if every guess misses.

    ``fetch`` is one of the OLS client's hierarchy methods, already bound to its
    endpoint and relation type. This function owns the whole disconfirmation
    protocol so the four hierarchy tools stay thin and cannot each get it subtly
    different:

    1. Try each candidate IRI in rung order. A real answer wins immediately.
    2. An empty list under an **authoritative** IRI is the truth — the term is a
       leaf or a root — so it is returned as ``[]``, not escalated. But rung 0
       (hand-written templates) and rung 1 (OLS's own cached IRI) are both
       flagged authoritative, and they are not equally strong evidence: a stale
       or mistaken template answering empty must not be believed over OLS's own
       answer for this exact term if that stronger candidate is still waiting
       later in the list — only the LAST authoritative candidate's empty result
       is trusted immediately.
    3. If every candidate missed, escalate to rung 4: fetch the term by ``obo_id``
       (which needs no IRI), take the real IRI off the payload, persist it, retry
       once.
    4. Still nothing: return ``[]``, having actually established it.
    """
    candidate_list = candidates(curie, db_path=db_path, registry=registry)
    last: list[dict] = []
    for position, (iri, authoritative) in enumerate(candidate_list):
        nodes = await fetch(curie, iri)
        if not _is_miss(nodes):
            return nodes
        if authoritative and nodes and nodes[0].get("empty"):
            stronger_candidate_pending = any(a for _, a in candidate_list[position + 1 :])
            if not stronger_candidate_pending:
                # OLS knows this IRI and says it has no neighbours. Believe it.
                return []
        last = nodes

    real_iri = await escalate(curie, db_path=db_path, client=client)
    if real_iri:
        nodes = await fetch(curie, real_iri)
        if not _is_miss(nodes):
            return nodes
        logger.info("hierarchy: %s resolved to %s but has no nodes", curie, real_iri)
        return []
    logger.info("hierarchy: could not resolve an IRI for %s (last attempt: %s)", curie, last)
    return [{"error": "not_found", "curie": curie}]


async def escalate(
    curie: str,
    *,
    db_path: Path | None = None,
    client: OLSClient | None = None,
) -> str | None:
    """Rung 4: ask OLS for the term itself and take the IRI off the payload.

    ``fetch_term`` addresses the term by ``obo_id``, which needs no IRI — that is
    what makes this rung able to rescue a wrong guess. The result is persisted, so
    the next call resolves at rung 1 with zero network.

    This is the disconfirmation half of the chain. A resolver that can guess must
    be able to detect the miss, or it converts "I built the wrong URL" into "this
    term has no parents" — which is what the four hierarchy tools did for any IRI
    OLS answered with a 404 (tournament/spike-results.md S-4).
    """
    from ontomcp.core.tools._common import ols_client

    normalized = normalize_curie(curie)
    async with ols_client(client) as cli:
        term = await cli.fetch_term(normalized)
    if not isinstance(term, dict) or "error" in term:
        logger.info("escalate: OLS could not resolve %s", normalized)
        return None
    iri = term.get("iri")
    if iri and db_path is not None:
        cache.put_term(db_path, term)
    return iri or None
