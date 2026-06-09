"""search route: POST /search → core search_terms."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ontomcp.api.responses import get_db_path, get_ols_client, respond
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import search_terms

router = APIRouter()


class SearchRequest(BaseModel):
    query: str = Field(..., max_length=500)
    ontologies: list[str] | None = None
    limit: int = 10


@router.post("/search")
async def search(
    body: SearchRequest,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await search_terms(
        body.query, body.ontologies, body.limit, db_path=db_path, client=client
    )
    return respond(result, cached)
