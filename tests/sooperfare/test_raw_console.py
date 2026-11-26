"""Raw SooperFare console: Super Admin only, sends calls unchanged, returns SooperFare's answer unchanged."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import sooperfare_supplier
from app.api.routes.sooperfare_supplier import router
from app.core.authentication import require_authenticated_user
from app.core.config import settings
from app.vendors.flights.sooperfare import client as sf_client
from app.vendors.flights.sooperfare.client import SooperfareClient
from app.vendors.flights.sooperfare.operations import OPERATIONS

BASE = "/v1/admin/suppliers/sooperfare/raw"


def app_for(monkeypatch, role):
    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u-" + role, role=role, role_id=9))
    monkeypatch.setattr("app.core.field_visibility.granted_permissions", lambda *_: set())
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    return TestClient(app)


class Supplier:
    """A fake SooperFare: records each call and answers from `answers` by path."""

    def __init__(self, answers=None):
        self.calls: list[httpx.Request] = []
        self.answers = answers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/partner/auth/login":
            return httpx.Response(200, json={"status": True, "data": {"access_token": {"token": "tok", "expires_at": "2099-01-01T00:00:00Z"}}})
        self.calls.append(request)
        answer = self.answers.get(request.url.path)
        if isinstance(answer, Exception):
            raise answer
        return answer or httpx.Response(200, json={"status": True, "data": {"echo": request.url.path}})


@pytest.fixture
def supplier(monkeypatch):
    monkeypatch.setattr(settings, "sooperfare_base_url", "https://sf.test")
    monkeypatch.setattr(settings, "sooperfare_username", "u")
    monkeypatch.setattr(settings, "sooperfare_password", SecretStr("p"))
    monkeypatch.setattr(sf_client, "_session", None)
    fake = Supplier()
    monkeypatch.setattr(sooperfare_supplier, "configured_client",
                        lambda: SooperfareClient(http=httpx.AsyncClient(transport=httpx.MockTransport(fake))))
    return fake


@pytest.mark.parametrize("role", ["staff", "agency_owner", "customer"])
def test_super_admin_only(monkeypatch, supplier, role):
    http = app_for(monkeypatch, role)
    assert http.get(f"{BASE}/operations").status_code == 403
    assert http.post(f"{BASE}/v1/airline/search", json={}).status_code == 403
    assert supplier.calls == []


def test_lists_every_call_from_sooperfares_collection(monkeypatch, supplier):
    listed = app_for(monkeypatch, "super_admin").get(f"{BASE}/operations").json()
    assert len(listed) == len(OPERATIONS) == 14
    search = next(o for o in listed if o["key"] == "search")
    assert search["gateway_path"] == f"{BASE}/v1/airline/search" and search["writes"] is False
    assert {o["key"] for o in listed if o["writes"]} == {"reservation", "booking_extras_submit", "booking_confirm",
                                                         "booking_cancel", "booking_void"}


def test_sends_body_query_and_headers_unchanged_and_returns_sooperfares_answer(monkeypatch, supplier):
    http = app_for(monkeypatch, "super_admin")
    body = {"trip_type": "one_way", "legs": [{"from": "KHI", "to": "LHE", "date": "2026-12-10"}], "provider": "PA"}
    response = http.post(f"{BASE}/v1/airline/search", json=body, headers={"X-SF-Client-Currency": "PKR"})
    assert response.status_code == 200 and response.json() == {"status": True, "data": {"echo": "/v1/airline/search"}}
    sent = supplier.calls[-1]
    assert (sent.method, json.loads(sent.content), sent.headers["X-SF-Authorization"], sent.headers["X-SF-Client-Currency"]) \
        == ("POST", body, "tok", "PKR")

    http.get(f"{BASE}/v1/airline/airports?query=fra")
    assert (supplier.calls[-1].method, supplier.calls[-1].url.params["query"], supplier.calls[-1].content) == ("GET", "fra", b"")

    # Availability is a GET with a JSON body, as SooperFare documents it.
    http.request("GET", f"{BASE}/v1/airline/availability", json={"provider": "PA"})
    assert json.loads(supplier.calls[-1].content) == {"provider": "PA"}

    http.request("PATCH", f"{BASE}/v1/airline/ancillary", json={"ref_id": "r"})
    assert supplier.calls[-1].method == "PATCH"


def test_refusals_come_back_unchanged_with_the_supplier_status(monkeypatch, supplier):
    http = app_for(monkeypatch, "super_admin")
    supplier.answers["/v1/airline/quote"] = httpx.Response(422, json={"status": False, "message": "Fare expired"})
    response = http.post(f"{BASE}/v1/airline/quote", json={"ref_id": "x"})
    assert (response.status_code, response.json(), response.headers["X-Supplier-Status"]) == \
        (422, {"status": False, "message": "Fare expired"}, "422")
    supplier.answers["/v1/booking/list"] = httpx.Response(403, json={"message": "IP not allowed"})
    response = http.post(f"{BASE}/v1/booking/list", json={})
    assert (response.status_code, response.headers["X-Supplier-Status"], response.json()) == (502, "403", {"message": "IP not allowed"})


def test_booking_changes_are_off_unless_switched_on_and_never_retried(monkeypatch, supplier):
    http = app_for(monkeypatch, "super_admin")
    for path in ("/v1/airline/reservation", "/v1/booking/confirm", "/v1/booking/cancel", "/v1/booking/void"):
        response = http.post(f"{BASE}{path}", json={"booking_ref_id": "b"})
        assert (response.status_code, response.json()["code"]) == (409, "writes_disabled")
    assert http.patch(f"{BASE}/v1/booking/extras", json={}).status_code == 409
    assert supplier.calls == []

    monkeypatch.setattr(settings, "sooperfare_raw_write_enabled", True)
    assert http.post(f"{BASE}/v1/booking/confirm", json={"booking_ref_id": "b"}).status_code == 200
    supplier.answers["/v1/booking/confirm"] = httpx.ReadTimeout("slow")
    response = http.post(f"{BASE}/v1/booking/confirm", json={"booking_ref_id": "b"})
    assert (response.status_code, response.json()["code"]) == (504, "outcome_unknown")
    supplier.answers["/v1/booking/void"] = httpx.Response(401, json={"message": "expired"})
    http.post(f"{BASE}/v1/booking/void", json={})
    assert [c.url.path for c in supplier.calls].count("/v1/booking/void") == 1


def test_a_read_renews_the_login_once_on_401(monkeypatch, supplier):
    http = app_for(monkeypatch, "super_admin")
    supplier.answers["/v1/booking/list"] = httpx.Response(401, json={"message": "expired"})
    http.post(f"{BASE}/v1/booking/list", json={})
    assert [c.url.path for c in supplier.calls].count("/v1/booking/list") == 2


def test_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "sooperfare_base_url", None)
    response = app_for(monkeypatch, "super_admin").post(f"{BASE}/v1/airline/search", json={})
    assert (response.status_code, response.json()["code"]) == (503, "not_configured")
