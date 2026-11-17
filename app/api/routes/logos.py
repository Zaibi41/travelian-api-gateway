"""Logos served by the gateway so no supplier host reaches a client (D23).

No sign-in: browsers load these in <img> tags. Only IDs the gateway issued are
served; see app.services.supplier_logos for the fetch rules.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import Response

from app.services import supplier_logos

router = APIRouter(prefix="/logos", tags=["logos"])


@router.get("/{logo_id}", response_class=Response, responses={200: {"content": {"image/png": {}}}, 404: {"description": "Unknown logo."}})
async def logo(logo_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]):
    try:
        content_type, body = await supplier_logos.fetch(logo_id)
    except supplier_logos.LogoUnavailable:
        raise HTTPException(404, "Logo not available.") from None
    return Response(body, media_type=content_type, headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    })
