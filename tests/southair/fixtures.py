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
