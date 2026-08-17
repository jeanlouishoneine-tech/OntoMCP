"""get_profile_setup_guidance: hand the client's model what it needs to propose a profile.

This tool takes **no arguments** and performs **no matching**.

It used to take a research description and run it through a keyword table. That is
the one behaviour the objective prohibits: it made the scientist re-describe their
research, and it capped reach at whatever the table happened to list. The keyword
table and the example-term dictionary are deleted, not rebound.

The division of labour, which is the whole design in three lines:

- the **index** says what is *valid* — ~286 prefixes and titles, generated from OLS4;
- **policy.md** says what a person had to *decide* — semantic calls only, ~10 lines;
- the **client's frontier model** supplies domain -> ontology mapping, which it
  already knows better than any table we could write and keep current.

So this returns the policy text, the grounding list, and the rules. The model does
the proposing; the human approves; ``save_profile`` validates and freezes.
"""

import logging
from pathlib import Path

from ontomcp.core import registry
from ontomcp.core.config import (
    DEFAULT_SCOPE_WARN,
    PROFILE_MAX,
    PROFILE_WARN,
    QUARANTINE,
)

logger = logging.getLogger("ontomcp")

POLICY_PATH = Path(__file__).parent.parent.parent / "data" / "policy.md"


def _policy_text() -> str:
    """Return policy.md verbatim, or "" if unreadable. Never parsed — it is
    LLM-facing prose. An empty string is a valid-looking value for the
    ``policy`` field, so a missing file must not look identical to an
    intentionally short policy — see ``policy_unavailable`` on the payload.
    """
    try:
        return POLICY_PATH.read_text()
    except OSError as exc:
        logger.error("get_profile_setup_guidance: policy.md unreadable at %s: %s", POLICY_PATH, exc)
        return ""


def get_profile_setup_guidance() -> tuple[dict, bool]:
    """Return everything needed to propose a research profile. Zero arguments.

    ``cache_hit`` is always False — nothing is looked up, so nothing is cached.

    The grounding list is ``prefix -> title`` for every searchable ontology in the
    index. Titles are included deliberately: an existence check proves ``PO`` is a
    real prefix, not that the user meant Plant Ontology rather than Population and
    Community Ontology. The title is the disambiguator, and it is already in hand.

    Ontologies flagged ``searchable: false`` are omitted — they are property-
    dominant, so proposing them wastes a profile slot. Nothing else is omitted:
    a ``terms < 100`` rule would silently make a shipped starter's own member
    unproposable by the model meant to propose it.
    """
    try:
        index = registry.load_index()
    except registry.IndexError_ as exc:
        logger.error("get_profile_setup_guidance: %s", exc)
        return {"error": "index_unavailable", "detail": str(exc)}, False
    entries = index.get("ontologies") or {}
    grounding = {
        key: entry["title"] for key, entry in sorted(entries.items()) if entry.get("searchable")
    }
    policy = _policy_text()

    payload = {
        "policy": policy,
        "grounding_list": grounding,
        "index_version": index.get("index_version"),
        "rules": {
            "max_ontologies": PROFILE_MAX,
            "target_range": [5, PROFILE_WARN],
            "default_scope_warn_above": DEFAULT_SCOPE_WARN,
            "quarantined": dict(QUARANTINE),
            "quarantine_note": (
                "Quarantined ontologies can still be in a profile. They are held out "
                "of unscoped search because their labels collide with ordinary words; "
                "name them explicitly, or set force_default to search them by default."
            ),
        },
        "next_step": (
            "Propose 5-8 ontologies for the user's described work, using the grounding "
            "list to check every prefix exists. Show each proposal's prefix AND title "
            "and get an explicit yes from the human before calling save_profile."
        ),
    }
    if not policy:
        payload["policy_unavailable"] = True
    return payload, False
