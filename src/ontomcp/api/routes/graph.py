"""graph route: GET /graph/{curie} → core get_term_graph."""

from fastapi import APIRouter, Depends

from ontomcp.api.responses import get_db_path, get_ols_client, respond
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import get_term_graph

router = APIRouter()


@router.get("/graph/{curie}")
async def graph(
    curie: str,
    include_siblings: bool = True,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await get_term_graph(
        curie,
        include_siblings,
        db_path=db_path,
        client=client,
    )
    return respond(result, cached)
