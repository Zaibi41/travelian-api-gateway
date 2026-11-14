import json
import unittest

import httpx

from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient


class DhilluSahiClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_and_group_inventory_contract(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/api/login":
                return httpx.Response(200, json={"success": {"token": "test-token", "name": "Agent"}})
            return httpx.Response(200, json={"success": True, "data": [{"sector": "DMM - LHE", "airline": {"name": "PF"}, "tickets": []}]})

        client = DhilluSahiAuthClient("https://vendor.test", transport=httpx.MockTransport(handler))
        login = await client.login("agent@example.test", "sample-password")
        groups = await client.fetch_groups(login["token"], "101")

        self.assertEqual(len(groups), 1)
        self.assertEqual(seen[0].url.path, "/api/login")
        self.assertEqual(json.loads(seen[0].content), {"email": "agent@example.test", "password": "sample-password"})
        self.assertEqual(seen[1].url.path, "/api/pnr-respect-to-mysector-user")
        self.assertEqual(seen[1].headers["Authorization"], "Bearer test-token")
        self.assertEqual(json.loads(seen[1].content), {"my_sector_id": "101"})

    async def test_availability_booking_and_details_contracts(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/api/check-availbility":
                return httpx.Response(200, json={"success": True, "data": {"status": False}})
            if request.url.path == "/api/add-booking":
                return httpx.Response(200, json={"success": True, "data": {"id": 27049}})
            return httpx.Response(200, json={"success": True, "data": {"data": {"id": 2749, "booking_sub": []}, "groupdata": {"id": 188, "subpart": []}}, "message": "Booking Details Fetch Successful"})

        client = DhilluSahiAuthClient("https://vendor.test", transport=httpx.MockTransport(handler))
        available = await client.check_availability("test-token", pnr="33MBD5", adult=1, child=1, price=78000)
        booking_id = await client.add_booking("test-token", {"pnr": "9EGGG7", "agent": 828})
        details = await client.fetch_booking_details("test-token", 2749)

        self.assertFalse(available)
        self.assertEqual(booking_id, 27049)
        self.assertEqual(details["booking"]["id"], 2749)
        self.assertEqual(details["group"]["id"], 188)
        self.assertEqual(details["message"], "Booking Details Fetch Successful")
        self.assertEqual([request.url.path for request in seen], [
            "/api/check-availbility", "/api/add-booking", "/api/agent-booking-detail"
        ])
        for request in seen:
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        self.assertEqual(json.loads(seen[0].content), {"pnr": "33MBD5", "adult": 1, "child": 1, "price": 78000})
        self.assertEqual(json.loads(seen[2].content), {"booking_id": 2749})


if __name__ == "__main__":
    unittest.main()
