"""term routes keyed by CURIE path param.

Ancestors/descendants live here too (folded from hierarchy.py) since they are
CURIE-keyed and belong to the /term/{curie} resource.
"""

from fastapi import APIRouter, Depends

from ontomcp.api.responses import get_db_path, get_ols_client, respond
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import (
    find_synonyms,
    get_ancestors,
    get_children,
    get_descendants,
    get_parents,
    get_term,
    validate_term,
)

router = APIRouter()


@router.get("/term/{curie}")
async def term(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_term(curie, db_path=db_path, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/synonyms")
async def synonyms(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await find_synonyms(curie, db_path=db_path, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/validate")
async def validate(curie: str, client: OLSClient = Depends(get_ols_client)):
    # validate_term never uses the cache — no db_path; cached is always False.
    result, cached = await validate_term(curie, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/parents")
async def parents(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_parents(curie, db_path=db_path, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/children")
async def children(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_children(curie, db_path=db_path, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/ancestors")
async def ancestors(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_ancestors(curie, db_path=db_path, client=client)
    return respond(result, cached)


@router.get("/term/{curie}/descendants")
async def descendants(
    curie: str,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_descendants(curie, db_path=db_path, client=client)
    return respond(result, cached)
