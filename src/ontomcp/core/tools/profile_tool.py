"""save_profile: write and freeze a research profile from an approved ontology list.

On by default, and force-disabled only under an administrator's lock. There is no
``ONTOMCP_ALLOW_PROFILE_TOOLS`` opt-in: an environment variable whose only enabler
is the config edit and client restart it exists to eliminate is circular.

Every write goes through ``profile_gate.confirm_and_apply``, which asks the human
before anything is written.
"""

from ontomcp.core import profile as profile_mod
from ontomcp.core import profile_gate, registry
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


def _stored(name: str) -> dict:
    """The user's file or starter for ``name``, or {} for the built-in default."""
    try:
        return profile_mod.load(name)
    except profile_mod.ProfileError:
        return {}


def get_profile() -> tuple[dict, bool]:
    """The profile actually in use, why it was chosen, and whether an undo exists."""
    active = registry.current()
    try:
        undo = active.source not in {"builtin", "admin_lock"} and profile_mod.has_previous(
            active.profile
        )
    except profile_mod.ProfileError:
        undo = False
    return {
        "profile": active.profile,
        "source": active.source,
        "description": _stored(active.profile).get("description", "")
        if active.source not in {"builtin", "admin_lock"}
        else "",
        "members": [
            {
                "ontology": key,
                "title": active.members[key].get("title"),
                "terms": active.members[key].get("terms"),
                "version": active.members[key].get("version"),
                "held_back": active.reasons.get(key) if key in active.named_only else None,
            }
            for key in active.all_members
        ],
        "undo_available": undo,
        "warnings": list(active.warnings),
    }, False


async def update_profile(
    add: list[str] | None = None,
    remove: list[str] | None = None,
    reset: str | None = None,
    *,
    client: OLSClient | None = None,
    confirm: Confirm | None = None,
) -> tuple[dict, bool]:
    """Add/remove ontologies on the active profile, or reset it ("defaults" | "previous").

    The edit is saved under the active profile's name: a starter becomes a user
    profile that shadows it, and the built-in set becomes ``default``. Never raises.
    """
    add, remove = add or [], remove or []
    if reset is not None and (add or remove):
        return _bad("Use either reset or add/remove, not both."), False
    if reset not in (None, "defaults", "previous"):
        return _bad("reset must be 'defaults' or 'previous'."), False
    if reset is None and not (add or remove):
        return _bad("Nothing to change: give add, remove, or reset."), False

    active = registry.current()
    before = tuple(active.all_members)

    if reset == "defaults":
        if active.source == "builtin":
            return {"status": "unchanged", "detail": "Already using the default ontologies."}, False
        if active.source == "env":
            return _env_pinned(active.profile), False
        change = ProfileChange("reset_defaults", active.profile, before=before)
    elif reset == "previous":
        change = ProfileChange("restore_previous", active.profile)
    else:
        added = [registry.canonical_key(o) for o in add]
        removed = {registry.canonical_key(o) for o in remove}
        missing = sorted(removed - set(before))
        if missing:
            return _bad(f"Not in your profile, so nothing to remove: {', '.join(missing)}."), False
        resulting = [o for o in before if o not in removed]
        resulting += [o for o in added if o not in resulting]
        if not resulting:
            return _bad(
                "That would leave the profile empty. To go back to the default ontologies, "
                "use reset='defaults'."
            ), False
        stored = _stored(active.profile)
        change = ProfileChange(
            "update",
            active.profile,
            resulting=tuple(resulting),
            before=before,
            description=stored.get("description", ""),
            force_default=tuple(stored.get("force_default") or ()),
        )
    return await profile_gate.confirm_and_apply(change, confirm, client=client), False


def _bad(detail: str) -> dict:
    return {"error": "bad_request", "detail": detail}


def _env_pinned(name: str) -> dict:
    return {
        "error": "pinned_by_config",
        "detail": (
            f"Your client config pins '{name}' via ONTOMCP_PROFILE, so a reset here would "
            f"change nothing. Remove that setting to go back to the default ontologies."
        ),
    }
