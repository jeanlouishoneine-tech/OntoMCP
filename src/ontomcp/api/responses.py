"""Response envelope and error-to-HTTP-status mapping shared by all routes.

Routes stay thin: they call a core tool and pass the result through ``respond``.
No business logic here — only the HTTP framing the plan requires.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from ontomcp import __version__
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error

# Map a tool/OLS error code to an HTTP status. Anything unlisted is treated as a
# bad gateway, since the remaining errors are OLS/fetch failures.
_ERROR_STATUS = {
    "bad_curie": 400,
    "too_many_terms": 400,
    "unknown_ontology": 400,
    "not_found": 404,
}
_DEFAULT_ERROR_STATUS = 502


def envelope(data, cached: bool | None = None) -> dict:
    """Wrap a payload in the standard response envelope.

    ``cached`` reports whether the result was served from the SQLite cache, as
    returned by the core tool. None means not applicable (e.g. /health).
    """
    return {"data": data, "cached": cached, "ontomcp_version": __version__}


def _error_code(result) -> str | None:
    """Extract the error code from an error dict or list-of-one error dict."""
    if isinstance(result, dict):
        return result.get("error")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return result[0].get("error")
    return None


def respond(result, cached: bool | None = None):
    """Return a JSONResponse: error dicts map to 4xx/5xx, everything else is 200.

    ``cached`` is the cache-hit flag from the core tool, passed straight into the
    envelope. The error payload still rides inside the envelope's ``data`` field
    so clients get a consistent shape regardless of status.
    """
    if is_error(result):
        status = _ERROR_STATUS.get(_error_code(result) or "", _DEFAULT_ERROR_STATUS)
        return JSONResponse(status_code=status, content=envelope(result, cached))
    return JSONResponse(status_code=200, content=envelope(result, cached))


def get_ols_client(request: Request) -> OLSClient:
    """FastAPI dependency yielding the app-lifetime shared OLS client."""
    return request.app.state.ols_client


def get_db_path(request: Request):
    """FastAPI dependency yielding the resolved cache DB path (honors --db-path)."""
    return request.app.state.db_path
