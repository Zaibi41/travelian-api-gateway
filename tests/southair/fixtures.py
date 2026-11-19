"""Synthetic Southair payloads shaped like the AEROST guide (2.2) and the UAT variant."""

NLD = "NLD-SECRET-eyJpdWFj"
HASH_EB = "HASH-EB-eyJpSECRET"
HASH_UAT = "HASH-UAT-eyJpSECRET"
HASH_RET = "HASH-RET-eyJpSECRET"


def flight(callsign="MM112", dep="2026-11-27 23:00:00", arr="2026-11-28 00:00:00", fare_shape="guide", total="457.70", fare_hash=HASH_EB):
    main = {"dep_date_time": dep, "arr_date_time": arr, "dep_dt_utc": "2026-11-27 20:00:00", "arr_dt_utc": "2026-11-27 21:00:00",
            "dep_city": "KHI", "arr_city": "MUX", "STOP_COUNT": "0", "STOP_CITY": "-", "flight_time": "01h 00m", "callsign": callsign}
    body = {"FLT_MAIN": main, "FLT_INFO": {"KHIMUX": {"INFO": main, "OPRT": {"name": "Synthetic Aviation", "iata_code": "MM", "icao_code": "MNM", "ticket_code": "740"}, "EQPT": {"full_type": "CRJ900"}}}, "SEAT": "HK"}
    if fare_shape == "guide":
        body["CLASS"] = {
            "EP": {"ACCEPTANCE": "NA"},
            "EB": {"ACCEPTANCE": fare_hash, "TARRIF": {"ADT": {"BASE": {"YQ": "0.00 PKR", "YR": "0.00 PKR", "FARE": "50.00 PKR"}, "TFC": {"AMT": "348.00"}, "TAX": {"VAT-1": "59.70 PKR"}, "TOTAL": total}},
                   "FEATURES": [{"title": "Meal", "service_text": "Snacks"}, {"title": "Time Limit", "service_text": "1 hours for domestic"}, {"title": "Refund", "service_text": "Refundable"}]},
        }
    else:
        body["AVB"] = {"CABIN": {"Y": {"STATUS": 1, "HASH": HASH_UAT, "TARRIF": {"ADT": {"TOTAL": total}}}, "Z": {"STATUS": 0, "HASH": "closed"}}}
    return body


def search_data(round_trip=False, **kwargs):
    result = {"KHI-MUX": {"itinerary": {"FROM": "KHI", "TO": "MUX", "DATE": "27 Nov 2026"}, "flights": {"1": flight(**kwargs), "2": flight("MM114", "2026-11-27 08:00:00", "2026-11-27 09:10:00", fare_shape="uat", total="300.00")}}}
    if round_trip:
        back = flight("MM115", "2026-11-30 10:00:00", "2026-11-30 11:00:00", total="400.00", fare_hash=HASH_RET)
        result["MUX-KHI"] = {"itinerary": {"FROM": "MUX", "TO": "KHI", "DATE": "30 Nov 2026"}, "flights": {"1": back}}
    return {"request": {"adult": "1", "NLD": NLD}, "result": result}


HASH_LIVE = {"EB": "HASH-LIVE-EB-SECRET", "EV": "HASH-LIVE-EV-SECRET", "EP": "HASH-LIVE-EP-SECRET"}


def live_uat_search_data():
    """Sanitized copy of a real UAT search, LHE-KHI 19 Nov 2026 (shape and amounts; NLD and hashes replaced)."""
    def highlight(baggage):
        return [
            {"LIMITED": 0, "INFO": {"title": "Checkin Baggage", "feature_code": "checkin_baggage", "icon_code": "fa fa-suitcase", "info": baggage}},
            {"LIMITED": 0, "INFO": {"title": "Validity", "feature_code": "validity", "icon_code": "fa fa-ticket", "info": "Validity 6 Months"}},
        ]
    info = {"airline_id": 1, "flight_no": "266", "dep_city": "LHE", "arr_city": "KHI", "dep_date_time": "2026-11-19 00:25:00",
            "arr_date_time": "2026-11-19 02:35:00", "dep_dt_utc": "2026-11-18 19:25:00", "arr_dt_utc": "2026-11-18 21:35:00",
            "leg_no": 1, "is_connecting": 0, "conn_to": "", "equipment_id": 1, "flight_status": "ONTIME", "STOP_COUNT": 0,
            "STOP_CITY": "-", "flight_time": 130, "callsign": "Z8266", "short": "Z8266 19NOV LHEKHI", "operated_by": None}
    flight = {
        "FLT_INFO": {"LHEKHI": {"INFO": info, "OPRT": {"name": "South Air", "iata_code": "Z8", "icao_code": "SOA", "ticket_code": "995"},
                                "EQPT": {"full_type": "ATR72-500", "short_type": "AT75"}}},
        "AVB": {"CABIN": {
            "EB": {"STATUS": 1, "ACC": 13330, "HASH": HASH_LIVE["EB"], "HIGHLIGHT": highlight("Nill Baggage")},
            "EV": {"STATUS": 1, "ACC": 14924, "HASH": HASH_LIVE["EV"], "HIGHLIGHT": highlight("20KG")},
            "EP": {"STATUS": 1, "ACC": 16518, "HASH": HASH_LIVE["EP"], "HIGHLIGHT": highlight("30KG")},
        }, "SEAT": "HK"},
    }
    return {"request": {"adult": "1", "child": "0", "infant": "0", "travel_type": "O", "dep_city": "LHE", "arr_city": "KHI",
                        "dep_date": "19-11-2026", "NLD": NLD},
            "result": {"LHE-KHI": {"itinerary": {"FROM": "LHE", "TO": "KHI", "DATE": "19 Nov 2026"}, "flights": {"6af1bc80e37bf": flight}}}}


def live_fare_info(final_total=13330):
    """Sanitized shape of a real UAT fare_info for one adult on EB (LHE-KHI): components and FINAL_TOTAL, no ADT.TOTAL."""
    return {
        "ADT": {"BASE": {"FARE": {"VALUE": 10528}, "YR": {"VALUE": 316}, "YQ": {"VALUE": 316}},
                "TFC": {"YI": {"VALUE": 20}, "XZ": {"VALUE": 100}, "SP": {"VALUE": 500},
                        "N9": {"TITLE": "STAMP TAX - N9 (TAX)", "VALUE": 50}, "PK": {"VALUE": 1500}}},
        **({"FINAL_TOTAL": final_total} if final_total is not None else {}),
    }


def live_fare_info_with(kind, final_total):
    """Sanitized live fare_info for 1 adult plus 1 infant or child (amounts as returned in UAT, 9 Nov 2026)."""
    adult = live_fare_info(None)["ADT"]
    if kind == "INF":
        other = {"BASE": {"FARE": {"VALUE": 1053}, "YR": {"VALUE": 316}, "YQ": {"VALUE": 316}},
                 "TFC": {code: {"VALUE": "-"} for code in ("YI", "XZ", "SP", "N9", "PK")}, "TAX": []}
    else:
        other = {"BASE": {"FARE": {"VALUE": 7896}, "YR": {"VALUE": 316}, "YQ": {"VALUE": 316}},
                 "TFC": {"YI": {"VALUE": 20}, "XZ": {"VALUE": 100}, "SP": {"VALUE": 500}, "N9": {"VALUE": 50}, "PK": {"VALUE": 1500}}, "TAX": []}
    return {"ADT": {**adult, "TAX": []}, kind: other, **({"FINAL_TOTAL": final_total} if final_total is not None else {})}


def pnr_info(pnr="63B55C", status="BK", tickets=(None, None), paid=0, total=457.70, can_ticketing=True):
    """Sanitized pnr_info in the UAT shape recorded on 9 Nov 2026 (field names and status values as
    seen; no passenger data). The ticketed variant (tkt_no set, totalAmountPaid = total) is not yet
    seen live: ticketing can't run in UAT with a zero balance."""
    types = ["ADT", "INF"][:len(tickets)]
    return {
        "PNRN": pnr, "invoice_no": "", "totalAmountPaid": paid, "can_ticketing": can_ticketing, "can_update_res": 1, "seat_status": "HK",
        "res": {"pnrn": pnr, "status": status, "total_amount": total, "booked_currency": "PKR"},
        "names": [{"name_type": kind, "tkt_no": number, "id_type": "PSPT"} for kind, number in zip(types, tickets)],
        "flight": [{"seat_status": "HK"}],
    }


def ticketed_pnr_info(pnr="EB2F2F", tickets=("9000000000672",), total=13570):
    """Sanitized pnr_info after ticketing, as returned live in UAT (19 Nov 2026, PNR EB2F2F): res.status OK, everything paid,
    invoice_no set, names[].tkt_no still empty and the ticket number in names[].sale.tkt_no. No passenger data."""
    data = pnr_info(pnr=pnr, status="OK", tickets=tuple(None for _ in tickets), paid=total, total=total)
    data["invoice_no"] = "305232-1912371347"
    for name, number in zip(data["names"], tickets):
        name["sale"] = {"tkt_no": number}
    return data


#: Ticketing answer as documented in the supplier guide (data.booking_status "OK", pnrn, invoice_no).
TICKETING_OK = {"booking_status": "OK", "pnrn": "63B55C", "invoice_no": "INV-UAT-1"}


#: The booking reply as returned live in UAT (9 Nov 2026, PNR 5A5282): no total in it.
BOOKED_LIVE = {"booking_status": 1, "pnrn": "5A5282", "pax_count": 1, "time_limit": "2026-11-10 18:14:09 UTC", "ticket_ref": "TICKET-REF-SECRET"}


def live_pnr_info(total=14390, status="BK", currency="PKR", pnr="5A5282"):
    """Sanitized pnr_info for that hold (1 adult, EB LHE-KHI): amounts and status as returned, no personal data."""
    data = pnr_info(pnr=pnr, status=status, tickets=(None,), total=total)
    data["res"].update({"fare": 12220, "taxes": 2170, "es_amount": 0, "discount": 0, "booked_currency": currency, "farebasis": "EBU6M"})
    data["names"][0]["price"] = [{"price_type": "FARE", "sub_type": sub, "amount": amount} for sub, amount in (("FARE", 11528), ("YR", 346), ("YQ", 346))] + [
        {"price_type": "TFC", "sub_type": sub, "amount": amount} for sub, amount in (("FEE", 20), ("TAX", 100), ("FEE", 500), ("TAX", 50), ("TAX", 1500))]
    if total is None:
        del data["res"]["total_amount"]
    return data
