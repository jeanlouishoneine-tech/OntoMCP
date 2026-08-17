"""Health check route — no tool call, always 200."""

import logging

from fastapi import APIRouter, Depends

from ontomcp import __version__
from ontomcp.api.responses import envelope, get_db_path
from ontomcp.core import cache, registry

logger = logging.getLogger("ontomcp")

router = APIRouter()


def _scope() -> tuple[tuple[str, ...], bool]:
    """The resolved profile's members — never the ~286-entry index — plus whether
    resolution actually succeeded.

    /health reports what this session will actually search. Binding it to the
    index would report 286 ontologies for a session that searches six.

    An empty scope is normal for other reasons (e.g. a lock-empty env override),
    but an empty scope *accompanied by a warning* means resolution degraded
    rather than legitimately resolving to nothing — that combination is what
    ``status`` below must not silently report as "ok". This is the one surface
    whose entire job is to reveal that class of failure.
    """
    try:
        resolved = registry.current()
    except Exception:
        logger.warning("health check: registry unreadable, reporting empty scope", exc_info=True)
        return (), False
    ok = bool(resolved.all_members) or not resolved.warnings
    return resolved.all_members, ok


@router.get("/health")
async def health(db_path=Depends(get_db_path)) -> dict:
    # ontology_versions are whatever has been captured during term lookups so far
    # (cache-only — /health never calls OLS). Empty until the first term is fetched.
    scope, ok = _scope()
    payload = {
        "status": "ok" if ok else "degraded",
        "version": __version__,
        "ontologies": list(scope),
        "ontology_versions": cache.get_all_ontology_versions(db_path),
    }
    if not ok:
        payload["detail"] = "ontology registry unreadable or unresolvable; run scripts/refresh_index.py"
    return envelope(payload)
