"""save_profile: write and freeze a research profile from an approved ontology list.

On by default, and force-disabled only under an administrator's lock. There is no
``ONTOMCP_ALLOW_PROFILE_TOOLS`` opt-in: an environment variable whose only enabler
is the config edit and client restart it exists to eliminate is circular.

Every write goes through ``profile_gate.confirm_and_apply``, which asks the human
before anything is written.
"""

from ontomcp.core import profile as profile_mod
from ontomcp.core import profile_gate
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.profile_gate import Confirm, ProfileChange


async def save_profile(
    name: str,
    ontologies: list[str],
    *,
    force_default: list[str] | None = None,
    description: str = "",
    client: OLSClient | None = None,
    confirm: Confirm | None = None,
) -> tuple[dict, bool]:
    """Create or replace a profile, freeze it, and make it the active profile.

    Returns ``(payload, cache_hit)``; ``cache_hit`` is always False. The payload
    reports each member's **title and term count**, not just its prefix, so the
    model can show the human what was actually written.

    Never raises: a validation failure, a locked profile or a declined confirmation
    comes back as a structured dict with prose the model can read out.
    """
    try:
        before = profile_mod.current_ontologies(name)
    except profile_mod.ProfileError as exc:
        return {"error": "profile_invalid", "detail": str(exc)}, False
    change = ProfileChange(
        "replace",
        name,
        resulting=tuple(ontologies),
        before=tuple(before),
        description=description,
        force_default=tuple(force_default or ()),
    )
    return await profile_gate.confirm_and_apply(change, confirm, client=client), False
