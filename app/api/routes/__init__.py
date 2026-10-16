from fastapi import APIRouter

from app.api.routes.group_ticketing import router as group_ticketing_router
from app.api.routes.hotels import router as hotels_router
from app.api.routes.live_ticketing import router as live_ticketing_router
from app.api.routes.operations import router as operations_router

router = APIRouter()
router.include_router(operations_router)
router.include_router(live_ticketing_router)
router.include_router(group_ticketing_router)
router.include_router(hotels_router)
