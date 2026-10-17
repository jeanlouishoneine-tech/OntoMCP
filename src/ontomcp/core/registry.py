"""The ontology registry: index validation, precedence, and scope resolution.

One module, four jobs, deliberately kept together because they are one concept —
"which ontologies is this session allowed to discover, and what are they called":

1. **Index** — load and validate ``data/ols4-index.json``, the generated list of
   every ontology OLS4 serves. Built by ``scripts/refresh_index.py``; read here
   with zero network.
2. **Precedence** — decide which of the five sources (§4.4) supplies scope.
3. **Resolution** — one pure ``resolve(profile)`` over *pinned inputs*. Freeze
   pins inputs, not outputs, so there is exactly one resolver and no comparator.
4. **Slug lookup** — the registry key -> OLS URL slug mapping, which used to be a
   module-level dict in ``ols_client`` with a ``.lower()`` fallback that
   fabricated plausible-but-wrong slugs.

Keys are UPPERCASE everywhere — registry, quarantine, profile, ``_derived``.
OLS's own casing survives only in ``slug``, ``curie_prefix``, ``base_uri`` and
``title``. See ``tournament/spike-results.md`` S-1 and S-6 for why this matters.

No network. Ever. The live re-check belongs to ``ontomcp profile check`` and
``/health``, which are the surfaces where a round-trip is affordable.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ontomcp.core.config import (
    INDEX_PATH,
    INDEX_SANITY_FLOOR,
    LARGE_ADVISORY,
    ONTOLOGIES,
    QUARANTINE,
)

logger = logging.getLogger("ontomcp")

# OLS CURIE prefix -> our registry key, for the handful where they differ. OLS
# mints HP:0000001 while the registry (and therefore every cached row, because
# normalize_curie applies this map) uses HPO. Kept here rather than in
# ols_client so index keys, profile members and cache rows share one source.
#
# ORPHANET -> ORDO: ORDO's own obo_id mints as "ORDO:NNN" (see policy.md), but
# xref annotations from OTHER ontologies (e.g. MONDO's
# database_cross_reference) spell the same reference "Orphanet:NNN" — the OBO
# foundry xref-database name, not ORDO's CURIE prefix. Without this alias,
# map_across_ontologies silently drops every curated ORDO xref it sees.
PREFIX_ALIASES = {"HP": "HPO", "ORPHANET": "ORDO"}


class IndexError_(Exception):
    """Raised when the committed index is missing or unusable. Never caught by tools."""


@dataclass(frozen=True)
class ResolvedRegistry:
    """The scope for this session: what is searched by default, what is held back.

    Frozen because nothing downstream may mutate resolved scope — a tool that
    could widen its own scope is the runtime fuzzy inference compile-once-freeze
    exists to forbid.
    """

    profile: str
    default_scope: tuple[str, ...]
    named_only: tuple[str, ...]
    members: dict[str, dict]
    index_version: str
    reasons: dict[str, str] = field(default_factory=dict)
    frozen_at: str | None = None
    locked: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def all_members(self) -> tuple[str, ...]:
        """Every member of the profile — default scope plus held-back. Used by bulk_annotate."""
        return tuple(self.default_scope) + tuple(self.named_only)

    def slug_for(self, code: str) -> str:
        """OLS URL slug for a registry key, or raise. Never guesses.

        The old ``_ONTOLOGY_SLUGS.get(code, code.lower())`` fallback fabricated a
        plausible slug for any unknown code, which OLS answered with HTTP 200 and
        zero documents — indistinguishable from "no such term".
        """
        member = self.members.get(canonical_key(code))
        if member is None or not member.get("slug"):
            raise KeyError(f"Unknown ontology code: {code!r}")
        return member["slug"]


# --- key canonicalization --------------------------------------------------


def canonical_key(code: str) -> str:
    """Uppercase an ontology code and apply the alias map. The one way to make a key.

    ``"go"`` -> ``"GO"``; ``"hp"`` -> ``"HPO"`` (OLS mints HP, we key HPO, and the
    cache stores HPO because normalize_curie applies the same map).
    """
    upper = code.strip().upper()
    return PREFIX_ALIASES.get(upper, upper)


# --- index building and validation ----------------------------------------


def _entry_key(ols_entry: dict, slug_to_registry: dict[str, str]) -> str:
    """Pick the uppercase index key for one OLS entry.

    Precedence: a built-in registry key wins (so HPO stays HPO, not HP), then
    ``config.preferredPrefix``, then ``ontologyId``. 28 of 286 entries have no
    preferredPrefix, so the last fallback is load-bearing, not defensive.
    """
    slug = ols_entry.get("ontologyId") or ""
    if slug in slug_to_registry:
        return slug_to_registry[slug]
    cfg = ols_entry.get("config") or {}
    return canonical_key(cfg.get("preferredPrefix") or slug)


def _is_searchable(terms: int | None, properties: int | None) -> tuple[bool, str | None]:
    """Advisory flag: is this ontology worth searching? Returns (searchable, review_note).

    ``not (properties > terms)``. RO (58 terms, 829 properties) is false; GO, CHEBI
    and NCBITaxon are true. Both counts zero is true *plus* a review note, because a
    present-and-zero count is OLS4's least trustworthy value and must never silently
    vanish an ontology. Absent metadata fails open to true.

    The flag never blocks: it warns at freeze, is suppressed from escape-hatch
    add-suggestions, and is omitted from the grounding list. Its failure mode is
    misclassification, so it must never be load-bearing.
    """
    if terms is None or properties is None:
        return True, None
    if terms == 0 and properties == 0:
        return True, "terms=0 and properties=0 — suspicious metadata"
    return not (properties > terms), None


def build_index(payload: dict) -> dict:
    """Build the committed index shape from a raw OLS4 /ontologies response.

    Pure: takes the parsed JSON body, returns the index dict. No network, no file
    IO, so it is unit-testable against the captured fixture.
    """
    entries = (payload.get("_embedded") or {}).get("ontologies") or []
    slug_to_registry = {meta["slug"]: key for key, meta in ONTOLOGIES.items()}

    ontologies: dict[str, dict] = {}
    review: list[str] = []

    for entry in entries:
        key = _entry_key(entry, slug_to_registry)
        cfg = entry.get("config") or {}
        terms = entry.get("numberOfTerms")
        properties = entry.get("numberOfProperties")
        searchable, note = _is_searchable(terms, properties)
        if note:
            review.append(f"{key}: {note}")
        if terms is not None and terms >= LARGE_ADVISORY and key not in QUARANTINE:
            review.append(f"{key}: {terms:,} terms, not in QUARANTINE — confirm it does not flood")

        record = {
            "slug": entry.get("ontologyId"),
            "title": cfg.get("title") or entry.get("ontologyId"),
            # The prefix OLS itself mints CURIEs with — "HP", not our key "HPO";
            # "NCBITaxon", not "NCBITAXON". Addressing /terms?obo_id= with our key
            # returns an empty list, which is indistinguishable from "no such term".
            "curie_prefix": cfg.get("preferredPrefix") or entry.get("ontologyId"),
            "terms": terms,
            "properties": properties,
            # 217 of 286 entries have no baseUris, and RO's is a BFO term IRI
            # rather than an RO stem — so this is a hint, never a source of truth.
            "base_uris": entry.get("baseUris") or cfg.get("baseUris") or [],
            "searchable": searchable,
        }

        if key in ontologies:
            # dpo and fbcv both declare preferredPrefix "FBcv". Deterministic
            # tie-break: the larger ontology keeps the key, the loser is re-keyed
            # on its slug, and both are named in review so nobody discovers this
            # by having a profile silently resolve to the wrong one.
            incumbent = ontologies[key]
            loser, winner = (
                (record, incumbent)
                if (incumbent.get("terms") or 0) >= (terms or 0)
                else (incumbent, record)
            )
            ontologies[key] = winner
            ontologies[canonical_key(loser["slug"])] = loser
            review.append(
                f"{key}: prefix collision between '{winner['slug']}' and '{loser['slug']}'; "
                f"'{winner['slug']}' keeps the key, '{loser['slug']}' is keyed "
                f"{canonical_key(loser['slug'])}"
            )
        else:
            ontologies[key] = record

    index = {
        "backend": "ols4",
        "generated_at": None,  # filled by the refresh script
        "index_version": None,  # filled below, over the canonicalized content
        "ontologies": dict(sorted(ontologies.items())),
        "review": sorted(review),
    }
    index["index_version"] = _index_version(index)
    return index


def _index_version(index: dict) -> str:
    """``YYYY.MM+<sha256[:8]>`` over the canonical content, so same-month drift shows."""
    from datetime import UTC, datetime

    body = json.dumps(index["ontologies"], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()[:8]
    return f"{datetime.now(UTC):%Y.%m}+{digest}"


def validate_index(
    payload: dict,
    *,
    previous: dict | None = None,
    allow_removals: tuple[str, ...] = (),
) -> tuple[dict, list[str]]:
    """Build an index from an OLS response and refuse it if it looks degraded.

    Returns ``(index, problems)``. A non-empty ``problems`` list means the caller
    must NOT overwrite the committed index — a partial OLS response that silently
    shrank the index would orphan every frozen profile that references a dropped
    prefix.

    The checks, and why each exists:

    - **Sanity floor** — fewer than INDEX_SANITY_FLOOR entries is a degraded
      response, not a real shrinkage (OLS4 served 286 on 2026-08-15).
    - **totalElements agreement** — a truncated page reports the real total, so
      this catches a paging failure the floor would miss.
    - **Anchors present** — derived from the built-in registry's own slugs rather
      than hand-typed, which is how MESH went missing from an earlier hand-typed list.
    - **Enumerated removals** — an ontology present in the committed index and
      absent from the response must be named in ``allow_removals`` explicitly.
    - **Anchor base_uri stability** — a changed IRI stem produces wrong IRIs
      rather than a suspicious diff. Only checked for anchors that have one:
      76% of OLS4 entries ship no baseUris at all.
    """
    problems: list[str] = []
    entries = (payload.get("_embedded") or {}).get("ontologies") or []
    total = (payload.get("page") or {}).get("totalElements")

    if len(entries) < INDEX_SANITY_FLOOR:
        problems.append(
            f"only {len(entries)} ontologies returned; floor is {INDEX_SANITY_FLOOR}. "
            "Refusing to overwrite the index with a partial response."
        )
    if total is not None and len(entries) != total:
        problems.append(
            f"got {len(entries)} entries but page.totalElements says {total} — "
            "the response is paged or truncated."
        )

    index = build_index(payload)
    built = index["ontologies"]

    for key, meta in ONTOLOGIES.items():
        if key not in built:
            problems.append(f"anchor {key} (slug '{meta['slug']}') is missing from the response.")

    if previous:
        for key in previous.get("ontologies", {}):
            if key not in built and key not in allow_removals:
                problems.append(
                    f"{key} is in the committed index but absent from the response. "
                    f"If OLS4 really dropped it, re-run with --allow-removals={key}."
                )
        for key in ONTOLOGIES:
            old = (previous.get("ontologies") or {}).get(key, {}).get("base_uris") or []
            new = built.get(key, {}).get("base_uris") or []
            if old and new and old != new:
                problems.append(f"anchor {key} base_uri changed: {old} -> {new}. IRIs would move.")

    return index, problems


# --- index loading (cached on path/mtime/size) -----------------------------

_index_cache: dict[tuple, dict] = {}


def _stat_key(path: Path) -> tuple:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load_index(path: Path = INDEX_PATH) -> dict:
    """Read the committed index, cached on ``(path, mtime, size)``.

    Not an import-time singleton: an mtime key re-reads a *frozen artifact* and
    infers nothing, so determinism survives while a profile or index edit is
    picked up without a client restart. Documenting a required restart would
    document the abandonment point instead of removing it.
    """
    path = Path(path)
    if not path.exists():
        raise IndexError_(
            f"Ontology index not found at {path}. Run: python scripts/refresh_index.py"
        )
    key = _stat_key(path)
    cached = _index_cache.get(key)
    if cached is None:
        cached = json.loads(path.read_text())
        _index_cache.clear()  # only ever one index; don't grow across edits
        _index_cache[key] = cached
    return cached


# --- resolution ------------------------------------------------------------


def did_you_mean(code: str, entries: dict | None = None, limit: int = 3) -> list[str]:
    """Nearest known prefixes to a code OLS4 does not serve. No new dependency.

    Lives here because both callers already import this module and both mean the
    same thing by it — the runtime rejection in ``normalize_ontologies`` and the
    freeze-time validation in ``profile.validate``. Only the *formatting* differs
    between them, and that belongs at the call sites, which is why this returns a
    plain list rather than prose.

    ``entries`` defaults to the committed index; pass it explicitly to avoid a
    second read when the caller already has it.
    """
    from rapidfuzz import process

    if entries is None:
        try:
            entries = load_index().get("ontologies") or {}
        except Exception:
            return []
    if not entries:
        return []
    return [m[0] for m in process.extract(code, list(entries), limit=limit, score_cutoff=70)]


def minting_curie(curie: str, index_path: Path = INDEX_PATH) -> str:
    """Rewrite a canonical CURIE into the prefix OLS mints it with, for outbound calls.

    ``HPO:0001250`` -> ``HP:0001250``; ``NCBITAXON:4932`` -> ``NCBITaxon:4932``.

    This is **read-side only**, and the distinction is load-bearing. The canonical
    uppercase CURIE is the cache primary key; rewriting *that* would orphan every
    previously cached row. This rewrites only the value sent to OLS in an
    ``obo_id`` parameter, where OLS insists on its own spelling and answers an
    unrecognised prefix with an empty list — indistinguishable from "no such term".
    """
    prefix, _, local_id = curie.partition(":")
    if not local_id:
        return curie
    entry = (load_index(index_path).get("ontologies") or {}).get(canonical_key(prefix))
    minted = (entry or {}).get("curie_prefix") or prefix
    return f"{minted}:{local_id}"


def slug_for(code: str, index_path: Path = INDEX_PATH) -> str:
    """OLS URL slug for any ontology the index knows. Raises on an unknown code.

    Looks up the **index**, not the resolved profile, because the profile bounds
    discovery and never bounds resolution: ``get_term``, the hierarchy tools and
    ``validate_term`` must work for a pasted CURIE from any of the ~286, whether
    or not it is in scope.

    Raising is the point. The dict this replaces fell back to ``code.lower()``,
    which fabricated a plausible slug that OLS answered with HTTP 200 and zero
    documents — indistinguishable from "no such term", and impossible to debug.
    """
    key = canonical_key(code)
    entry = (load_index(index_path).get("ontologies") or {}).get(key)
    if entry is None or not entry.get("slug"):
        raise KeyError(f"Unknown ontology code: {code!r} (not in the OLS4 index)")
    return entry["slug"]


def resolve(profile: dict) -> ResolvedRegistry:
    """Resolve a profile's pinned inputs into a scope. Pure, offline, one path.

    Freeze pins *inputs* (``index_version``, the per-profile quarantine map, the
    alias map, per-member probe results) rather than outputs, so this function is
    the single resolver for every precedence row — pinned profiles and the
    synthesized built-in/env blocks alike. There is no second path and no
    comparator, because two paths plus a comparator is three things that can
    disagree.
    """
    derived = profile.get("_derived") or {}
    members: dict[str, dict] = derived.get("members") or {}
    quarantine: dict[str, str] = derived.get("quarantine") or {}
    forced = {canonical_key(o) for o in profile.get("force_default") or []}

    held = [m for m in members if m in quarantine and m not in forced]
    return ResolvedRegistry(
        profile=profile.get("name", "default"),
        default_scope=tuple(m for m in members if m not in held),
        named_only=tuple(held),
        reasons={m: quarantine[m] for m in held},
        members=members,
        index_version=derived.get("index_version") or "",
        frozen_at=derived.get("frozen_at"),
        locked=bool(profile.get("locked", False)),
        warnings=tuple(derived.get("warnings") or ()),
    )


def _members_from_index(codes: list[str], index: dict) -> tuple[dict, list[str]]:
    """Project index entries into a ``_derived.members`` block. Returns (members, warnings).

    This is A2's mandatory load-time check: the set-difference of the requested
    codes against the shipped index, naming every miss. It is what makes deleting
    ``fts_search``'s registry filter safe — that filter was the last layer that
    would have caught a mistyped profile member, and it caught it by silently
    returning nothing.
    """
    entries = index.get("ontologies") or {}
    members: dict[str, dict] = {}
    warnings: list[str] = []
    for raw in codes:
        key = canonical_key(raw)
        entry = entries.get(key)
        if entry is None:
            warnings.append(
                f"{key} is not in the ontology index (index_version "
                f"{index.get('index_version')}) — a typo, or OLS4 no longer serves it."
            )
            continue
        members[key] = {
            "slug": entry["slug"],
            "title": entry["title"],
            "terms": entry["terms"],
            "curie_prefix": entry.get("curie_prefix"),
            "base_uri": (entry.get("base_uris") or [None])[0],
            "version": None,
            "verified": False,
        }
    return members, warnings


def _synthesize(name: str, codes: list[str], index: dict, *, quarantined: bool) -> dict:
    """Build a profile-shaped dict for a source that has no frozen ``_derived`` block.

    Used by precedence rows 1 (env lock) and 5 (built-in default). ``quarantined``
    is False for the env lock: an admin naming an ontology is the most explicit
    signal in the system, and applying a quarantine heuristic on top of it is the
    design second-guessing the one actor it has no business second-guessing.
    """
    members, warnings = _members_from_index(codes, index)
    return {
        "name": name,
        "locked": name == "env-lock",
        "_derived": {
            "index_version": index.get("index_version") or "",
            "frozen_at": None,
            "members": members,
            "quarantine": (
                {k: v for k, v in QUARANTINE.items() if k in members} if quarantined else {}
            ),
            "warnings": warnings,
        },
    }


def _env_codes() -> list[str] | None:
    raw = os.environ.get("ONTOMCP_ONTOLOGIES", "").strip()
    if not raw:
        return None
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return codes or None


def _drift_notes(profile: dict, index: dict, db_path: Path | None) -> list[str]:
    """Surface index and ontology-version drift. Never applies it, never calls the network.

    Drift is offered, not applied: silently re-resolving a frozen profile because
    the shipped index moved is exactly the reproducibility hole compile-once-freeze
    exists to close. Version comparison uses only ``ontology_versions`` rows the
    cache already holds — the live re-check belongs to ``profile check`` and
    ``/health``, where a round-trip is affordable.
    """
    notes: list[str] = []
    derived = profile.get("_derived") or {}
    pinned_index = derived.get("index_version")
    if pinned_index and pinned_index != index.get("index_version"):
        notes.append(
            f"Profile '{profile.get('name')}' was frozen against index {pinned_index}; "
            f"the shipped index is {index.get('index_version')}. Your scope is unchanged. "
            f"Ask me to refresh the profile to pick up the new index."
        )
    for key in derived.get("members") or {}:
        if key not in (index.get("ontologies") or {}):
            notes.append(
                f"{key} was in your profile at freeze but is no longer in the index — "
                f"OLS4 may have withdrawn it. Searches scoped to it will return nothing."
            )
    if db_path is not None:
        try:
            from ontomcp.core import cache

            observed = cache.get_all_ontology_versions(db_path)
        except Exception:
            observed = {}
        for key, member in (derived.get("members") or {}).items():
            pinned = member.get("version")
            seen = observed.get(key)
            if pinned and seen and pinned != seen:
                notes.append(f"{key}: frozen at version {pinned}, cache has seen {seen}.")
    return notes


def _profile_name() -> str | None:
    name = os.environ.get("ONTOMCP_PROFILE", "").strip()
    return name or None


def current(index_path: Path = INDEX_PATH, db_path: Path | None = None) -> ResolvedRegistry:
    """Resolve the active registry by the §4.4 precedence table.

    Every row goes through the same ``resolve()``. Fail open at every step: a
    broken profile degrades to the built-in default with a warning on the response,
    and never refuses to start. That includes the index itself — a missing or
    unreadable committed index degrades to an empty scope with a warning rather
    than raising ``IndexError_`` out of this function, because every caller in the
    tool layer (search, hierarchy, health, the Jupyter panel) resolves scope
    through here and none of them may crash on it.
    """
    from ontomcp.core.config import DB_PATH

    try:
        index = load_index(index_path)
    except IndexError_ as exc:
        logger.error("registry.current(): %s", exc)
        return ResolvedRegistry(
            profile="default",
            default_scope=(),
            named_only=(),
            members={},
            index_version="",
            warnings=(str(exc),),
        )
    db_path = DB_PATH if db_path is None else db_path

    codes = _env_codes()
    if codes is not None:
        # Row 1 — admin hard-lock. Verbatim: no quarantine split, no force_default.
        return resolve(_synthesize("env-lock", codes, index, quarantined=False))

    # Rows 2-4: a named profile, then default.json.
    from ontomcp.core import profile as profile_mod

    name = _profile_name()
    warnings: list[str] = []
    if name is None and profile_mod.profile_path("default").exists():
        name = "default"

    if name is not None:
        try:
            profile = profile_mod.load(name)
        except profile_mod.ProfileError as exc:
            warnings.append(str(exc))
        else:
            if not (profile.get("_derived") or {}).get("members"):
                # Hand-written, never frozen: resolve computed-at-load, with a
                # warning naming the fix. Fail open; never refuse to start.
                warnings.append(
                    f"Profile '{name}' has never been frozen, so its members are unverified. "
                    f"Ask me to save the profile again to freeze it."
                )
                synthesized = _synthesize(name, profile["ontologies"], index, quarantined=True)
                synthesized["force_default"] = profile.get("force_default", [])
                synthesized["locked"] = profile.get("locked", False)
                profile = synthesized
            notes = _drift_notes(profile, index, db_path)
            derived = profile.setdefault("_derived", {})
            derived["warnings"] = list(derived.get("warnings") or []) + warnings + notes
            return resolve(profile)

    # Row 5 — zero-config. No QUARANTINE member is among the built-ins, so the
    # split is provably a no-op here; it is applied anyway so there is one path.
    fallback = _synthesize("default", list(ONTOLOGIES), index, quarantined=True)
    fallback["_derived"]["warnings"] += warnings
    return resolve(fallback)
