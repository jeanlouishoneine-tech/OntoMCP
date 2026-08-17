"""``ontomcp`` — the terminal path to everything the MCP tools do for profiles.

Nobody is required to use it: `save_profile` and the setup prompt cover the whole
flow from inside Claude. This exists for people who prefer a terminal, and because
every remediation string that names a command needs that command to exist.

    ontomcp profile list
    ontomcp profile show plant-breeding
    ontomcp profile create plant-breeding --ontologies PO,TO,PECO,GO,ENVO,UO
    ontomcp profile edit   plant-breeding --ontologies PO,TO,PECO,GO,ENVO,UO,NCBITAXON
    ontomcp profile check  plant-breeding
    ontomcp profile refresh plant-breeding
    ontomcp profile delete plant-breeding
    ontomcp profile suggest
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from ontomcp.core import profile as profile_mod
from ontomcp.core import registry


def cli_equivalent(args: str) -> str:
    """Render a runnable command string, falling back when ``ontomcp`` is not on PATH.

    Printed as a display-only field, never as an imperative line in a tool result:
    in Claude Code a shell line in tool output is an executable side effect.
    """
    if shutil.which("ontomcp"):
        return f"ontomcp {args}"
    root = Path(__file__).resolve().parent.parent.parent
    return f"uv run --directory {root} ontomcp {args}"


def _print_profile(name: str) -> int:
    try:
        profile = profile_mod.load(name)
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1

    resolved = registry.resolve(profile)
    derived = profile.get("_derived") or {}

    print(f"Profile: {name}")
    if profile.get("description"):
        print(f"  {profile['description']}")
    print(f"  frozen_at    {derived.get('frozen_at') or '(never frozen)'}")
    print(f"  index        {derived.get('index_version') or '(none)'}")
    print(f"  locked       {profile.get('locked', False)}")
    print()
    print("  Searched by default:")
    for key in resolved.default_scope:
        member = resolved.members.get(key, {})
        version = member.get("version") or ("unversioned" if member.get("unversioned") else "?")
        flag = "" if member.get("verified", True) else "  [unverified]"
        print(f"    {key:<12} {member.get('title', '')}  ({version}){flag}")

    if resolved.named_only:
        print()
        print("  In your profile but held back from default search:")
        for key in resolved.named_only:
            print(f"    {key:<12} {resolved.reasons.get(key, '')}")
            print(f'      search it:  search_terms("...", ontologies=["{key}"])')

    for warning in resolved.warnings:
        print(f"\n  ! {warning}")
    return 0


def _create(args, *, require_new: bool) -> int:
    try:
        exists = profile_mod.profile_path(args.name).exists()
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    if require_new and exists:
        print(
            f"Profile '{args.name}' already exists. Use 'profile edit' to change it.",
            file=sys.stderr,
        )
        return 1
    if not require_new and not exists and args.name not in profile_mod.starters():
        # `edit` on a starter name is a legitimate shadow-and-save, same as the
        # MCP `save_profile` tool already allows (profile.load() falls back to
        # STARTERS_DIR) — _not_found()'s own listing names starters as
        # available, so refusing them here contradicted the message it prints.
        print(profile_mod._not_found(args.name), file=sys.stderr)
        return 1

    codes = [c.strip() for c in args.ontologies.split(",") if c.strip()]
    forced = [c.strip() for c in (args.force_default or "").split(",") if c.strip()]
    try:
        asyncio.run(
            profile_mod.save(
                args.name,
                codes,
                force_default=forced,
                description=args.description or "",
                require_verified=args.require_verified,
            )
        )
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    return _print_profile(args.name)


def _check(args) -> int:
    """Re-probe every member live. This is where a round-trip is affordable."""
    try:
        profile = profile_mod.load(args.name)
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        index = registry.load_index()
    except registry.IndexError_ as exc:
        print(exc, file=sys.stderr)
        return 1
    derived = profile.get("_derived") or {}
    pinned = derived.get("index_version")
    if pinned and pinned != index["index_version"]:
        print(f"Index drift: frozen against {pinned}, shipped index is {index['index_version']}.")
        print(f"  Refresh with:  {cli_equivalent(f'profile refresh {args.name}')}")

    from ontomcp.core.ols_client import OLSClient

    async def probe_all():
        async with OLSClient() as cli:
            for key, member in (derived.get("members") or {}).items():
                live = await cli.fetch_ontology_version(key)
                pinned_version = member.get("version")
                if live and pinned_version and live != pinned_version:
                    print(f"  {key}: frozen at {pinned_version}, OLS now serves {live}")
                elif live is None:
                    # fetch_ontology_version collapses "unreachable" and "no
                    # version field" into one None (see profile.py's _probe,
                    # which records the distinction at freeze time as
                    # `unversioned` — trust that pinned flag here rather than
                    # re-deriving the wrong answer from the same collapsed value).
                    if member.get("unversioned"):
                        print(f"  {key}: up to date (unversioned)")
                    else:
                        print(f"  {key}: could not reach OLS")
                else:
                    print(f"  {key}: up to date ({live or 'unversioned'})")

    asyncio.run(probe_all())
    return 0


def _refresh(args) -> int:
    try:
        profile = profile_mod.load(args.name)
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1

    old = set((profile.get("_derived") or {}).get("members") or {})
    try:
        index = registry.load_index()
    except registry.IndexError_ as exc:
        print(exc, file=sys.stderr)
        return 1
    new = {registry.canonical_key(o) for o in profile["ontologies"]} & set(index["ontologies"])

    print(f"Refreshing '{args.name}' against index {index['index_version']}:")
    for key in sorted(old - new):
        print(f"  - {key} (no longer in the index)")
    for key in sorted(new - old):
        print(f"  + {key}")
    if old == new:
        print("  (membership unchanged; versions and probes will be re-observed)")

    if not args.yes and input("Rewrite _derived? [y/N] ").strip().lower() != "y":
        print("Cancelled. Nothing was written.")
        return 0

    try:
        asyncio.run(
            profile_mod.save(
                args.name,
                profile["ontologies"],
                force_default=profile.get("force_default"),
                description=profile.get("description", ""),
            )
        )
    except profile_mod.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    return _print_profile(args.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ontomcp", description="OntoMCP research profiles")
    sub = parser.add_subparsers(dest="group", required=True)
    profile_parser = sub.add_parser("profile", help="Manage research profiles")
    actions = profile_parser.add_subparsers(dest="action", required=True)

    actions.add_parser("list", help="List available profiles")
    actions.add_parser("suggest", help="Print the policy and the valid-ontology list")

    for verb in ("show", "delete", "check"):
        p = actions.add_parser(verb)
        p.add_argument("name")

    p = actions.add_parser("refresh")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    for verb in ("create", "edit"):
        p = actions.add_parser(verb)
        p.add_argument("name")
        p.add_argument("--ontologies", required=True, help="Comma-separated prefixes")
        p.add_argument("--force-default", default="", help="Comma-separated prefixes")
        p.add_argument("--description", default="")
        p.add_argument(
            "--require-verified",
            action="store_true",
            help="Refuse to freeze if any member could not be reached (regulated deployments)",
        )

    args = parser.parse_args(argv)

    if args.action == "list":
        names = profile_mod.available()
        print("\n".join(names) if names else "No profiles yet.")
        return 0
    if args.action == "suggest":
        from ontomcp.core.tools import get_profile_setup_guidance

        guidance, _ = get_profile_setup_guidance()
        print(guidance["policy"] or "(no policy file)")
        print(f"\n{len(guidance['grounding_list'])} searchable ontologies available.")
        print(f"Rules: {guidance['rules']}")
        return 0
    if args.action == "show":
        return _print_profile(args.name)
    if args.action == "create":
        return _create(args, require_new=True)
    if args.action == "edit":
        return _create(args, require_new=False)
    if args.action == "check":
        return _check(args)
    if args.action == "refresh":
        return _refresh(args)
    if args.action == "delete":
        try:
            profile_mod.delete(args.name)
        except profile_mod.ProfileError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Deleted {args.name}.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
