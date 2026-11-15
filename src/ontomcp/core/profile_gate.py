"""The one write path for research profiles from the MCP tools, behind one gate.

Every profile write a model can trigger — replace, update, reset to defaults,
undo — goes through ``confirm_and_apply``. It refuses locked scopes first, then
asks the human through the best mechanism the client offers, and only then writes:

1. ``confirm`` given (the client supports in-app confirmation): ask, and write
   only on an explicit accept. Decline, cancel, timeout or any error writes nothing.
2. ``confirm`` is None and strict mode is on: refuse, pointing at the CLI.
3. ``confirm`` is None otherwise: the client's own tool-permission prompt is the
   confirmation, and the response says so.

Free of FastMCP on purpose: ``confirm`` is a plain async callback, which the MCP
server builds from its request context. The CLI never comes through here — its
user is the human.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ontomcp.core import config, registry
from ontomcp.core import profile as profile_mod
from ontomcp.core.ols_client import OLSClient

logger = logging.getLogger("ontomcp")

Decision = Literal["accepted", "declined", "cancelled"]
Confirm = Callable[[str], Awaitable[Decision]]
ChangeKind = Literal["replace", "update", "reset_defaults", "restore_previous"]


@dataclass(frozen=True)
class ProfileChange:
    """A proposed write. ``before`` and ``resulting`` are ontology prefix lists."""

    kind: ChangeKind
    name: str
    resulting: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    description: str = ""
    force_default: tuple[str, ...] = ()

    @property
    def added(self) -> list[str]:
        return [o for o in self.resulting if o not in self.before]

    @property
    def removed(self) -> list[str]:
        return [o for o in self.before if o not in self.resulting]


# --- refusals ----------------------------------------------------------------


def _admin_locked() -> str | None:
    """The refusal message when an administrator has fixed the scope, else None."""
    if os.environ.get("ONTOMCP_ONTOLOGIES", "").strip():
        return (
            "Scope is locked by an administrator via ONTOMCP_ONTOLOGIES, so profiles "
            "cannot be written here. Ask whoever configured this server to change it."
        )
    return None


def _refusal(change: ProfileChange) -> dict | None:
    """Refuse locked scopes before any confirmation is shown to the human."""
    locked = _admin_locked()
    if locked:
        return {"error": "profile_writes_disabled", "detail": locked}
    if change.kind == "reset_defaults":
        return None  # only clears the pointer; the profile file is untouched
    try:
        path = profile_mod.profile_path(change.name)
        if path.exists() and profile_mod.load(change.name).get("locked"):
            return {
                "error": "profile_invalid",
                "detail": (
                    f"Profile '{change.name}' is locked by an administrator and cannot be "
                    f"changed here. Ask whoever distributed it, or save under a different name."
                ),
            }
    except profile_mod.ProfileError as exc:
        return {"error": "profile_invalid", "detail": str(exc)}
    return None


def _strict_refusal(change: ProfileChange) -> dict:
    from ontomcp.cli import cli_equivalent  # lazy: the CLI module imports core

    if change.kind == "reset_defaults":
        args = "profile reset"
    elif change.kind == "restore_previous":
        args = f"profile undo {change.name}"
    else:
        exists = profile_mod.profile_path(change.name).exists()
        verb = "edit" if exists or change.name in profile_mod.starters() else "create"
        args = f"profile {verb} {change.name} --ontologies {','.join(change.resulting)}"
    return {
        "error": "confirmation_unavailable",
        "detail": (
            "This server requires an in-app confirmation for profile changes, and this "
            "client cannot ask you for one. Make the change from a terminal instead."
        ),
        # Display-only: a shell line in tool output must never read as an instruction.
        "cli_equivalent": cli_equivalent(args),
    }


# --- the confirmation message ----------------------------------------------


def _line(key: str, entries: dict) -> str:
    entry = entries.get(key) or {}
    terms = entry.get("terms")
    count = f"{terms:,} terms" if isinstance(terms, int) else "term count unknown"
    return f"  {key}: {entry.get('title') or key} ({count})"


def confirmation_message(change: ProfileChange, entries: dict) -> str:
    """What the human approves: every prefix with its full title, never prefixes alone."""
    if change.kind == "reset_defaults":
        return (
            f"Stop using research profile '{change.name}' and go back to the default "
            f"ontologies? The profile is kept, so you can switch back later."
        )
    heading = {
        "replace": f"Save research profile '{change.name}'?",
        "update": f"Change research profile '{change.name}'?",
        "restore_previous": f"Undo the last change to research profile '{change.name}'?",
    }[change.kind]
    lines = [heading]
    if change.before:
        for label, keys in (("Adding", change.added), ("Removing", change.removed)):
            if keys:
                lines += ["", f"{label}:"] + [_line(k, entries) for k in keys]
    lines += ["", f"Final list ({len(change.resulting)}):"]
    lines += [_line(k, entries) for k in change.resulting]
    return "\n".join(lines)


# --- reporting -------------------------------------------------------------


def active_report(target: str | None) -> dict:
    """What is in use after a write, as fields plus one sentence the model can relay."""
    active = registry.current()
    if target is None:
        note = f"Now using '{active.profile}' ({active.source})."
    elif active.profile == target:
        note = (
            f"'{target}' is active now — no restart needed. Searches are scoped to it; "
            f"get_term and the hierarchy tools still work for any CURIE."
        )
    elif active.source == "env":
        note = (
            f"Saved, but your client config pins '{active.profile}' via ONTOMCP_PROFILE, "
            f"so '{active.profile}' is still in use. Remove that setting to use '{target}'."
        )
    else:
        note = f"Saved, but '{active.profile}' is still in use ({active.source})."
    return {"active_profile": active.profile, "active_source": active.source, "next_step": note}


def _saved_payload(name: str, profile: dict) -> dict:
    """Report each member's title and term count, so the human sees what was written."""
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
    }


# --- the gate --------------------------------------------------------------


def _prepare(change: ProfileChange, entries: dict) -> tuple[ProfileChange, dict | None]:
    """Validate before asking: never ask a human to approve a list that cannot be saved."""
    if change.kind == "restore_previous":
        if not profile_mod.has_previous(change.name):
            return change, {
                "error": "nothing_to_undo",
                "detail": f"There is no earlier version of '{change.name}' to go back to.",
            }
        previous = profile_mod.previous_ontologies(change.name)
        current = profile_mod.current_ontologies(change.name)
        return ProfileChange("restore_previous", change.name, tuple(previous), tuple(current)), None
    if change.kind == "reset_defaults":
        return change, None
    canonical, _, errors = profile_mod.validate(list(change.resulting), {"ontologies": entries})
    if errors:
        return change, {"error": "profile_invalid", "detail": " ".join(errors)}
    before = tuple(registry.canonical_key(o) for o in change.before)
    return ProfileChange(
        change.kind,
        change.name,
        tuple(canonical),
        before,
        change.description,
        change.force_default,
    ), None


async def _ask(confirm: Confirm, message: str) -> tuple[str, dict | None]:
    """Return (method, outcome). An outcome means stop: nothing is written."""
    try:
        decision = await asyncio.wait_for(confirm(message), config.ELICIT_TIMEOUT_SECONDS)
    except TimeoutError:
        return "elicitation", {
            "status": "no_response",
            "detail": "No answer to the confirmation, so nothing was changed.",
        }
    except Exception as exc:  # a client bug must never fall through to a write
        logger.warning("profile confirmation failed: %s", exc)
        return "elicitation", {
            "error": "confirmation_failed",
            "detail": f"The confirmation could not be shown ({exc}), so nothing was changed.",
        }
    if decision != "accepted":
        return "elicitation", {
            "status": "declined",
            "detail": "You did not approve the change, so nothing was changed.",
        }
    return "elicitation", None


async def _apply(change: ProfileChange, client: OLSClient | None) -> dict:
    if change.kind == "reset_defaults":
        profile_mod.clear_active()
        return {"status": "reset", **active_report(None)}
    if change.kind == "restore_previous":
        profile_mod.restore_previous(change.name)
        profile_mod.set_active(change.name)
        return {
            "status": "restored",
            "profile": change.name,
            "ontologies": list(change.resulting),
            **active_report(change.name),
        }
    profile = await profile_mod.save(
        change.name,
        list(change.resulting),
        force_default=list(change.force_default),
        description=change.description,
        client=client,
    )
    profile_mod.set_active(change.name)
    return {"status": "saved", **_saved_payload(change.name, profile), **active_report(change.name)}


async def confirm_and_apply(
    change: ProfileChange,
    confirm: Confirm | None,
    *,
    client: OLSClient | None = None,
) -> dict:
    """Refuse, confirm, then write. Never raises; every failure is a structured dict."""
    refused = _refusal(change)
    if refused:
        return refused
    try:
        entries = registry.load_index()["ontologies"]
    except registry.IndexError_ as exc:
        return {"error": "profile_invalid", "detail": str(exc)}
    change, problem = _prepare(change, entries)
    if problem:
        return problem

    if confirm is not None:
        method, stop = await _ask(confirm, confirmation_message(change, entries))
        if stop:
            return {**stop, "confirmation": method}
    elif config.REQUIRE_CONFIRMATION:
        return _strict_refusal(change)
    else:
        method = "client_tool_permission"

    try:
        result = await _apply(change, client)
    except profile_mod.ProfileError as exc:
        return {"error": "profile_invalid", "detail": str(exc), "confirmation": method}
    return {**result, "confirmation": method}
