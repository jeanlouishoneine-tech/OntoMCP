"""Synchronous HTTP client wrapping the OntoMCP FastAPI endpoints.

Used from ipywidgets button callbacks and the ``%%ontomcp`` cell magic, both of
which run in synchronous contexts — hence ``httpx.Client`` rather than the async
client used elsewhere. This is pure glue: every method maps one-to-one to a
FastAPI route and returns the route's ``data`` payload. No business logic lives
here (project CLAUDE.md: logic belongs in ``core/``).

The API wraps every response in ``{"data": ..., "cached": ..., "ontomcp_version": ...}``
and embeds tool errors as ``{"error": ...}`` dicts inside ``data`` — even on 4xx/5xx.
So each method returns ``data`` directly; callers inspect it for an ``error`` key.
"""

import os
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = os.environ.get("ONTOMCP_API_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = 15.0


class OntoMCPConnectionError(RuntimeError):
    """Raised when the OntoMCP API cannot be reached at all."""


class OntoMCPClient:
    """Thin synchronous client for the OntoMCP HTTP API.

    Args:
        base_url: API root. Defaults to ``$ONTOMCP_API_URL`` or localhost:8000.
        timeout: Per-request timeout in seconds.
        transport: Optional httpx transport — used by tests to drive the ASGI
            app in-process without a live server.
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT, transport=None):
        self.base_url = base_url
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    # --- transport ---------------------------------------------------------

    def _request(self, method, path, *, params=None, json=None):
        """Send a request and return the envelope's ``data`` field.

        Connection failures raise ``OntoMCPConnectionError``; a malformed (non-JSON
        or envelope-less) body raises ``RuntimeError``. Tool-level errors are *not*
        raised — they ride inside ``data`` so the caller can render them.
        """
        try:
            resp = self._http.request(method, path, params=params, json=json)
        except httpx.ConnectError as exc:
            raise OntoMCPConnectionError(
                f"Could not reach OntoMCP API at {self.base_url}. Start it with `ontomcp-api`."
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"OntoMCP API returned a non-JSON response ({resp.status_code})."
            ) from exc

        if not isinstance(body, dict) or "data" not in body:
            raise RuntimeError("OntoMCP API response missing the 'data' envelope field.")
        return body["data"]

    @staticmethod
    def _curie_path(curie):
        """Encode a CURIE for use as a path segment (``GO:0008219`` -> ``GO%3A0008219``)."""
        return quote(curie, safe="")

    # --- endpoints ---------------------------------------------------------

    def health(self):
        return self._request("GET", "/health")

    def search(self, query, ontologies=None, limit=10):
        payload = {"query": query, "limit": limit}
        if ontologies is not None:
            payload["ontologies"] = ontologies
        return self._request("POST", "/search", json=payload)

    def get_term(self, curie):
        return self._request("GET", f"/term/{self._curie_path(curie)}")

    def synonyms(self, curie):
        return self._request("GET", f"/term/{self._curie_path(curie)}/synonyms")

    def validate(self, curie):
        return self._request("GET", f"/term/{self._curie_path(curie)}/validate")

    def ancestors(self, curie):
        return self._request("GET", f"/term/{self._curie_path(curie)}/ancestors")

    def descendants(self, curie):
        return self._request("GET", f"/term/{self._curie_path(curie)}/descendants")

    def graph(self, curie, include_siblings=True):
        return self._request(
            "GET",
            f"/graph/{self._curie_path(curie)}",
            params={"include_siblings": include_siblings},
        )

    def bulk(self, terms, ontology_hint=None, threshold=0.8):
        payload = {"terms": terms, "threshold": threshold}
        if ontology_hint is not None:
            payload["ontology_hint"] = ontology_hint
        return self._request("POST", "/bulk", json=payload)

    def suggest(self, context):
        return self._request("POST", "/suggest", json={"context": context})

    # --- lifecycle ---------------------------------------------------------

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
