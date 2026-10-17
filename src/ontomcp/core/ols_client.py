"""Async httpx client for the EBI OLS4 API.

Pure OLS: this module talks to the network and returns parsed dicts. It does not
read or write SQLite — cache-first orchestration lives in the Phase 4 tools.

Public methods never raise: on failure they return a structured ``{"error": ...}``
dict (or list of those) so tools never see an unhandled exception.

CURIE rules (see project CLAUDE.md):
- Store/return uppercase prefix, e.g. ``GO:0008219``.
- Strip a leading ``obo:`` if OLS returns it.
- The config registry uses the key ``HPO`` while OLS CURIEs/IRIs use ``HP``
  (``HP:0000001`` / ``HP_0000001``). We map ``HP`` -> ``HPO`` at the CURIE prefix
  layer so the registry key resolves; the IRI template for ``HPO`` already yields
  ``HP_{id}``.
"""

import asyncio
import logging
import re
from urllib.parse import quote

import httpx

from ontomcp.core import config, registry

logger = logging.getLogger("ontomcp")

# OLS CURIE prefix -> registry key. Only HP differs from its registry key.
# Sourced from core.registry so index keys, profile members and cache rows cannot
# drift apart (see tournament/spike-results.md S-6).
_PREFIX_ALIASES = registry.PREFIX_ALIASES

# OLS obo_synonym scope -> our synonym bucket.
_SYNONYM_SCOPES = {
    "hasExactSynonym": "exact",
    "hasRelatedSynonym": "related",
    "hasNarrowSynonym": "narrow",
    "hasBroadSynonym": "broad",
}


# --- CURIE / IRI helpers ---------------------------------------------------


def normalize_curie(raw: str) -> str:
    """Return a canonical CURIE: uppercase prefix, ``obo:`` stripped, ``HP`` -> ``HPO``.

    ``"obo:go_0008219"`` and ``"GO:0008219"`` both yield ``"GO:0008219"``. OLS
    sometimes returns short forms with ``_`` instead of ``:`` (e.g. ``GO_0008219``).
    """
    text = raw.strip()
    if text.lower().startswith("obo:"):
        text = text[4:]
    sep = ":" if ":" in text else ("_" if "_" in text else None)
    if sep is None:
        raise ValueError(f"Not a CURIE: {raw!r}")
    prefix, local_id = text.split(sep, 1)
    if not prefix or not local_id:
        # Reject malformed CURIEs like ":", "GO:", ":0008219" — both a prefix and
        # a local id are required.
        raise ValueError(f"Not a CURIE: {raw!r}")
    prefix = prefix.upper()
    prefix = _PREFIX_ALIASES.get(prefix, prefix)
    return f"{prefix}:{local_id}"


def curie_to_iri(curie: str) -> str:
    """Build the full OBO IRI for a CURIE using ``config.IRI_TEMPLATES``."""
    normalized = normalize_curie(curie)
    prefix, local_id = normalized.split(":", 1)
    template = config.IRI_TEMPLATES.get(prefix)
    if template is None:
        raise ValueError(f"Unknown ontology prefix: {prefix}")
    return template.format(id=local_id)


def double_encode_iri(iri: str) -> str:
    """Double-URL-encode an IRI for use as an OLS path parameter."""
    return quote(quote(iri, safe=""), safe="")


def _ontology_slug(curie: str) -> str:
    """OLS ontology id for the CURIE's prefix, from the committed index.

    Raises ``KeyError`` for an ontology OLS4 does not serve. That is deliberate:
    the previous ``.lower()`` fallback fabricated a slug, which OLS answered 200
    with zero documents — a silent wrong answer rather than a loud wrong input.
    """
    prefix = normalize_curie(curie).split(":", 1)[0]
    return registry.slug_for(prefix)


# --- Response parsers ------------------------------------------------------


def _first(value):
    """Return the first item of a list, the value itself if scalar, else None."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


# OLS's `description` field mixes the real definition with provenance/citation
# comments, e.g. "GOC:mtg_apoptosis in PMID:12345 inferred from mutant phenotype".
# Blindly taking index 0 (the old `_first` behavior) served the comment as often
# as the definition.
_PROVENANCE_RE = re.compile(r"^\S+\s+in\s+PMID:\d+", re.IGNORECASE)


def _pick_definition(value):
    """Return the best definition-shaped entry from a possibly multi-valued field.

    Filters out entries that look like a citation/provenance comment, then keeps
    the longest remaining candidate. Falls back to the longest raw candidate if
    every entry looks like a comment, so a non-empty field never becomes None.
    """
    if not isinstance(value, list):
        return value
    candidates = [v for v in value if isinstance(v, str) and v]
    if not candidates:
        return None
    filtered = [v for v in candidates if not _PROVENANCE_RE.match(v)]
    return max(filtered or candidates, key=len)


def _opt_bool(value) -> bool | None:
    """Coerce to bool, or None when OLS omitted the field (vs. an explicit False)."""
    return None if value is None else bool(value)


def _leaf_flag(obj: dict) -> bool | None:
    """Derive leaf status: prefer an explicit ``is_leaf``, else negate ``has_children``."""
    if obj.get("is_leaf") is not None:
        return obj.get("is_leaf")
    if obj.get("has_children") is not None:
        return not obj.get("has_children")
    return None


def _parse_synonyms(obj: dict) -> dict[str, list[str]]:
    """Group an OLS term's synonyms into the four OBO scope buckets."""
    buckets: dict[str, list[str]] = {"exact": [], "related": [], "narrow": [], "broad": []}
    for entry in obj.get("obo_synonym") or []:
        scope = _SYNONYM_SCOPES.get(entry.get("type"), "exact")
        name = entry.get("name")
        if name:
            buckets[scope].append(name)
    # Plain synonym strings (no scope) fall back to the exact bucket.
    for name in obj.get("synonyms") or []:
        if name and name not in buckets["exact"]:
            buckets["exact"].append(name)
    return buckets


def _iri_to_curie(value: str) -> str | None:
    """Best-effort convert an OBO IRI or short form to a CURIE, else None.

    Accepts ``http://purl.obolibrary.org/obo/GO_0006915``, ``GO_0006915``, or
    ``GO:0006915`` and returns ``GO:0006915``. Returns None when unparseable —
    callers drop unparseable entries rather than raise.
    """
    if not isinstance(value, str):
        return None
    candidate = value.rsplit("/", 1)[-1] if "/" in value else value
    try:
        return normalize_curie(candidate)
    except (ValueError, AttributeError):
        return None


def _parse_consider(obj: dict) -> list[str]:
    """Collect oboInOwl ``consider`` alternates for an obsolete term as CURIEs.

    OLS exposes this in a few shapes depending on the ontology: a top-level
    ``consider``/``obo_consider`` list, or under ``annotation`` keyed ``consider``.
    Values may be IRIs, short forms, or CURIEs. Deduped, order preserved.
    """
    raw: list = []
    for key in ("consider", "obo_consider"):
        value = obj.get(key)
        if value:
            raw.extend(value if isinstance(value, list) else [value])
    annotation = obj.get("annotation") or {}
    if isinstance(annotation, dict):
        value = annotation.get("consider")
        if value:
            raw.extend(value if isinstance(value, list) else [value])

    out: list[str] = []
    for item in raw:
        curie = _iri_to_curie(item)
        if curie and curie not in out:
            out.append(curie)
    return out


def _parse_subsets(obj: dict) -> list[str]:
    """Return the term's subset/slim memberships (e.g. ``gocheck_do_not_annotate``).

    OLS may use ``in_subset`` or ``obo_id_subset``; values are short subset names.
    """
    raw = obj.get("in_subset") or obj.get("obo_id_subset") or []
    if isinstance(raw, str):
        raw = [raw]
    return [s for s in raw if isinstance(s, str)]


def _parse_definition_sources(obj: dict) -> list[str]:
    """Return definition citation xrefs (e.g. ``GOC:mtg_apoptosis``, ``PMID:...``).

    OLS exposes these under ``obo_definition_citation[*].oboXrefs[*].id`` (and the
    flat ``oboXref`` form on older payloads). Best-effort; deduped, order kept.
    """
    out: list[str] = []
    for citation in obj.get("obo_definition_citation") or []:
        if not isinstance(citation, dict):
            continue
        for xref in citation.get("oboXrefs") or citation.get("oboXref") or []:
            if isinstance(xref, dict):
                # OLS nests these as {database, id}; compose a prefixed citation
                # (e.g. "PMID:25236395") unless the id is already prefixed.
                ident = xref.get("id")
                database = xref.get("database")
                if isinstance(ident, str) and ident and ":" not in ident and database:
                    ident = f"{database}:{ident}"
            else:
                ident = xref
            if isinstance(ident, str) and ident and ident not in out:
                out.append(ident)
    return out


def _parse_term(obj: dict) -> dict:
    """Parse one OLS term object into the dict shape ``cache.put_term`` expects."""
    raw_curie = obj.get("obo_id") or obj.get("short_form")
    if not raw_curie:
        raise ValueError("OLS term object has no obo_id or short_form")
    curie = normalize_curie(raw_curie)
    ontology = curie.split(":", 1)[0]
    replaced_by = _first(obj.get("term_replaced_by"))
    return {
        "curie": curie,
        "ontology": ontology,
        "label": obj.get("label"),
        "definition": _pick_definition(obj.get("description")),
        "is_obsolete": int(bool(obj.get("is_obsolete", False))),
        "replaced_by": _iri_to_curie(replaced_by) if replaced_by else None,
        "consider": _parse_consider(obj),
        "subsets": _parse_subsets(obj),
        "definition_sources": _parse_definition_sources(obj),
        "has_children": _opt_bool(obj.get("has_children")),
        "is_leaf": _opt_bool(_leaf_flag(obj)),
        "iri": obj.get("iri"),
        "synonyms": _parse_synonyms(obj),
        "raw_json": obj,
    }


def _parse_hierarchy_node(obj: dict, rel_type: str) -> dict:
    """Parse one hierarchy node, tagging it with the caller-supplied ``rel_type``.

    The OLS hierarchy endpoints do not return the edge predicate in the node body,
    so the predicate is set by the calling method, which knows what it asked for:
    ``/parents`` and ``/children`` are direct ``subClassOf`` edges (``is_a``);
    ``/ancestors`` and ``/hierarchicalDescendants`` return the transitive closure
    (mixing is_a and part_of), so they are tagged ``ancestor`` / ``descendant``
    rather than falsely asserting ``is_a``.
    """
    raw_curie = obj.get("obo_id") or obj.get("short_form")
    if not raw_curie:
        raise ValueError("OLS hierarchy node has no obo_id or short_form")
    return {
        "curie": normalize_curie(raw_curie),
        "label": obj.get("label"),
        "rel_type": rel_type,
    }


# --- Client ----------------------------------------------------------------


class OLSClient:
    """Async OLS4 client. Use as an async context manager or call ``aclose()``."""

    def __init__(
        self,
        base_url: str = config.OLS_BASE_URL,
        timeout: float = config.OLS_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": config.USER_AGENT},
        )

    async def __aenter__(self) -> "OLSClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET with retry/backoff on 429 + 5xx and transport/timeout errors.

        Returns the final ``httpx.Response`` (caller inspects status). Raises
        ``httpx.HTTPError`` only if every retry exhausts on a network error.
        """
        last_exc: Exception | None = None
        for attempt in range(1, config.OLS_MAX_RETRIES + 1):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            else:
                if response.status_code not in config.OLS_RETRY_STATUS:
                    return response
                last_exc = None
            if attempt < config.OLS_MAX_RETRIES:
                await asyncio.sleep(config.OLS_BACKOFF_BASE * 2 ** (attempt - 1))
        if last_exc is not None:
            raise last_exc
        return response

    async def search(
        self,
        query: str,
        ontologies: list[str] | None = None,
        limit: int = 10,
        *,
        rows: int | None = None,
    ) -> list[dict]:
        """Free-text search. Returns ``[{curie, label, ontology, definition, iri, score}]``."""
        params = {
            "q": query,
            # ``rows`` lets the caller over-fetch when a post-filter will drop
            # rows, so the filter cannot silently shrink the result set below the
            # requested limit. Defaults to limit when no filter is in play.
            "rows": rows or limit,
            # `synonym` is the full synonym bag across all scopes. It is requested
            # for the CACHE, not for the caller: without it a search-warmed row is
            # synonym-blind in FTS, so a synonym query could never be served from
            # cache until someone happened to call get_term on that CURIE.
            # `obo_synonym` and `synonyms` are NOT valid search fields — OLS drops
            # them silently — and `related_synonyms` is a strict subset.
            #
            # Deliberately NOT requested: `ontology_prefix`. It reports which
            # ontology INDEXED a row, which is not what any caller wants — the
            # minting-prefix filter needs the ontology that MINTED it, and a
            # CURIE's prefix is that by construction. Requesting it produced a
            # `source_ontology` field nothing ever read.
            "fieldList": "iri,label,description,short_form,obo_id,is_obsolete,synonym",
        }
        if ontologies:
            slugs = []
            for code in ontologies:
                try:
                    slugs.append(registry.slug_for(code))
                except (KeyError, registry.IndexError_):
                    # normalize_ontologies has already validated membership, so an
                    # unknown code here means the index moved under a frozen
                    # profile, or the index is unavailable. Drop it rather than
                    # sending a fabricated slug.
                    continue
            if slugs:
                params["ontology"] = ",".join(slugs)
        try:
            response = await self._get("/search", params=params)
            response.raise_for_status()
            docs = response.json().get("response", {}).get("docs", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OLS search failed for %r: %s", query, exc)
            return [{"error": "search_failed", "detail": str(exc), "query": query}]

        results = []
        for doc in docs:
            raw_curie = doc.get("obo_id") or doc.get("short_form")
            if not raw_curie:
                continue
            curie = normalize_curie(raw_curie)
            results.append(
                {
                    "curie": curie,
                    "label": doc.get("label"),
                    "ontology": curie.split(":", 1)[0],
                    "definition": _pick_definition(doc.get("description")),
                    "is_obsolete": bool(doc.get("is_obsolete", False)),
                    # Cache-only: search.py strips this before the response leaves,
                    # because 20-odd synonyms per row is exactly the payload bloat
                    # the project's cap rule exists to prevent.
                    "synonyms": doc.get("synonym") or [],
                    # The fieldList already asked for this; the parse loop used to
                    # discard it, so every search threw away the one authoritative
                    # IRI we get for free.
                    "iri": doc.get("iri"),
                }
            )
        # OLS returns docs in relevance order. Rank is the ordinal position; there
        # is no score, because a value that is a function of result-set size alone
        # carries zero query information (see the note on rank in tools/search.py).
        for i, item in enumerate(results, start=1):
            item["rank"] = i
        return results

    async def search_facets(self, query: str) -> dict[str, int]:
        """Per-ontology hit counts for a query across all of OLS4, as exact counts.

        OLS4 returns ``facet_counts.facet_fields`` on every ``/search`` **by
        default**, with no facet parameters requested — so this costs one call and
        the counts are exact over the whole result set, not an estimate from a
        sampled page. Returns ``{PREFIX: count}``; empty on any failure, because
        the escape hatch is a courtesy and must never turn a search into an error.
        """
        try:
            response = await self._get("/search", params={"q": query, "rows": 0})
            response.raise_for_status()
            fields = response.json().get("facet_counts", {}).get("facet_fields", {})
        except (httpx.HTTPError, ValueError):
            return {}

        flat = fields.get("ontologyPreferredPrefix") or []
        # Solr renders facets as a flat [value, count, value, count, ...] list.
        counts: dict[str, int] = {}
        for i in range(0, len(flat) - 1, 2):
            prefix, count = flat[i], flat[i + 1]
            if not prefix:
                continue
            try:
                counts[registry.canonical_key(str(prefix))] = int(count)
            except (TypeError, ValueError):
                continue
        return counts

    async def fetch_term(self, curie: str) -> dict:
        """Fetch a single term. Returns the parsed term or an error/not-found dict."""
        normalized = normalize_curie(curie)
        try:
            slug = _ontology_slug(normalized)
            minted = registry.minting_curie(normalized)
        except KeyError as exc:
            return {"error": "unknown_ontology", "detail": str(exc), "curie": normalized}
        except registry.IndexError_ as exc:
            return {"error": "index_unavailable", "detail": str(exc), "curie": normalized}
        # OLS matches obo_id against its OWN minting prefix. Sending our canonical
        # key returns an empty list, so HPO and NCBITaxon terms were unfetchable.
        try:
            response = await self._get(f"/ontologies/{slug}/terms", params={"obo_id": minted})
            if response.status_code == 404:
                return {"error": "not_found", "curie": normalized}
            response.raise_for_status()
            terms = response.json().get("_embedded", {}).get("terms", [])
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": "fetch_failed", "detail": str(exc), "curie": normalized}
        if not terms:
            return {"error": "not_found", "curie": normalized}
        return _parse_term(terms[0])

    async def fetch_ontology_version(self, ontology: str) -> str | None:
        """Return the source release/version string for an ontology, or None.

        ``ontology`` is a registry key (e.g. ``"GO"``). OLS exposes the release in
        ``version`` (root) or ``config.version``. Never raises — returns None on any
        failure so version capture is strictly best-effort.
        """
        try:
            slug = registry.slug_for(ontology)
        except (KeyError, registry.IndexError_):
            return None
        try:
            response = await self._get(f"/ontologies/{slug}")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        version = body.get("version")
        if not version:
            config_block = body.get("config") or {}
            version = config_block.get("version") if isinstance(config_block, dict) else None
        return version

    async def _fetch_hierarchy(
        self, curie: str, endpoint: str, rel_type: str, iri: str | None = None
    ) -> list[dict]:
        """Fetch hierarchy nodes, optionally against a pre-resolved IRI.

        ``iri`` lets ``core.iri.fetch_with_disconfirmation`` supply an address for
        any of the ~286 ontologies, keeping this module SQLite-free — the
        resolver needs the cache and the registry, so it lives one layer up and
        passes the answer down.

        A 404 returns the ``iri_miss`` marker rather than ``[]``. ``[]`` means "this
        term genuinely has no parents", and conflating the two is how an
        unreachable IRI became a confident "no results" (tournament/spike-results.md
        S-4). Callers escalate on the marker; nothing else reads it.
        """
        normalized = normalize_curie(curie)
        try:
            slug = _ontology_slug(normalized)
        except KeyError as exc:
            return [{"error": "unknown_ontology", "detail": str(exc), "curie": normalized}]
        except registry.IndexError_ as exc:
            return [{"error": "index_unavailable", "detail": str(exc), "curie": normalized}]
        if iri is None:
            try:
                iri = curie_to_iri(normalized)
            except ValueError as exc:
                return [{"error": "bad_curie", "detail": str(exc), "curie": normalized}]
        encoded_iri = double_encode_iri(iri)
        try:
            response = await self._get(f"/ontologies/{slug}/terms/{encoded_iri}/{endpoint}")
            if response.status_code == 404:
                return [{"error": "iri_miss", "curie": normalized, "iri": iri}]
            response.raise_for_status()
            terms = response.json().get("_embedded", {}).get("terms", [])
        except (httpx.HTTPError, ValueError) as exc:
            return [{"error": "fetch_failed", "detail": str(exc), "curie": normalized}]
        if not terms:
            # An empty node list is the other shape a wrong IRI takes: OLS answers
            # 200 for a well-formed IRI it does not know. Indistinguishable from a
            # true leaf here, so it is marked and the caller decides.
            return [{"error": "iri_miss", "curie": normalized, "iri": iri, "empty": True}]
        return [_parse_hierarchy_node(t, rel_type) for t in terms]

    async def fetch_parents(self, curie: str, iri: str | None = None) -> list[dict]:
        """Fetch DIRECT parent nodes (one hop). Returns ``[{curie, label, rel_type}, ...]``.

        OLS ``/parents`` returns only direct ``subClassOf`` parents, so these are
        true ``is_a`` edges suitable for the relationships table and graph building.
        """
        return await self._fetch_hierarchy(curie, "parents", "is_a", iri)

    async def fetch_children(self, curie: str, iri: str | None = None) -> list[dict]:
        """Fetch DIRECT child nodes (one hop). Returns ``[{curie, label, rel_type}, ...]``.

        OLS ``/children`` returns only direct ``subClassOf`` children — true
        ``is_a`` edges.
        """
        return await self._fetch_hierarchy(curie, "children", "is_a", iri)

    async def fetch_ancestors(self, curie: str, iri: str | None = None) -> list[dict]:
        """Fetch the TRANSITIVE set of ancestor nodes (full closure, flattened).

        OLS ``/ancestors`` returns every ancestor regardless of distance and mixes
        edge predicates, so nodes are tagged ``rel_type="ancestor"`` — not ``is_a``,
        which would falsely assert a direct subsumption edge.
        """
        return await self._fetch_hierarchy(curie, "ancestors", "ancestor", iri)

    async def fetch_descendants(self, curie: str, iri: str | None = None) -> list[dict]:
        """Fetch the TRANSITIVE set of descendant nodes (full closure, flattened).

        OLS ``/hierarchicalDescendants`` returns every descendant regardless of
        distance and mixes edge predicates, so nodes are tagged
        ``rel_type="descendant"`` rather than the false ``is_a``.
        """
        return await self._fetch_hierarchy(curie, "hierarchicalDescendants", "descendant", iri)
