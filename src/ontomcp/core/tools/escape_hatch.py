"""The escape hatch: what to say when a search finds nothing in scope.

Fires only on **zero results in the searched scope**, never on an error — an OLS
failure is not evidence about scope. ``ONTOMCP_ESCAPE_HATCH=0`` disables both
stages for strict determinism.

**Stage 1 costs nothing.** The search that would have run over `default_scope` runs
instead over `default ∪ named_only` and is partitioned locally by minting prefix.
If the default partition is non-empty, only it is returned and Stage 1 never
speaks. This is a local partition rather than an auto-widen precisely so that
*searched scope never becomes a function of whether the first attempt was empty* —
otherwise the same query means different things on different days.

**Stage 2 costs one call, and returns ontology names and counts, never terms.**
OLS4 ships per-ontology facet counts on every search by default, so the counts are
exact over the whole result set. A quarantined ontology with hits is **named**,
with its direct-search line — it is only suppressed from the *"add to your
profile"* recommendation. Suppressing it entirely would recreate the dead end the
escape hatch exists to close, for exactly the users most likely to hit it.
"""

import os

from ontomcp.core.config import QUARANTINE

# Never suggest adding more than this many ontologies at once: a list of twenty is
# a research decision dressed as a menu.
_MAX_SUGGESTIONS = 3


def enabled() -> bool:
    return os.environ.get("ONTOMCP_ESCAPE_HATCH", "1").strip() != "0"


def partition(
    rows: list[dict], default_scope: set[str], named_only: set[str]
) -> tuple[list[dict], dict[str, int]]:
    """Split rows into (in-default-scope, {held_back_prefix: count}) by minting prefix.

    A row minted by an ontology that is in neither ``default_scope`` nor
    ``named_only`` belongs to neither bucket — it is not in the profile at all,
    so it is Stage 2's "add to your profile" case, not Stage 1's "you already
    have this, held back". Without this distinction, ``held`` used to claim any
    foreign-minted row was "in your profile but held back", printing a false
    notice with a remediation call ``normalize_ontologies`` would reject.
    """
    from ontomcp.core.tools.search import _minting_prefix

    in_default: list[dict] = []
    held: dict[str, int] = {}
    for row in rows:
        prefix = _minting_prefix(row)
        if prefix in default_scope:
            in_default.append(row)
        elif prefix in named_only:
            held[prefix] = held.get(prefix, 0) + 1
    return in_default, held


def stage_one(query: str, held: dict[str, int], default_scope: list[str]) -> str:
    """The notice for "nothing in your default scope, but something you already own"."""
    lines = [
        f"No matches in your default scope ({', '.join(default_scope)}).",
    ]
    for prefix, count in sorted(held.items(), key=lambda kv: -kv[1]):
        reason = QUARANTINE.get(prefix, "held back from default search")
        lines.append(
            f"{prefix} is in your profile but held back from default search "
            f"({reason}) — {count} match{'es' if count != 1 else ''} there."
        )
        lines.append(f'  Search it:  search_terms("{query}", ontologies=["{prefix}"])')
    return "\n".join(lines)


def stage_two(query: str, counts: dict[str, int], members: set[str], index: dict) -> dict:
    """Name the ontologies that DO have hits, ranked by exact count.

    Returns a structured notice. Two suppression rules, and they differ:

    - a **quarantined** ontology is named with its direct-search line but never
      recommended for adding — adding it would flood every future query;
    - a **non-searchable** (property-dominant) ontology is likewise named but not
      recommended — it would consume a profile slot and return almost nothing.

    Neither is hidden. Hiding is what recreated the dead end.

    ``counts`` comes from OLS4's ``ontologyPreferredPrefix`` facet, which counts
    by the ontology that INDEXED each hit — an ontology can index a term it
    imports from elsewhere. ``search_terms``'s own filter narrows by the ontology
    that MINTED the CURIE instead (see ``search._minting_prefix``), and the two
    can disagree: a hit facet-counted under an importer may be minted by an
    ontology the profile does not have. A single facet-only call (deliberately
    cheap — no terms fetched) cannot resolve that without adding a second call,
    so every record here carries an explicit caveat rather than a confident
    promise the minting filter may not honor.
    """
    entries = index.get("ontologies") or {}
    ranked = sorted(
        ((p, c) for p, c in counts.items() if p not in members and c > 0),
        key=lambda kv: -kv[1],
    )

    suggestions: list[dict] = []
    mentioned: list[dict] = []
    for prefix, count in ranked:
        entry = entries.get(prefix)
        if entry is None:
            continue
        record = {
            "ontology": prefix,
            "title": entry["title"],
            "matches": count,
            "matches_note": (
                f"{prefix} INDEXES this many hits for the query; some may be "
                f"MINTED by a different ontology {prefix} imports, so adding "
                f"{prefix} is not guaranteed to surface them."
            ),
        }
        reason = QUARANTINE.get(prefix)
        if reason is None and not entry.get("searchable", True):
            reason = f"{prefix} is property-dominant; it would return little."

        if reason is None:
            # Recommendable. Over the cap it is simply dropped — it must NOT fall
            # through to the "named but not recommended" list, which would attach
            # a reason that is not true of it. A ranked list that runs out of room
            # has nothing to explain.
            if len(suggestions) < _MAX_SUGGESTIONS:
                record["remediation"] = f"Ask me to add {prefix} to your profile."
                suggestions.append(record)
        elif len(mentioned) < _MAX_SUGGESTIONS:
            record["reason"] = reason
            record["search_directly"] = f'search_terms("{query}", ontologies=["{prefix}"])'
            mentioned.append(record)

    if not suggestions and not mentioned:
        return {}

    notice: dict = {"query": query}
    if suggestions:
        notice["add_to_profile"] = suggestions
    if mentioned:
        notice["named_but_not_recommended"] = mentioned
    return notice
