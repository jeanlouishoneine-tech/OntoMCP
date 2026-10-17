"""save_profile: write and freeze a research profile from an approved ontology list.

On by default, and force-disabled only under an administrator's lock. There is no
``ONTOMCP_ALLOW_PROFILE_TOOLS`` opt-in: an environment variable whose only enabler
is the config edit and client restart it exists to eliminate is circular.
"""

import logging

from ontomcp.core import profile as profile_mod
from ontomcp.core import registry
from ontomcp.core.ols_client import OLSClient

logger = logging.getLogger("ontomcp")


def _admin_locked() -> str | None:
    """Return the refusal message when an administrator has fixed the scope, else None."""
    import os

    if os.environ.get("ONTOMCP_ONTOLOGIES", "").strip():
        return (
            "Scope is locked by an administrator via ONTOMCP_ONTOLOGIES, so profiles "
            "cannot be written here. Ask whoever configured this server to change it."
        )
    return None


async def save_profile(
    name: str,
    ontologies: list[str],
    *,
    force_default: list[str] | None = None,
    description: str = "",
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
    """Create or update a profile, freeze it, and swap the in-process registry.

    Returns ``(payload, cache_hit)``; ``cache_hit`` is always False. The payload
    reports each member's **title and term count**, not just its prefix, so the
    model can show the human what was actually written — the same anti-hallucination
    gate that applies before the call.

    Never raises: a validation failure or a locked profile comes back as a
    structured error with prose the model can read out.
    """
    locked = _admin_locked()
    if locked:
        return {"error": "profile_writes_disabled", "detail": locked}, False

    try:
        profile = await profile_mod.save(
            name,
            ontologies,
            force_default=force_default,
            description=description,
            client=client,
        )
    except profile_mod.ProfileError as exc:
        return {"error": "profile_invalid", "detail": str(exc)}, False

    # The registry keys on (path, mtime, size), so the next resolve() re-reads the
    # file we just wrote. No restart, and no in-process singleton to invalidate.
    resolved = registry.resolve(profile)
    derived = profile["_derived"]

    return {
        "profile": name,
        "saved_to": str(profile_mod.profile_path(name)),
        "index_version": derived["index_version"],
        "frozen_at": derived["frozen_at"],
        "members": [
            {
                "ontology": key,
                "title": member["title"],
                "terms": member["terms"],
                "version": member["version"],
                "verified": member["verified"],
            }
            for key, member in derived["members"].items()
        ],
        "default_scope": list(resolved.default_scope),
        "held_back": {k: resolved.reasons[k] for k in resolved.named_only},
        "warnings": derived.get("warnings") or [],
        "next_step": (
            "This profile is active now — no restart needed. Searches are scoped to "
            "it; get_term and the hierarchy tools still work for any CURIE."
        ),
    }, False
