"""Health check route — no tool call, always 200."""

from fastapi import APIRouter, Depends

from ontomcp import __version__
from ontomcp.api.responses import envelope, get_db_path
from ontomcp.core import cache
from ontomcp.core.config import ONTOLOGIES

router = APIRouter()


@router.get("/health")
async def health(db_path=Depends(get_db_path)) -> dict:
    # ontology_versions are whatever has been captured during term lookups so far
    # (cache-only — /health never calls OLS). Empty until the first term is fetched.
    return envelope(
        {
            "status": "ok",
            "version": __version__,
            "ontologies": list(ONTOLOGIES),
            "ontology_versions": cache.get_all_ontology_versions(db_path),
        }
    )
