"""bulk route: POST /bulk → core bulk_annotate."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ontomcp.api.responses import get_db_path, get_ols_client, respond
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools import bulk_annotate

router = APIRouter()


class BulkRequest(BaseModel):
    terms: list[str]
    ontology_hint: str | None = None
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)


@router.post("/bulk")
async def bulk(
    body: BulkRequest,
    client: OLSClient = Depends(get_ols_client),
    db_path=Depends(get_db_path),
):
    result, cached = await bulk_annotate(
        body.terms,
        body.ontology_hint,
        body.threshold,
        db_path=db_path,
        client=client,
    )
    return respond(result, cached)
