"""Shared helpers for the tool layer: ontology normalization and client lifecycle.

Kept tiny on purpose. Business logic lives in the individual tool modules.
"""

from contextlib import asynccontextmanager

from ontomcp.core import registry
from ontomcp.core.ols_client import OLSClient, normalize_curie


def safe_normalize_curie(curie: str) -> tuple[str | None, dict | None]:
    """Normalize a CURIE, returning (normalized, None) or (None, error_dict).

    Tools must never raise; an unparseable CURIE becomes a structured error.
    Callers guard with ``if norm is None: return err_or_default(err), False`` so
    the success path has ``norm`` typed as ``str``.
    """
    try:
        return normalize_curie(curie), None
    except (ValueError, AttributeError) as exc:
        return None, {"error": "bad_curie", "detail": str(exc), "curie": curie}


def normalize_ontologies(
    ontologies: list[str] | None,
    *,
    scope: list[str] | None = None,
) -> tuple[list[str] | None, list[dict]]:
    """Canonicalize requested ontology codes against the active scope.

    Returns ``(resolved, rejected)``. ``resolved`` is None only when the caller
    supplied no filter at all — it never again means "every code you gave me was
    junk, so search everything", which is how a typo used to silently widen scope
    to the whole registry.

    Aliases are applied **before** the membership test, so ``["HP"]`` resolves to
    HPO rather than being rejected as unknown: OLS mints HP, the registry keys
    HPO, and a user pasting either means the same ontology.

    ``scope`` defaults to the resolved registry's members. This is the single
    enforcement point — MCP, REST and the Jupyter panel all pass through here, so
    none of them can disagree about what is in scope or bypass the check.
    """
    if not ontologies:
        return None, []

    if scope is None:
        try:
            resolved_registry = registry.current()
        except Exception:
            # No index yet (fresh checkout, mid-install). Fail open rather than
            # refusing to start: a missing index must never break a search.
            scope = None
        else:
            members = list(resolved_registry.all_members)
            # registry.current() itself fails open (never raises) and returns an
            # empty, warning-carrying registry when the index is unreadable — so
            # that case no longer reaches the `except` above. An empty scope
            # *with* a warning is that same failure, not a legitimately empty
            # profile, and must fail open the same way: an unreadable index must
            # never block an explicitly named search.
            scope = None if (not members and resolved_registry.warnings) else members

    resolved: list[str] = []
    rejected: list[dict] = []
    for raw in ontologies:
        key = registry.canonical_key(raw)
        if scope is not None and key not in scope:
            rejected.append(_reject(key, raw))
            continue
        if key not in resolved:
            resolved.append(key)
    return (resolved or None), rejected


def _reject(key: str, requested: str) -> dict:
    """Build the rejection, distinguishing a real ontology from a typo.

    This split is what makes the rejection useful rather than punitive. An
    out-of-profile prefix is a real ontology the user could add in one step; an
    unknown prefix is a mistake. Returning the same error for FOODON and MOONDO
    tells the model nothing and leaves the user stuck.

    Scope is still refused in both cases. The profile bounds *discovery*: if it
    merely defaulted it, the cap of 12 would be decorative and the model could
    search any prefix it invented — the runtime fuzzy inference compile-once-freeze
    exists to forbid. Resolution stays unrestricted (get_term, hierarchy,
    validate_term), which is where reachability actually lives.
    """
    try:
        entries = registry.load_index().get("ontologies") or {}
    except Exception:
        entries = {}

    entry = entries.get(key)
    if entry is not None:
        return {
            "error": "out_of_profile",
            "ontology": key,
            "title": entry.get("title"),
            "remediation": f"Ask me to add {key} to your profile.",
        }

    return {
        "error": "unknown_ontology",
        "ontology": key,
        "did_you_mean": registry.did_you_mean(key, entries),
        "remediation": "Check the prefix — OLS4 does not serve an ontology by that name.",
    }


@asynccontextmanager
async def ols_client(client: OLSClient | None):
    """Yield ``client`` if provided, else a fresh OLSClient closed on exit.

    Lets every tool accept an optional injected client (tests, bulk reuse) while
    owning the lifecycle of a default one.
    """
    if client is not None:
        yield client
        return
    async with OLSClient() as owned:
        yield owned


def build_meta(
    resolved,
    scope: list[str] | None,
    *,
    source: str,
    filtered_out: list[str] | None = None,
) -> dict:
    """Build the ``search_terms`` meta block. The only place this dict is constructed.

    ``source``, ``profile`` and ``scope`` are always present: a model plans from
    append-only context, so a scope stated once scrolls away and the model starts
    guessing. ``held_back``, ``filtered_out`` and ``notice`` attach only when
    non-empty, so the common response stays small.

    ``source`` is ``"cache"`` or ``"ols"``. It was already computed and thrown
    away at every MCP wrapper (``result, _ =``), so surfacing it costs nothing new.
    """
    meta: dict = {
        "source": source,
        "profile": resolved.profile if resolved else "default",
        "scope": list(scope or []),
    }
    if resolved and resolved.named_only:
        meta["held_back"] = {code: resolved.reasons.get(code, "") for code in resolved.named_only}
    if filtered_out:
        meta["filtered_out"] = sorted(filtered_out)
    if resolved and resolved.warnings:
        meta["notice"] = list(resolved.warnings)
    return meta


def is_error(result) -> bool:
    """True if an OLS call returned a structured error (dict, or list of them)."""
    if isinstance(result, dict):
        return "error" in result
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return "error" in result[0]
    return False
