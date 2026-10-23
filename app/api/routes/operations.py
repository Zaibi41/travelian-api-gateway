from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["operations"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", responses={503: {"description": "Application is not ready"}})
async def ready(request: Request) -> JSONResponse:
    is_ready = getattr(request.app.state, "ready", False)
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready"},
    )
