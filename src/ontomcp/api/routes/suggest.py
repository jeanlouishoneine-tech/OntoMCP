"""profile-setup guidance route: GET /profile-setup-guidance (sync, no OLS/cache).

A GET with no body, because the tool takes no arguments. The old POST /suggest
accepted a research description and keyword-matched it; that behaviour is deleted,
not relocated (see core/tools/suggest.py).
"""

from fastapi import APIRouter

from ontomcp.api.responses import respond
from ontomcp.core.tools import get_profile_setup_guidance

router = APIRouter()


@router.get("/profile-setup-guidance")
async def profile_setup_guidance():
    result, cached = get_profile_setup_guidance()
    return respond(result, cached)
