"""RateHawk client against a mocked supplier: Basic auth, ?data= encoding, errors, write switch, downloads."""

import asyncio
import base64
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.vendors.hotels.ratehawk.client import RateHawkClient, RateHawkError, RateHawkNotConfigured, RateHawkWritesDisabled
from app.vendors.hotels.ratehawk.operations import OPERATIONS

KEY = "synthetic-api-key-xyz"


def client(handler, writes=False):
    return RateHawkClient("https://rh.test/", "1234", SecretStr(KEY), writes_enabled=writes,
                          transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_48_documented_operations_with_unique_keys_and_paths():
    assert len(OPERATIONS) == 48
    assert len({o.key for o in OPERATIONS}) == 48
    assert len({o.path for o in OPERATIONS}) == 48


def test_not_configured_without_credentials():
    with pytest.raises(RateHawkNotConfigured):
        RateHawkClient("https://rh.test", "1234", None)
    with pytest.raises(RateHawkNotConfigured):
        RateHawkClient(None, "1234", SecretStr(KEY))


def test_json_operation_sends_basic_auth_and_body():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "data": {"regions": []}})

    assert run(client(handler).call("multicomplete", {"query": "Dubai", "language": "en"})) == {"regions": []}
    assert seen["auth"] == "Basic " + base64.b64encode(f"1234:{KEY}".encode()).decode()
    assert seen["url"] == "https://rh.test/api/b2b/v3/search/multicomplete/"
    assert seen["body"] == {"query": "Dubai", "language": "en"}


def test_get_operation_sends_payload_as_data_query():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["data"] = json.loads(request.url.params["data"])
        return httpx.Response(200, json={"status": "ok", "data": {"documents": []}})

    run(client(handler).call("closing_documents_info", {"order_ids": ["1"]}))
    assert seen == {"method": "GET", "data": {"order_ids": ["1"]}}


def test_supplier_error_status_is_raised_with_code_and_no_key_in_logs(caplog):
    def handler(request):
        return httpx.Response(400, json={"status": "error", "error": "invalid_params", "debug": {"validation_error": "currency"}})

    caplog.set_level(logging.DEBUG)
    with pytest.raises(RateHawkError) as error:
        run(client(handler).call("search_region", {"currency": "PKR"}))
    assert error.value.error_code == "invalid_params"
    assert error.value.debug == {"validation_error": "currency"}
    assert KEY not in caplog.text


def test_ok_http_with_error_status_body_is_an_error():
    def handler(request):
        return httpx.Response(200, json={"status": "error", "error": "hotel_not_found", "data": None})

    with pytest.raises(RateHawkError) as error:
        run(client(handler).call("hotel_info", {"hid": 1}))
    assert error.value.error_code == "hotel_not_found"


def test_rate_limit_and_timeout():
    with pytest.raises(RateHawkError) as error:
        run(client(lambda r: httpx.Response(429, json={"status": "error", "error": "overlimit"})).call("overview"))
    assert error.value.status == 429

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(RateHawkError) as error:
        run(client(slow).call("overview"))
    assert error.value.error_code == "timeout"


def test_write_operations_are_refused_when_switched_off_and_never_sent():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "ok", "data": None})

    for key in ("booking_finish", "order_cancel", "profile_delete", "order_group_pay_overpay"):
        with pytest.raises(RateHawkWritesDisabled):
            run(client(handler).call(key, {}))
    assert calls == []
    run(client(handler, writes=True).call("order_cancel", {"partner_order_id": "x"}))
    assert len(calls) == 1


def test_write_timeout_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(RateHawkError):
        run(client(handler, writes=True).call("booking_finish", {}))
    assert len(calls) == 1


def test_download_returns_file_and_json_answer_is_an_error():
    def pdf(request):
        return httpx.Response(200, content=b"%PDF-1", headers={"content-type": "application/pdf",
                                                                "content-disposition": 'attachment; filename="voucher.pdf"'})

    file = run(client(pdf).download("voucher_download", {"partner_order_id": "x", "language": "en"}))
    assert (file.content, file.content_type, file.filename) == (b"%PDF-1", "application/pdf", "voucher.pdf")

    def json_error(request):
        return httpx.Response(200, json={"status": "error", "error": "order_not_found"})

    with pytest.raises(RateHawkError) as error:
        run(client(json_error).download("voucher_download", {"partner_order_id": "x"}))
    assert error.value.error_code == "order_not_found"
