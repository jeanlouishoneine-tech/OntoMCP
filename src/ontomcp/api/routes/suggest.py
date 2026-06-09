"""suggest route: POST /suggest → core suggest_ontology (sync, no OLS/cache)."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ontomcp.api.responses import respond
from ontomcp.core.tools import suggest_ontology

router = APIRouter()


class SuggestRequest(BaseModel):
    context: str = Field(..., max_length=2000)


@router.post("/suggest")
async def suggest(body: SuggestRequest):
    result, cached = suggest_ontology(body.context)
    return respond(result, cached)
