"""Pricing & Revenue > Flight Pricing & Rules: the flight suppliers and the Rs-per-passenger rules on their fares
(migration 0041, app.services.flight_pricing_rules). Super Admin only: it names suppliers and sets margins."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser, require_super_admin
from app.db import flight_pricing_rules as store
from app.schemas.flight_pricing import (DeletedFlightPricingRule, FlightPricingRule, FlightPricingRuleInput, FlightPricingRuleList, FlightSupplier,
                                        FlightSupplierList, SupplierSource)
from app.services import flight_pricing_rules as rules_service, public_booking
from app.vendors.flights.kyte import client as kyte
from app.vendors.flights.sooperfare import client as sooperfare
from app.vendors.flights.southair import client as southair

router = APIRouter(prefix="/admin/flight-pricing", tags=["admin flight pricing"],
                   responses={401: {}, 403: {"description": "Super Admin only."}, 503: {"description": "Pricing rules unavailable."}})
SuperAdmin = Annotated[BackOfficeUser, Depends(require_super_admin)]
RuleId = Annotated[str, Path(max_length=64)]


def _view(rule: store.Rule) -> FlightPricingRule:
    return FlightPricingRule(id=rule.id, supplier=rule.supplier, source=rule.source, airline_code=rule.airline_code,  # type: ignore[arg-type]
                             amount_minor=rule.amount_minor, active=rule.active, updated_at=rule.updated_at)


def _southair_configured() -> bool:
    try:
        southair.configured_client()  # only reads the settings: no call to Southair
    except southair.SouthairNotConfigured:
        return False
    return True


def _rules_changed() -> None:
    """The next search, website search and fare calendar use the new rules at once."""
    rules_service.clear_cache()
    public_booking.forget_flight_prices()


def _refused(error: store.RuleRefused) -> HTTPException:
    return HTTPException(error.status, error.message, headers={"X-Error-Code": error.code})


@router.get("/suppliers", response_model=FlightSupplierList)
async def list_suppliers(_: SuperAdmin):
    """The flight suppliers a rule can be set on, whether each is searched for customers, and SooperFare's sources."""
    kyte_status = ("live" if settings.kyte_public_enabled else "not_public") if kyte.configured() else "not_configured"
    return FlightSupplierList(suppliers=[
        FlightSupplier(code="southair", name="Southair", status="live" if _southair_configured() else "not_configured"),
        FlightSupplier(code="sooperfare", name="SooperFare", status="live" if sooperfare.configured() else "not_configured",
                       sources=[SupplierSource(code=code, name=name) for code, name in store.SOOPERFARE_SOURCES.items()]),
        FlightSupplier(code="kyte", name="Kyte", status=kyte_status),
    ])


@router.get("/rules", response_model=FlightPricingRuleList)
async def list_rules(_: SuperAdmin):
    try:
        rules = await run_in_threadpool(store.list_rules)
    except SQLAlchemyError:
        raise HTTPException(503, "Pricing rules are not available.") from None
    return FlightPricingRuleList(rules=[_view(rule) for rule in rules])


@router.post("/rules", response_model=FlightPricingRule, status_code=201, responses={409: {}, 422: {}})
async def create_rule(body: FlightPricingRuleInput, viewer: SuperAdmin):
    """One rule per supplier, source and airline (409 if it exists). Applies to new searches within seconds."""
    try:
        rule = await run_in_threadpool(store.create_rule, body.supplier, body.source, body.airline_code, body.amount_minor,
                                       body.active, viewer.user_id)
    except store.RuleRefused as error:
        raise _refused(error) from None
    except SQLAlchemyError:
        raise HTTPException(503, "Pricing rules are not available.") from None
    _rules_changed()
    return _view(rule)


@router.put("/rules/{rule_id}", response_model=FlightPricingRule, responses={404: {}, 409: {}, 422: {}})
async def update_rule(rule_id: RuleId, body: FlightPricingRuleInput, viewer: SuperAdmin):
    try:
        rule = await run_in_threadpool(store.update_rule, rule_id, body.supplier, body.source, body.airline_code,
                                       body.amount_minor, body.active, viewer.user_id)
    except store.RuleRefused as error:
        raise _refused(error) from None
    except SQLAlchemyError:
        raise HTTPException(503, "Pricing rules are not available.") from None
    _rules_changed()
    return _view(rule)


@router.delete("/rules/{rule_id}", response_model=DeletedFlightPricingRule, responses={404: {}})
async def delete_rule(rule_id: RuleId, _: SuperAdmin):
    try:
        await run_in_threadpool(store.delete_rule, rule_id)
    except store.RuleRefused as error:
        raise _refused(error) from None
    except SQLAlchemyError:
        raise HTTPException(503, "Pricing rules are not available.") from None
    _rules_changed()
    return DeletedFlightPricingRule(id=rule_id)
