"""map_across_ontologies: find the equivalent of a term in another ontology.

Two strategies, best first:

1. Authoritative cross-references in the source term's raw OLS payload
   (``obo_xref`` / ``oboXref`` / ``annotation.database_cross_reference``). These
   are curated by the ontology, so they are reported as ``skos:exactMatch`` with
   ``confidence=1.0``.
2. A fuzzy label search against the target ontology — only used when no curated
   xref exists. String similarity is NOT semantic equivalence, so these are
   reported as ``heuristic_label`` and their confidence is squeezed into a band
   that can never reach or tie a curated xref. The label score is only promoted
   toward the top of that band when the candidate's own synonyms also contain the
   source label (corroborating evidence beyond a single string compare).
"""

from pathlib import Path

from rapidfuzz import fuzz

from ontomcp.core.config import DB_PATH, ONTOLOGIES
from ontomcp.core.ols_client import OLSClient, normalize_curie
from ontomcp.core.tools._common import is_error, ols_client
from ontomcp.core.tools.search import search_terms
from ontomcp.core.tools.term import get_term

# A fuzzy label match must clear this raw token-sort ratio to be reported at all.
_LABEL_MATCH_THRESHOLD = 0.85
# Heuristic matches are rescaled into [0, _LABEL_MATCH_CONFIDENCE_CAP] so a string
# match can never tie or beat a curated xref (confidence 1.0). A perfect string
# match (ratio 1.0) maps to the cap; the threshold maps to 0.
_LABEL_MATCH_CONFIDENCE_CAP = 0.7


def _normalized_xref(raw: str, target: str) -> str | None:
    """Normalize an xref string and return it iff its prefix matches ``target``."""
    try:
        norm = normalize_curie(raw)
    except (ValueError, AttributeError):
        return None
    return norm if norm.split(":", 1)[0] == target else None


def _xrefs_for_target(raw_json: dict | None, target: str) -> list[dict]:
    """Pull cross-references whose prefix matches ``target`` from the OLS payload.

    Handles both the structured ``obo_xref`` form ([{database, id}]) and the flat
    ``oboXref`` string form (["MESH:D003643", ...]). Both forms are run through
    ``normalize_curie`` (the ``id`` field is sometimes already prefixed, e.g.
    "MESH:D009369"), so no double-prefixed CURIEs leak out. Results are deduped.
    """
    if not raw_json:
        return []
    seen: set[str] = set()
    matches: list[dict] = []

    def _add(raw: str | None) -> None:
        if not raw:
            return
        norm = _normalized_xref(raw, target)
        if norm and norm not in seen:
            seen.add(norm)
            matches.append({"curie": norm})

    for entry in raw_json.get("obo_xref") or []:
        local_id = entry.get("id")
        # ``id`` may already be a full CURIE; if not, compose database:id.
        if isinstance(local_id, str) and ":" in local_id:
            _add(local_id)
        else:
            _add(f"{entry.get('database') or ''}:{local_id}")

    for ref in raw_json.get("oboXref") or []:
        if isinstance(ref, str):
            _add(ref)

    # OLS also exposes curated xrefs under annotation.database_cross_reference.
    annotation = raw_json.get("annotation") or {}
    if isinstance(annotation, dict):
        for ref in annotation.get("database_cross_reference") or []:
            if isinstance(ref, str):
                _add(ref)

    return matches


def _label_match_confidence(ratio: float) -> float:
    """Rescale a raw token-sort ratio in [threshold, 1] into [0, cap].

    Keeps heuristic confidences strictly below curated xref confidence (1.0): even
    a perfect string match tops out at ``_LABEL_MATCH_CONFIDENCE_CAP``.
    """
    span = 1.0 - _LABEL_MATCH_THRESHOLD
    fraction = (ratio - _LABEL_MATCH_THRESHOLD) / span if span else 1.0
    return round(fraction * _LABEL_MATCH_CONFIDENCE_CAP, 3)


async def map_across_ontologies(
    curie: str,
    target_ontology: str,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[list[dict], bool]:
    """Map a term to its equivalent(s) in ``target_ontology``.

    Returns ``(results, cache_hit)`` where results is
    ``[{curie, label, match_type, mapping_predicate, confidence}, ...]`` sorted by
    confidence descending. ``match_type`` is ``"exact_xref"`` (curated
    cross-reference, ``mapping_predicate="skos:exactMatch"``, confidence 1.0) or
    ``"label_match"`` (a string-similarity heuristic, ``mapping_predicate=
    "heuristic_label"``, confidence capped below any xref). Heuristic matches are
    candidates to verify, not asserted equivalences. ``cache_hit`` is True only
    when every underlying lookup was a cache hit. Propagates error dicts from the
    underlying calls.
    """
    target = target_ontology.upper()
    if target not in ONTOLOGIES:
        return [{"error": "unknown_ontology", "ontology": target_ontology}], False

    async with ols_client(client) as cli:
        source, source_hit = await get_term(curie, db_path=db_path, client=cli)
        if is_error(source):
            return [source], source_hit

        results: list[dict] = []
        for xref in _xrefs_for_target(source.get("raw_json"), target):
            results.append(
                {
                    "curie": xref["curie"],
                    "label": None,
                    "match_type": "exact_xref",
                    "mapping_predicate": "skos:exactMatch",
                    "confidence": 1.0,
                }
            )
        if results:
            return results, source_hit

        source_label = source.get("label") or ""
        source_synonyms = {
            s.lower() for bucket in (source.get("synonyms") or {}).values() for s in bucket
        }
        candidates, cand_hit = await search_terms(
            source_label, [target], limit=10, db_path=db_path, client=cli
        )
    cache_hit = source_hit and cand_hit
    if is_error(candidates):
        return candidates, cache_hit

    source_label_lower = source_label.lower()
    for cand in candidates:
        # OLS searches include imported terms (e.g. EFO imports MONDO); keep only
        # candidates actually minted by the target ontology.
        if cand["curie"].split(":", 1)[0] != target:
            continue
        cand_label = cand.get("label") or ""
        ratio = fuzz.token_sort_ratio(source_label, cand_label) / 100.0
        if ratio < _LABEL_MATCH_THRESHOLD:
            continue
        confidence = _label_match_confidence(ratio)
        # Corroborating evidence: if the candidate's label is itself a known synonym
        # of the source term (or vice versa), nudge to the top of the heuristic band.
        if cand_label.lower() in source_synonyms or source_label_lower == cand_label.lower():
            confidence = _LABEL_MATCH_CONFIDENCE_CAP
        results.append(
            {
                "curie": cand["curie"],
                "label": cand_label,
                "match_type": "label_match",
                "mapping_predicate": "heuristic_label",
                "confidence": confidence,
            }
        )

    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results, cache_hit
