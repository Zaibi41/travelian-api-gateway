from fastapi import APIRouter

from app.api.routes.admin_bookings import router as admin_bookings_router
from app.api.routes.admin_dashboard import router as admin_dashboard_router
from app.api.routes.admin_notifications import router as admin_notifications_router
from app.api.routes.admin_uploads import router as admin_uploads_router
from app.api.routes.admin_users import router as admin_users_router
from app.api.routes.agencies import router as agencies_router
from app.api.routes.agency_applications import router as agency_applications_router
from app.api.routes.agency_integrations import router as agency_integrations_router
from app.api.routes.auth_sessions import router as auth_sessions_router
from app.api.routes.finance import router as finance_router
from app.api.routes.flight_operations import router as flight_operations_router
from app.api.routes.group_holds import router as group_holds_router
from app.api.routes.group_ticketing import router as group_ticketing_router
from app.api.routes.hotels import router as hotels_router
from app.api.routes.identity import router as identity_router
from app.api.routes.integration_group_ticketing import (
    router as integration_group_ticketing_router,
)
from app.api.routes.live_ticketing import router as live_ticketing_router
from app.api.routes.ratehawk_supplier import router as ratehawk_supplier_router
from app.api.routes.logos import router as logos_router
from app.api.routes.operations import router as operations_router
from app.api.routes.payment_accounts import finance_router as finance_payment_accounts_router
from app.api.routes.payment_accounts import router as payment_accounts_router
from app.api.routes.public import router as public_router
from app.api.routes.saved_people import booking_router as saved_people_booking_router
from app.api.routes.saved_people import router as saved_people_router, travelers_router as saved_travelers_router

router = APIRouter()
router.include_router(operations_router)
router.include_router(identity_router)
router.include_router(live_ticketing_router)
router.include_router(group_holds_router)
router.include_router(group_ticketing_router)
router.include_router(hotels_router)
router.include_router(ratehawk_supplier_router)

router.include_router(agency_applications_router)

router.include_router(agencies_router)

router.include_router(auth_sessions_router)

router.include_router(agency_integrations_router)

router.include_router(integration_group_ticketing_router)

router.include_router(admin_users_router)

router.include_router(admin_dashboard_router)

router.include_router(admin_bookings_router)
router.include_router(flight_operations_router)
router.include_router(saved_people_router)
router.include_router(saved_travelers_router)
router.include_router(payment_accounts_router)
router.include_router(finance_payment_accounts_router)
router.include_router(finance_router)
router.include_router(saved_people_booking_router)
router.include_router(logos_router)
router.include_router(public_router)
router.include_router(admin_notifications_router)
router.include_router(admin_uploads_router)
