"""Southair client against a mocked supplier: token caching, 401 refresh, form encoding, no ticketing retry."""

import asyncio
import logging
from datetime import date
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import SecretStr

from app.vendors.flights.southair.client import SouthairClient, SouthairError, SouthairNotConfigured

PASSWORD = "synthetic-password-xyz"


class Supplier:
    def __init__(self):
        self.calls = []
        self.tokens = 0
        self.expire_next = set()
        self.responses = {}

    def __call__(self, request: httpx.Request):
        path = request.url.path.removeprefix("/api/ota/v1")
        form = dict(parse_qsl(request.content.decode()))
        self.calls.append((path, form, request.headers.get("authorization"), request.headers.get("content-type")))
        if path == "/login":
            self.tokens += 1
            return httpx.Response(200, json={"status_code": 200, "data": {"token": f"tok-{self.tokens}"}})
        if path in self.expire_next:
            self.expire_next.discard(path)
            return httpx.Response(401, json={"message": "unauthenticated.", "status": 401})
        return self.responses.get(path, httpx.Response(200, json={"status_code": 200, "message": "success", "data": {"ok": path}}))


@pytest.fixture(autouse=True)
def fresh_token():
    SouthairClient.reset_token()
    SouthairClient._lock = None
    yield
    SouthairClient.reset_token()


def client(supplier, clock=lambda: 1_000_000.0):
    return SouthairClient("https://southair.invalid/", "travelian-user", SecretStr(PASSWORD), 5,
                          transport=httpx.MockTransport(supplier), clock=clock)


def run(coro):
    return asyncio.run(coro)


def test_requires_configuration():
    for args in ((None, "u", SecretStr("p")), ("https://x", None, SecretStr("p")), ("https://x", "u", None), ("https://x", "u", SecretStr(""))):
        with pytest.raises(SouthairNotConfigured):
            SouthairClient(*args)


def test_logs_in_once_reuses_token_and_sends_form_encoded_dates():
    supplier = Supplier()
    api = client(supplier)
    run(api.search(origin="KHI", destination="MUX", departure=date(2026, 11, 27), return_date=date(2026, 11, 30), adults=2, children=1, infants=0))
    run(api.profile())
    login, search, profile = supplier.calls
    assert login[0] == "/login" and login[1] == {"username": "travelian-user", "password": PASSWORD} and login[2] is None
    assert search[0] == "/res/flights" and search[3].startswith("application/x-www-form-urlencoded")
    assert search[1] == {"adult": "2", "child": "1", "infant": "0", "travel_type": "RT", "dep_city[0]": "KHI",
                         "arr_city[0]": "MUX", "dep_date[0]": "27-11-2026", "ret_date[0]": "30-11-2026"}
    assert search[2] == profile[2] == "Bearer tok-1" and supplier.tokens == 1


def test_one_way_has_no_return_date():
    supplier = Supplier()
    run(client(supplier).search(origin="KHI", destination="MUX", departure=date(2026, 1, 5), return_date=None, adults=1, children=0, infants=0))
    form = supplier.calls[-1][1]
    assert form["travel_type"] == "OW" and form["dep_date[0]"] == "05-01-2026" and "ret_date[0]" not in form


def test_token_is_kept_for_seven_days_then_renewed():
    supplier, now = Supplier(), [1_000_000.0]
    api = client(supplier, clock=lambda: now[0])
    run(api.profile())
    now[0] += 6 * 24 * 3600
    run(api.profile())
    assert supplier.tokens == 1
    now[0] += 24 * 3600
    run(api.profile())
    assert supplier.tokens == 2


def test_401_logs_in_again_and_retries_once():
    supplier = Supplier()
    api = client(supplier)
    run(api.profile())
    supplier.expire_next.add("/my-profile")
    assert run(api.profile()) == {"ok": "/my-profile"}
    assert supplier.tokens == 2 and supplier.calls[-1][2] == "Bearer tok-2"


def test_ticketing_is_never_retried():
    supplier = Supplier()
    api = client(supplier)
    run(api.profile())
    supplier.expire_next.add("/res/ticketing")
    with pytest.raises(SouthairError) as error:
        run(api.ticketing(ticket_ref="ref", cash_amount="1000.00"))
    assert error.value.error_code == "unauthenticated"
    assert [call[0] for call in supplier.calls].count("/res/ticketing") == 1
    form = next(call[1] for call in supplier.calls if call[0] == "/res/ticketing")
    assert form == {"ticket_ref": "ref", "payment_method": "CASH", "cash_amount": "1000.00"}


def test_fare_info_booking_and_pnr_use_documented_fields():
    supplier = Supplier()
    api = client(supplier)
    run(api.fare_info(nld="NLD-1", selections=[("KHIMUX", "H-OUT"), ("MUXKHI", "H-RET")]))
    assert supplier.calls[-1][1] == {"carsf": "NLD-1", "__flt[0][KHIMUX]": "H-OUT", "__flt[1][MUXKHI]": "H-RET"}
    run(api.booking(nld="NLD-1", acceptances=["H-OUT"], contact={"email": "ops@example.invalid", "code": "92", "phone": "3001234567"},
                    passengers=[{"type": "ADT", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-01",
                                 "name_title": "MR", "id_type": "PSPT", "passport_no": "AB1234567"},
                                {"type": "INF", "first_name": "Baby", "last_name": "Passenger", "date_of_birth": "2025-06-01",
                                 "name_title": "MISS", "id_type": "PSPT", "infant_link_id": "1"}], remarks="Travelian booking"))
    form = supplier.calls[-1][1]
    assert form["nld"] == "NLD-1" and form["acceptance[0]"] == "H-OUT" and form["code1"] == "92"
    assert form["first_name[ADT][0]"] == "Test" and form["infant_link_id[INF][0]"] == "1" and "passport_no[INF][0]" not in form
    run(api.pnr_info("63B55C"))
    assert supplier.calls[-1][0] == "/res/pnr_info/63B55C"
    with pytest.raises(SouthairError):
        run(api.pnr_info("../x"))


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"status_code": 400, "message": "invalid.selection_not_valid"}),
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="not json"),
])
def test_supplier_errors_raise_without_leaking(response, caplog):
    supplier = Supplier()
    supplier.responses["/res/flights"] = response
    with caplog.at_level(logging.DEBUG), pytest.raises(SouthairError) as error:
        run(client(supplier).search(origin="KHI", destination="MUX", departure=date(2026, 1, 5), return_date=None, adults=1, children=0, infants=0))
    assert "supplier" in error.value.public_message.lower()
    assert PASSWORD not in caplog.text and "tok-1" not in caplog.text and "selection_not_valid" not in str(error.value.public_message)


def test_bad_login_raises():
    def supplier(request):
        return httpx.Response(200, json={"status_code": 400, "message": "invalid credentials"})
    with pytest.raises(SouthairError) as error:
        run(SouthairClient("https://x.invalid", "u", SecretStr("p"), transport=httpx.MockTransport(supplier)).profile())
    assert error.value.error_code == "login_failed"
