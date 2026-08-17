"""Research profiles: read, write, validate, freeze.

A profile is a JSON file in ``~/.ontomcp/profiles/``. The scientist authors **two
lines** — ``ontologies``, and rarely ``force_default``. Everything under
``_derived`` is compiler output, written by freeze and never hand-edited.

**Freeze pins inputs, not outputs.** It records the index version, the per-profile
quarantine map, the alias map and the per-member probe results. It resolves
nothing — ``registry.resolve()`` does that, once, from the pinned inputs. Pinning
outputs instead would mean two resolution paths plus a comparator, which is three
things that can disagree.

Freeze proceeds on transport failure and records ``verified: false``. A hard stop
at the single onboarding moment, for OLS's downtime or a corporate proxy, is the
abandonment point. Prefix validity is checked against the committed index with
zero network, so an offline freeze is still a *correct* freeze — it just has not
observed the ontology's version or minting prefix yet, and ``profile check``
retries.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ontomcp.core import registry
from ontomcp.core.config import (
    PROFILE_MAX,
    PROFILE_WARN,
    QUARANTINE,
)
from ontomcp.core.ols_client import OLSClient, normalize_curie

logger = logging.getLogger("ontomcp")

SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def profiles_dir() -> Path:
    """``~/.ontomcp/profiles/``, or ``ONTOMCP_PROFILE_DIR`` when set (tests, teams)."""
    raw = os.environ.get("ONTOMCP_PROFILE_DIR")
    base = Path(raw).expanduser() if raw else Path.home() / ".ontomcp" / "profiles"
    return base


def profile_path(name: str) -> Path:
    """The on-disk path for a profile, after validating its name.

    Centralized here so load/save/delete can't diverge on which of them
    enforces this: a name containing path segments (e.g. ``../``) must never
    reach the filesystem call in any of them.
    """
    if not _NAME_RE.match(name or ""):
        raise ProfileError(
            f"'{name}' is not a valid profile name. Use lowercase letters, digits and "
            f"hyphens, e.g. 'plant-breeding'."
        )
    return profiles_dir() / f"{name}.json"


def available() -> list[str]:
    directory = profiles_dir()
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


class ProfileError(Exception):
    """A profile could not be read, validated or written. Carries user-facing prose."""


# --- messages --------------------------------------------------------------
# Written here rather than at each raise site so every degraded state has one
# agreed wording. Degraded state must ride on the response: stdio stderr is
# invisible in Claude Desktop, so a message nobody sees is a message that does
# not exist.


def _not_found(name: str) -> str:
    names = available() + [f"{s} (starter)" for s in starters()]
    listing = ", ".join(names) if names else "none yet"
    return (
        f"Profile '{name}' not found in {profiles_dir()}. Available: {listing}. "
        f"Ask me to create it, or unset ONTOMCP_PROFILE to use the default ontologies."
    )


def _malformed(name: str, detail: str) -> str:
    return (
        f"Profile '{name}' is not valid JSON ({detail}). Fix {profile_path(name)} by hand, "
        f"or ask me to recreate the profile."
    )


def _too_many(count: int) -> str:
    return (
        f"{count} ontologies exceeds the hard cap of {PROFILE_MAX}. A focused profile is the "
        f"point: 5-8 is the working range. Drop the ones you reach for least — anything you "
        f"leave out is still resolvable by CURIE, just not searched by default."
    )


# --- read ------------------------------------------------------------------


STARTERS_DIR = Path(__file__).parent.parent / "starters"


def starters() -> list[str]:
    """Names of the shipped starter profiles."""
    return sorted(p.stem for p in STARTERS_DIR.glob("*.json")) if STARTERS_DIR.exists() else []


def load(name: str) -> dict:
    """Read a profile from disk, falling back to a shipped starter of that name.

    Starters are read-only templates: naming one in ``ONTOMCP_PROFILE`` works with
    no setup at all, and saving under that name writes a normal user profile that
    shadows it from then on. That is what makes "seven shipped starters" remove the
    description step rather than merely document it.
    """
    path = profile_path(name)
    if not path.exists():
        starter = STARTERS_DIR / f"{name}.json"
        if starter.exists():
            profile = json.loads(starter.read_text())
            profile["name"] = name
            return profile
        raise ProfileError(_not_found(name))
    try:
        profile = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ProfileError(_malformed(name, str(exc))) from exc
    if not isinstance(profile, dict) or "ontologies" not in profile:
        raise ProfileError(_malformed(name, "missing an 'ontologies' list"))
    profile.setdefault("name", name)
    return profile


# --- validate --------------------------------------------------------------


def validate(codes: list[str], index: dict) -> tuple[list[str], list[str], list[str]]:
    """Check requested prefixes against the committed index. Zero network.

    Returns ``(canonical, warnings, errors)``. Unknown prefixes are errors —
    validation is the point of the index. Non-searchable and quarantined members
    are warnings: the user still gets them if they insist, because the
    ``searchable`` flag's failure mode is misclassification and must never block.
    """
    entries = index.get("ontologies") or {}
    canonical: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    for raw in codes:
        key = registry.canonical_key(raw)
        entry = entries.get(key)
        if entry is None:
            suggestion = registry.did_you_mean(key, entries)
            hint = f" Did you mean {', '.join(suggestion)}?" if suggestion else ""
            errors.append(f"{raw} is not an ontology OLS4 serves.{hint}")
            continue
        if key in canonical:
            continue
        canonical.append(key)
        if not entry.get("searchable"):
            warnings.append(
                f"{key} ({entry['title']}) is property-dominant "
                f"({entry['properties']} properties vs {entry['terms']} terms) — "
                f"searches will return little."
            )
        if key in QUARANTINE:
            warnings.append(
                f"{key} is held back from default search: {QUARANTINE[key]}. "
                f"Search it by naming it, or add it to force_default."
            )

    if "DOID" in canonical and "MONDO" not in canonical:
        # MONDO is the integration layer over DOID; a DOID-only disease profile
        # will miss the cross-references most disease work depends on. A warning,
        # not a refusal — some teams have a real reason.
        # Deliberately NOT warned: NCIT without MONDO. That is the correct
        # CDISC/NCI-EVS configuration and the commonest real pharma setup.
        warnings.append(
            "DOID is present without MONDO. MONDO integrates DOID and is usually the "
            "better primary disease ontology — consider adding it."
        )

    if len(canonical) > PROFILE_MAX:
        errors.append(_too_many(len(canonical)))
    elif len(canonical) > PROFILE_WARN:
        warnings.append(
            f"{len(canonical)} ontologies is above the recommended {PROFILE_WARN}; "
            f"every extra member adds noise to every query."
        )
    return canonical, warnings, errors


# --- freeze ----------------------------------------------------------------


async def _probe(code: str, slug: str, client: OLSClient) -> dict:
    """Observe one member against OLS: version, title, minting prefix, IRI stem.

    Two calls per member, so a 12-member profile costs at most 24 requests once,
    at setup. Never raises — a failure records ``verified: false`` and the profile
    is still written and still usable.
    """
    probe: dict = {"version": None, "verified": False, "curie_prefix": None, "base_uri": None}

    # Fetched directly here rather than through OLSClient.fetch_ontology_version,
    # which deliberately collapses "unreachable" and "no version field" into one
    # None (best-effort, used elsewhere where the distinction does not matter).
    # This probe's whole point is to record that distinction, so it needs to see
    # the failure mode before it collapses.
    try:
        response = await client._get(f"/ontologies/{slug}")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        body = None

    if body is not None:
        version = body.get("version")
        if not version:
            config_block = body.get("config") or {}
            version = config_block.get("version") if isinstance(config_block, dict) else None
        if version is None:
            # OLS answered, this ontology has no version string — distinct from
            # not having reached OLS at all. Conflating them tells a user their
            # profile is broken when the ontology is simply unversioned. A
            # failed request above leaves `body` None instead, so a transient
            # failure never gets mislabeled as "unversioned" here.
            probe["unversioned"] = True
        else:
            probe["version"] = version
        probe["verified"] = True

    # size=5, not 1: the first entry of /terms is frequently a non-class artifact.
    # ORDO's is a Creative Commons licence node with an empty obo_id, which would
    # record curie_prefix "" for every clinical-rare-disease profile ever frozen.
    try:
        response = await client._get(f"/ontologies/{slug}/terms", params={"size": 5})
        response.raise_for_status()
        terms = response.json().get("_embedded", {}).get("terms", [])
    except Exception:
        probe["verified"] = False
        return probe
    probe["verified"] = True  # reached OLS here even if the version call above did not

    for term in terms:
        raw = term.get("obo_id") or term.get("short_form")
        if not raw:
            continue
        try:
            normalize_curie(raw)  # validation only: rejects an unparseable short-form
        except ValueError:
            continue
        # The real minting prefix, not the canonical registry key: normalize_curie
        # applies PREFIX_ALIASES (e.g. HP -> HPO), which is exactly the alias this
        # probe exists to observe, not re-apply.
        sep = ":" if ":" in raw else "_"
        probe["curie_prefix"] = raw.split(sep, 1)[0]
        iri = term.get("iri")
        local = raw.split(sep, 1)[-1]
        if iri and local and iri.endswith(local):
            probe["base_uri"] = iri[: -len(local)]
        break

    return probe


async def freeze(
    profile: dict,
    *,
    client: OLSClient | None = None,
    require_verified: bool = False,
) -> dict:
    """Pin the inputs a resolver needs, and return the profile with ``_derived`` written.

    Writes: the index version, the alias map, the per-profile quarantine map (so a
    scientist reading their own JSON sees which of *their* ontologies is held back
    and why), and one probe result per member.
    """
    from ontomcp.core.tools._common import ols_client

    try:
        index = registry.load_index()
    except registry.IndexError_ as exc:
        raise ProfileError(str(exc)) from exc
    codes = profile["ontologies"]
    canonical, warnings, errors = validate(codes, index)
    if errors:
        raise ProfileError(" ".join(errors))

    entries = index["ontologies"]
    members: dict[str, dict] = {}

    async with ols_client(client) as cli:
        for key in canonical:
            entry = entries[key]
            probe = await _probe(key, entry["slug"], cli)
            members[key] = {
                "slug": entry["slug"],
                "title": entry["title"],
                "terms": entry["terms"],
                "curie_prefix": probe["curie_prefix"] or key,
                # The probed stem wins over the index's base_uris: 217 of 286
                # entries ship none, and RO's is a BFO term IRI.
                "base_uri": probe["base_uri"] or (entry.get("base_uris") or [None])[0],
                "version": probe["version"],
                "verified": probe["verified"],
            }
            if probe.get("unversioned"):
                members[key]["unversioned"] = True

    unverified = [k for k, m in members.items() if not m["verified"]]
    if unverified and require_verified:
        raise ProfileError(
            f"--require-verified: could not reach OLS for {', '.join(unverified)}. "
            f"Retry when the network is available, or drop the flag to freeze anyway."
        )
    if unverified:
        warnings.append(
            f"Could not reach OLS for {', '.join(unverified)} — frozen unverified. "
            f"Run 'ontomcp profile check' when you are back online."
        )

    profile["ontologies"] = codes  # keep the user's own spelling in the authored list
    profile["_derived"] = {
        "frozen_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "index_version": index["index_version"],
        "aliases": {k: v for k, v in registry.PREFIX_ALIASES.items() if v in members},
        "quarantine": {k: v for k, v in QUARANTINE.items() if k in members},
        # Sorted here so the in-memory profile this function returns already
        # matches the alphabetical order `save()` produces on disk via
        # json.dumps(..., sort_keys=True) — otherwise the same freeze event
        # exposes two different member orderings depending on whether the
        # caller sees the pre-write object or a reloaded one.
        "members": dict(sorted(members.items())),
        "warnings": warnings,
    }
    return profile


# --- write -----------------------------------------------------------------


async def save(
    name: str,
    ontologies: list[str],
    *,
    force_default: list[str] | None = None,
    description: str = "",
    client: OLSClient | None = None,
    require_verified: bool = False,
) -> dict:
    """Create or update a profile, always freezing. There is no unfrozen state.

    ``create`` and ``edit`` are the same verb here on purpose: it removes a step
    from the user's workflow rather than adding one, and it makes "unfrozen" mean
    "hand-written" and nothing else.
    """
    existing = load(name) if profile_path(name).exists() else {}
    if existing.get("locked"):
        raise ProfileError(
            f"Profile '{name}' is locked by an administrator and cannot be changed here. "
            f"Ask whoever distributed it, or create a profile under a different name."
        )

    profile = {
        "schema": SCHEMA_VERSION,
        "name": name,
        "description": description or existing.get("description", ""),
        "created_at": existing.get("created_at") or datetime.now(UTC).strftime("%Y-%m-%d"),
        "locked": False,
        "notes": existing.get("notes", ""),
        "ontologies": ontologies,
        "force_default": [registry.canonical_key(o) for o in (force_default or [])],
    }
    profile = await freeze(profile, client=client, require_verified=require_verified)

    path = profile_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file in the same directory, then rename: an interrupted
    # write must not leave a half-parsed profile that fails to load at startup.
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    temp.replace(path)
    logger.info("Wrote profile %s (%d members)", name, len(profile["_derived"]["members"]))
    return profile


def delete(name: str) -> None:
    path = profile_path(name)
    if not path.exists():
        raise ProfileError(_not_found(name))
    if load(name).get("locked"):
        raise ProfileError(f"Profile '{name}' is locked and cannot be deleted here.")
    path.unlink()
