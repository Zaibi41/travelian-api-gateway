"""Synthetic RateHawk answers shaped like the sandbox's (17 Nov 2026). Not real supplier data."""


def payment(amount: str, *, free_until: str | None = "2026-12-16T00:00:00", commission: str = "10.67") -> dict:
    policies = ([{"start_at": None, "end_at": free_until, "amount_charge": "0.00"},
                 {"start_at": free_until, "end_at": None, "amount_charge": amount}]
                if free_until else [{"start_at": None, "end_at": None, "amount_charge": amount}])
    return {"payment_types": [{
        "amount": amount, "show_amount": amount, "currency_code": "USD", "show_currency_code": "USD", "type": "deposit",
        "commission_info": {"charge": {"amount_gross": amount, "amount_net": amount, "amount_commission": commission}},
        "cancellation_penalties": {"policies": policies, "free_cancellation_before": free_until},
    }]}


def rate(amount: str, meal: str = "nomeal", room: str = "Standard Double room", nights: int = 2, **kwargs) -> dict:
    return {"match_hash": f"m-{amount}", "book_hash": None, "daily_prices": ["1"] * nights, "meal": meal,
            "meal_data": {"value": meal}, "room_name": room, "payment_options": payment(amount, **kwargs)}


def serp() -> dict:
    return {"hotels": [
        {"hid": 101, "id": "hotel_a", "rates": [rate("120.00", "breakfast"), rate("96.00")]},
        {"hid": 102, "id": "hotel_b", "rates": [rate("80.00", free_until=None)]},
        {"hid": 103, "id": "no_content", "rates": [rate("50.00")]},
    ], "total_hotels": 3}


def content() -> list[dict]:
    return [
        {"hid": 101, "name": "Hotel A", "star_rating": 4, "address": "1 Road", "region": {"name": "Dubai", "country_code": "AE"}},
        {"hid": 102, "name": "Hotel B", "star_rating": 0, "address": "2 Road", "region": {"name": "Dubai", "country_code": "AE"}},
    ]


def suggest() -> dict:
    return {"regions": [{"id": 6053839, "name": "Dubai", "type": "RT_CITY", "country_code": "AE"}],
            "hotels": [{"hid": 101, "id": "hotel_a", "name": "Hotel A", "region_id": 6053839}]}
