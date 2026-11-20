# SooperFare API: recorded requests and responses

Live calls from the gateway server (static IP 52.76.26.224) to `https://api.sooperfare.com` on **19 Nov 2026, 16:23-16:24 UTC**,
approved by the owner: LHE -> KHI on 10 Dec 2026, **Airblue (PA401) only**, one TEST passenger, then cancelled.
The integration (`app/vendors/flights/sooperfare`, `app/services/sooperfare_holds.py`, `app/services/sooperfare_bookings.py`) is built from these answers.

**Masked by the recorder:** the login call and its token, every `X-SF-Authorization` header, passwords, phone numbers, email
addresses and the CNIC. The recorder also replaced **every** `name` field (people, and by over-matching airports, cities and
the currency) with `TEST TRAVELIAN`; airports and cities are identified by their `iata`/`code` fields. Long lists are cut to
their first item (`... N more`).

## What we learned

| Step | Endpoint | Result |
|---|---|---|
| Availability | `GET /v1/airline/availability` (JSON body) | providers `PA, PF, PK, ONEAPI, Z8` |
| Search | `POST /v1/airline/search` (provider `PA`) | PA401 10 Dec 11:15, fare *Value*, Rs 24,050 for 1 adult |
| Quote | `POST /v1/airline/quote` | passenger slot needs `title, first_name, last_name, gender, dob, nationality, nic` (CNIC for this domestic fare); platform fee Rs 20 |
| Reservation | `POST /v1/airline/reservation` | `data.uuid` 49c85ade-bdfe-48a9-b8f1-2424635b702c, `order_ref_id` **spa-0rj**, PNR **OFZOJG**, `reservation_status` active, `billable_price` 24070.00, expires 20 Nov 21:23 PKT |
| Verify | `POST /v1/booking/list` | listed: `booking.reservation_status` active; `additional.is_unpaid` true |
| Cancel | `POST /v1/booking/cancel` | `status: true`, "Operation Successfull": a transaction `txn-016`, description "Cancel For Airline Booking.", **status `unpaid`** |
| Verify cancel | `POST /v1/booking/list` (+2 s, +8 s, +3.5 min) | **still `active`, `additional.is_cancelled` false**: the cancel was accepted but not applied (for comparison, a reservation that expired shows `is_cancelled` true) |

Consequences in Travelian: a booking is marked **Cancelled only when `booking/list` says so** (`is_cancelled`, or a status
other than active); until then the admin shows "Cancel requested at SooperFare" with a read-only *Check with SooperFare*.
Open question for SooperFare: how an unpaid reservation is cancelled immediately (is `booking/cancel` processed by your team,
or only for issued bookings? is `booking/void` meant for unpaid reservations?).

`/v1/booking/confirm` was **not** called (it issues and charges the balance). Its body, from SooperFare's collection:
`booking_ref_id, txn_ref_id, amount, currency, remarks`.

## `POST /v1/booking/confirm` (issuing): waiting for the first real issue

Built in gateway#45; **not called yet**. The owner issues the first real booking from the admin (booking, Payments tab,
*2 · Issue the ticket*). Its request and SooperFare's answer are saved on that booking (`supplier_response.ticketing`:
`confirm.request`, `confirm.answer`, `booking_list`) and will be copied here, with secrets and personal data removed.

Before sending, `POST /v1/booking/list` must show the booking `active`, not paid, not issued, with `billable_price` equal to
the supplier cost on the booking; otherwise nothing is sent. SooperFare bookings are never issued automatically after a card
payment (staff get a red alert and issue from the admin).

What the gateway sends (one call, never retried), for a held, fully paid booking that isn't a TEST booking:

```json
{"booking_ref_id": "<SooperFare data.uuid from the reservation>", "txn_ref_id": "<Travelian reference, e.g. TRV-XXXXXX>",
 "amount": 24070.0, "currency": "PKR", "remarks": "Travelian booking TRV-XXXXXX, PNR XXXXXX"}
```

Then `POST /v1/booking/list` (re-read after 0, 3 and 6 s): **Ticket issued** only when `additional.is_issued` is true and
every passenger in `booking.providers[].legs[].passengers[]` has a `ticket_number` (matched to our travelers by type and
order). Anything else leaves the booking *unknown*, so staff use *Check with supplier* (read-only) and nobody confirms twice.
After a confirm, *Check with supplier* never re-opens issuing just because the list still shows the booking on hold (the list
can lag); staff check SooperFare's portal and use *Mark as not issued* with a reason.

### First real confirm

_To be filled in after the first real issue._

## Calls in order

### 1. `GET /v1/airline/availability` at 2026-11-19T16:23:40Z (1687 ms, ok)

Request:

```json
{
 "flexible_dates": false,
 "trip_type": "one_way",
 "cabin_class": "economy",
 "legs": [
  {
   "from": "LHE",
   "to": "KHI",
   "date": "2026-12-10",
   "return": null
  }
 ],
 "travelers": [
  {
   "type": "adult",
   "qty": 1
  },
  {
   "type": "child",
   "qty": 0
  },
  {
   "type": "infant",
   "qty": 0
  }
 ],
 "redirect": true
}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": {
  "ref_id": "6e431dd4-0e0d-4239-876d-1c425c99b9b6",
  "providers": [
   {
    "identifier": "PA",
    "created_at": "2026-01-09T11:13:01+05:00",
    "updated_at": "2026-11-18T20:48:03+05:00"
   },
   {
    "identifier": "PF",
    "created_at": "2026-01-09T11:13:01+05:00",
    "updated_at": "2026-11-18T20:48:03+05:00"
   },
   {
    "identifier": "PK",
    "created_at": "2026-01-09T11:13:01+05:00",
    "updated_at": "2026-11-18T20:48:03+05:00"
   },
   {
    "identifier": "ONEAPI",
    "created_at": "2026-01-09T11:13:01+05:00",
    "updated_at": "2026-11-18T20:48:03+05:00"
   },
   {
    "identifier": "Z8",
    "created_at": "2026-08-07T18:57:20+05:00",
    "updated_at": "2026-11-18T20:48:03+05:00"
   }
  ],
  "analytics": []
 }
}
```

### 2. `POST /v1/airline/search` at 2026-11-19T16:23:42Z (2891 ms, ok)

Request:

```json
{
 "flexible_dates": false,
 "trip_type": "one_way",
 "cabin_class": "economy",
 "legs": [
  {
   "from": "LHE",
   "to": "KHI",
   "date": "2026-12-10",
   "return": null
  }
 ],
 "travelers": [
  {
   "type": "adult",
   "qty": 1
  },
  {
   "type": "child",
   "qty": 0
  },
  {
   "type": "infant",
   "qty": 0
  }
 ],
 "redirect": true,
 "provider": "PA"
}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": {
  "ref_id": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
  "providers": [
   {
    "provider": {
     "identifier": "PA",
     "created_at": "2026-01-09T11:13:01+05:00",
     "updated_at": "2026-11-18T20:48:03+05:00"
    },
    "legs": [
     {
      "ref_id": "08ff3eaf-ba2f-496a-b477-41ecdda11669",
      "trip_nature": "domestic",
      "fares_on_request": false,
      "flights": [
       {
        "ref_id": "54fc0167-19a7-42e4-a289-02c1c44baa96",
        "signature": "LHEKHIPAPA401@2026-12-10 11:15",
        "flight_operation": "self",
        "is_recommended": 1,
        "is_refundable": true,
        "recommended_priority": 1,
        "has_layovers": false,
        "layovers_count": 0,
        "layovers_time": 0,
        "change_of_plane": false,
        "travel_time": 105,
        "distance": 0,
        "marketing_carrier": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "from": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "country": {
          "name": "TEST TRAVELIAN",
          "alpha_2": "PK",
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
          "url": "https://api.sooperfare.com/pk",
          "created_at": "2025-04-30T02:39:15+05:00",
          "updated_at": "2026-04-05T12:23:54+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:15:06+05:00"
        },
        "to": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "country": {
          "name": "TEST TRAVELIAN",
          "alpha_2": "PK",
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
          "url": "https://api.sooperfare.com/pk",
          "created_at": "2025-04-30T02:39:15+05:00",
          "updated_at": "2026-04-05T12:23:54+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "segments": [
         {
          "ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
          "operating_carrier": {
           "name": "TEST TRAVELIAN",
           "iata": "PA",
           "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
           "created_at": "2025-05-07T17:41:55+05:00",
           "updated_at": "2026-11-19T21:23:07+05:00"
          },
          "departure_at": "2026-12-10T11:15:00+05:00",
          "arrival_at": "2026-12-10T13:00:00+05:00",
          "from": {
           "name": "TEST TRAVELIAN",
           "iata": "LHE",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "LHE",
            "created_at": "2025-04-30T07:39:19+05:00",
            "updated_at": "2025-10-01T18:26:44+05:00"
           },
           "country": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "created_at": "2025-04-30T03:21:44+05:00",
           "updated_at": "2026-11-19T21:15:06+05:00"
          },
          "to": {
           "name": "TEST TRAVELIAN",
           "iata": "KHI",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "KHI",
            "created_at": "2025-04-30T07:39:31+05:00",
            "updated_at": "2025-10-01T15:12:56+05:00"
           },
           "country": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "created_at": "2025-04-30T03:21:38+05:00",
           "updated_at": "2026-11-19T21:17:17+05:00"
          },
          "from_terminal": {
           "gate": ""
          },
          "to_terminal": {
           "gate": ""
          },
          "cabin_class": "economy",
          "flight_time": 105,
          "layover_time": 0,
          "flight_number": "PA401",
          "distance": 1021.43,
          "aircraft": {
           "manufacturer": "A321",
           "model": "A321",
           "created_at": "2024-12-23T18:03:44+05:00",
           "updated_at": "2024-12-23T18:03:44+05:00"
          }
         }
        ],
        "ancillaries": {
         "baggages": [],
         "meals": [],
         "seatplans": [],
         "ssrs": []
        },
        "fares": [
         {
          "ref_id": "34367e11-f47d-47c6-8fed-5d1a05cef955",
          "signature": "Value|EV|1",
          "is_refundable": true,
          "name": "TEST TRAVELIAN",
          "class": "EV",
          "name_class": "Value",
          "available_seats": 0,
          "passenger_fares": [
           {
            "traveler_type": "adult",
            "total_passenger": 1,
            "base_price": 13030,
            "surcharge": 0,
            "taxes": 2145,
            "fees": 8875,
            "service_charges": 0,
            "ancillaries_charges": 0,
            "total_price": 24050
           }
          ],
          "fare_policies": [
           {
            "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
            "title": "Value",
            "description": "<0 hours before departure",
            "type": "exchange",
            "price": 8000,
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": "adult"
           },
           "... 2 more"
          ],
          "baggage_policies": [
           {
            "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
            "title": "Value",
            "description": "1 hand bag(s), up to 7 kg",
            "weight_limit": 7,
            "weight_unit": "kg",
            "pieces": 1,
            "type": "carry",
            "traveler_type": "adult"
           },
           "... 1 more"
          ],
          "combinability_fares": [],
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "base_price": 13030,
          "surcharge": 0,
          "taxes": 2145,
          "fees": 8875,
          "service_charges": 0,
          "ancillaries_charges": 0,
          "total_price": 24050,
          "total_discount": 0,
          "billable_price": 24050
         },
         "... 2 more"
        ]
       }
      ]
     },
     "... 1 more"
    ]
   }
  ]
 }
}
```

### 3. `POST /v1/airline/quote` at 2026-11-19T16:23:45Z (873 ms, ok)

Request:

```json
{
 "ref_id": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
 "legs": [
  {
   "ref_id": "08ff3eaf-ba2f-496a-b477-41ecdda11669",
   "flight": {
    "ref_id": "54fc0167-19a7-42e4-a289-02c1c44baa96",
    "fare": {
     "ref_id": "34367e11-f47d-47c6-8fed-5d1a05cef955"
    }
   }
  }
 ]
}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": {
  "ref_id": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
  "providers": [
   {
    "provider": {
     "identifier": "PA",
     "created_at": "2026-01-09T11:13:01+05:00",
     "updated_at": "2026-11-18T20:48:03+05:00"
    },
    "legs": [
     {
      "trip_nature": "domestic",
      "flight": {
       "ref_id": "54fc0167-19a7-42e4-a289-02c1c44baa96",
       "signature": "LHEKHIPAPA401@2026-12-10 11:15",
       "flight_operation": "self",
       "is_recommended": 1,
       "is_refundable": true,
       "recommended_priority": 1,
       "has_layovers": false,
       "layovers_count": 0,
       "layovers_time": 0,
       "change_of_plane": false,
       "travel_time": 105,
       "distance": 0,
       "marketing_carrier": {
        "name": "TEST TRAVELIAN",
        "iata": "PA",
        "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
        "created_at": "2025-05-07T17:41:55+05:00",
        "updated_at": "2026-11-19T21:23:07+05:00"
       },
       "departure_at": "2026-12-10T11:15:00+05:00",
       "arrival_at": "2026-12-10T13:00:00+05:00",
       "from": {
        "name": "TEST TRAVELIAN",
        "iata": "LHE",
        "city": {
         "name": "TEST TRAVELIAN",
         "code": "LHE",
         "created_at": "2025-04-30T07:39:19+05:00",
         "updated_at": "2025-10-01T18:26:44+05:00"
        },
        "country": {
         "name": "TEST TRAVELIAN",
         "alpha_2": "PK",
         "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
         "url": "https://api.sooperfare.com/pk",
         "created_at": "2025-04-30T02:39:15+05:00",
         "updated_at": "2026-04-05T12:23:54+05:00"
        },
        "created_at": "2025-04-30T03:21:44+05:00",
        "updated_at": "2026-11-19T21:15:06+05:00"
       },
       "to": {
        "name": "TEST TRAVELIAN",
        "iata": "KHI",
        "city": {
         "name": "TEST TRAVELIAN",
         "code": "KHI",
         "created_at": "2025-04-30T07:39:31+05:00",
         "updated_at": "2025-10-01T15:12:56+05:00"
        },
        "country": {
         "name": "TEST TRAVELIAN",
         "alpha_2": "PK",
         "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
         "url": "https://api.sooperfare.com/pk",
         "created_at": "2025-04-30T02:39:15+05:00",
         "updated_at": "2026-04-05T12:23:54+05:00"
        },
        "created_at": "2025-04-30T03:21:38+05:00",
        "updated_at": "2026-11-19T21:17:17+05:00"
       },
       "segments": [
        {
         "ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "operating_carrier": {
          "name": "TEST TRAVELIAN",
          "iata": "PA",
          "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
          "created_at": "2025-05-07T17:41:55+05:00",
          "updated_at": "2026-11-19T21:23:07+05:00"
         },
         "departure_at": "2026-12-10T11:15:00+05:00",
         "arrival_at": "2026-12-10T13:00:00+05:00",
         "from": {
          "name": "TEST TRAVELIAN",
          "iata": "LHE",
          "city": {
           "name": "TEST TRAVELIAN",
           "code": "LHE",
           "created_at": "2025-04-30T07:39:19+05:00",
           "updated_at": "2025-10-01T18:26:44+05:00"
          },
          "country": {
           "name": "TEST TRAVELIAN",
           "alpha_2": "PK",
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
           "url": "https://api.sooperfare.com/pk",
           "created_at": "2025-04-30T02:39:15+05:00",
           "updated_at": "2026-04-05T12:23:54+05:00"
          },
          "created_at": "2025-04-30T03:21:44+05:00",
          "updated_at": "2026-11-19T21:15:06+05:00"
         },
         "to": {
          "name": "TEST TRAVELIAN",
          "iata": "KHI",
          "city": {
           "name": "TEST TRAVELIAN",
           "code": "KHI",
           "created_at": "2025-04-30T07:39:31+05:00",
           "updated_at": "2025-10-01T15:12:56+05:00"
          },
          "country": {
           "name": "TEST TRAVELIAN",
           "alpha_2": "PK",
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
           "url": "https://api.sooperfare.com/pk",
           "created_at": "2025-04-30T02:39:15+05:00",
           "updated_at": "2026-04-05T12:23:54+05:00"
          },
          "created_at": "2025-04-30T03:21:38+05:00",
          "updated_at": "2026-11-19T21:17:17+05:00"
         },
         "from_terminal": {
          "gate": ""
         },
         "to_terminal": {
          "gate": ""
         },
         "cabin_class": "economy",
         "flight_time": 105,
         "layover_time": 0,
         "flight_number": "PA401",
         "distance": 1021.43,
         "aircraft": {
          "manufacturer": "A321",
          "model": "A321",
          "created_at": "2024-12-23T18:03:44+05:00",
          "updated_at": "2024-12-23T18:03:44+05:00"
         }
        }
       ],
       "ancillaries": {
        "baggages": [],
        "meals": [],
        "seatplans": [],
        "ssrs": []
       }
      },
      "fare": {
       "ref_id": "34367e11-f47d-47c6-8fed-5d1a05cef955",
       "signature": "Value|EV|1",
       "is_refundable": true,
       "name": "TEST TRAVELIAN",
       "class": "EV",
       "name_class": "Value",
       "available_seats": 0,
       "passenger_fares": [
        {
         "traveler_type": "adult",
         "total_passenger": 1,
         "base_price": 13030,
         "surcharge": 0,
         "taxes": 2145,
         "fees": 8875,
         "service_charges": 0,
         "ancillaries_charges": 0,
         "total_price": 24050
        }
       ],
       "fare_policies": [
        {
         "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "title": "Value",
         "description": "<0 hours before departure",
         "type": "exchange",
         "price": 8000,
         "price_type": "fixed",
         "currency": {
          "name": "TEST TRAVELIAN",
          "code": "PKR",
          "symbol": "Rs",
          "decimal": 0,
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
          "created_at": "2025-04-30T08:20:52+05:00",
          "updated_at": "2025-04-30T08:20:52+05:00"
         },
         "traveler_type": "adult"
        },
        {
         "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "title": "Value",
         "description": "<48 hours before departure",
         "type": "exchange",
         "price": 6500,
         "price_type": "fixed",
         "currency": {
          "name": "TEST TRAVELIAN",
          "code": "PKR",
          "symbol": "Rs",
          "decimal": 0,
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
          "created_at": "2025-04-30T08:20:52+05:00",
          "updated_at": "2025-04-30T08:20:52+05:00"
         },
         "traveler_type": "adult"
        },
        {
         "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "title": "Value",
         "description": ">48 hours before departure",
         "type": "exchange",
         "price": 5500,
         "price_type": "fixed",
         "currency": {
          "name": "TEST TRAVELIAN",
          "code": "PKR",
          "symbol": "Rs",
          "decimal": 0,
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
          "created_at": "2025-04-30T08:20:52+05:00",
          "updated_at": "2025-04-30T08:20:52+05:00"
         },
         "traveler_type": "adult"
        }
       ],
       "baggage_policies": [
        {
         "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "title": "Value",
         "description": "1 hand bag(s), up to 7 kg",
         "weight_limit": 7,
         "weight_unit": "kg",
         "pieces": 1,
         "type": "carry",
         "traveler_type": "adult"
        },
        {
         "segment_ref_id": "66d1a024-6998-49d5-b663-98c691c90a75",
         "title": "Value",
         "description": "Checked baggage not included",
         "weight_limit": 0,
         "weight_unit": "kg",
         "pieces": 0,
         "type": "checked",
         "traveler_type": "adult"
        }
       ],
       "combinability_fares": [],
       "currency": {
        "name": "TEST TRAVELIAN",
        "code": "PKR",
        "symbol": "Rs",
        "decimal": 0,
        "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
        "created_at": "2025-04-30T08:20:52+05:00",
        "updated_at": "2025-04-30T08:20:52+05:00"
       },
       "base_price": 13030,
       "surcharge": 0,
       "taxes": 2145,
       "fees": 8875,
       "service_charges": 0,
       "ancillaries_charges": 0,
       "total_price": 24050,
       "total_discount": 0,
       "billable_price": 24050
      },
      "loyalty_program": {
       "name": "TEST TRAVELIAN",
       "regex": "^\\d{10}$",
       "created_at": "2025-10-09T22:45:39+05:00",
       "updated_at": "2025-10-09T22:45:39+05:00"
      }
     }
    ],
    "is_passport_required": true
   }
  ],
  "passengers": [
   {
    "ref_id": "6d27afef-a775-4470-919b-6811bb22217e",
    "type": "adult",
    "required_fields": [
     "title",
     "first_name",
     "last_name",
     "gender",
     "dob",
     "nationality",
     "nic"
    ],
    "ancillaries": {
     "baggages": [],
     "meals": [],
     "seatplans": [],
     "ssrs": []
    }
   }
  ],
  "settings": {
   "discount": "no",
   "upfront": "no",
   "service_charges": "no",
   "platform_fee": 20,
   "refund_service_fee": 600
  }
 }
}
```

### 4. `POST /v1/airline/reservation` at 2026-11-19T16:23:45Z (3935 ms, ok)

Request:

```json
{
 "ref_id": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
 "is_ancillary": false,
 "contact_info": {
  "first_name": "TEST",
  "last_name": "TRAVELIAN",
  "email_address": "<test email>",
  "phone_number": "<Travelian support number>",
  "phone_number_country": "pk"
 },
 "passengers": [
  {
   "ref_id": "6d27afef-a775-4470-919b-6811bb22217e",
   "type": "adult",
   "title": "mr",
   "first_name": "TEST",
   "last_name": "TRAVELIAN",
   "gender": "male",
   "dob": "1990-01-01",
   "nationality": "pk",
   "nic": "<dummy CNIC>"
  }
 ]
}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": {
  "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
  "service_type": "airline",
  "traveler_types": [
   "adult"
  ],
  "total_passengers": {
   "adult": 1
  },
  "order_ref_id": "spa-0rj",
  "trip_type": "one_way",
  "cabin_class": "economy",
  "trip_nature": "domestic",
  "name": "TEST TRAVELIAN",
  "email_address": "<test email>",
  "phone_number": "<Travelian support number>",
  "nic": null,
  "currency": {
   "name": "TEST TRAVELIAN",
   "code": "PKR",
   "symbol": "Rs",
   "decimal": 0,
   "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
   "created_at": "2025-04-30T08:20:52+05:00",
   "updated_at": "2025-04-30T08:20:52+05:00"
  },
  "base_price": "13030.00",
  "surcharge": "0.00",
  "fee": "8875.00",
  "taxes": "2145.00",
  "service_charges": "0.00",
  "ancillaries_charges": "0.00",
  "total_price": "24050.00",
  "discount": "0.00",
  "platform_fee": "20.00",
  "billable_price": "24070.00",
  "reservation_status": "active",
  "expiry_at": "2026-11-20T21:23:32+05:00",
  "providers": [
   {
    "pnr": "OFZOJG",
    "from_airport": {
     "name": "TEST TRAVELIAN",
     "iata": "LHE",
     "city": {
      "name": "TEST TRAVELIAN",
      "code": "LHE",
      "created_at": "2025-04-30T07:39:19+05:00",
      "updated_at": "2025-10-01T18:26:44+05:00"
     },
     "created_at": "2025-04-30T03:21:44+05:00",
     "updated_at": "2026-11-19T21:23:49+05:00"
    },
    "to_airport": {
     "name": "TEST TRAVELIAN",
     "iata": "KHI",
     "city": {
      "name": "TEST TRAVELIAN",
      "code": "KHI",
      "created_at": "2025-04-30T07:39:31+05:00",
      "updated_at": "2025-10-01T15:12:56+05:00"
     },
     "created_at": "2025-04-30T03:21:38+05:00",
     "updated_at": "2026-11-19T21:17:17+05:00"
    },
    "first_departure_at": "2026-12-10T11:15:00+05:00",
    "last_arrival_at": "2026-12-10T13:00:00+05:00",
    "cabin_class": "economy",
    "base_price": "13030.00",
    "surcharge": "0.00",
    "fee": "8875.00",
    "taxes": "2145.00",
    "service_charges": "0.00",
    "ancillaries_charges": "0.00",
    "total_price": "24050.00",
    "discount": "0.00",
    "billable_price": "24050.00",
    "expiry_at": "2026-11-20T21:24:32+05:00",
    "provider": {
     "identifier": "PA",
     "created_at": "2026-01-09T11:13:01+05:00",
     "updated_at": "2026-11-18T20:48:03+05:00"
    },
    "airline": {
     "name": "TEST TRAVELIAN",
     "iata": "PA",
     "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
     "created_at": "2025-05-07T17:41:55+05:00",
     "updated_at": "2026-11-19T21:23:07+05:00"
    },
    "legs": [
     {
      "name": "TEST TRAVELIAN",
      "class": "EV",
      "has_layovers": false,
      "airline": {
       "name": "TEST TRAVELIAN",
       "iata": "PA",
       "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
       "created_at": "2025-05-07T17:41:55+05:00",
       "updated_at": "2026-11-19T21:23:07+05:00"
      },
      "from_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "LHE",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "LHE",
        "created_at": "2025-04-30T07:39:19+05:00",
        "updated_at": "2025-10-01T18:26:44+05:00"
       },
       "created_at": "2025-04-30T03:21:44+05:00",
       "updated_at": "2026-11-19T21:23:49+05:00"
      },
      "to_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "KHI",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "KHI",
        "created_at": "2025-04-30T07:39:31+05:00",
        "updated_at": "2025-10-01T15:12:56+05:00"
       },
       "created_at": "2025-04-30T03:21:38+05:00",
       "updated_at": "2026-11-19T21:17:17+05:00"
      },
      "departure_at": "2026-12-10T11:15:00+05:00",
      "arrival_at": "2026-12-10T13:00:00+05:00",
      "segments": [
       {
        "airline_pnr": "OFZOJG",
        "flight_number": "PA401",
        "cabin_class": "economy",
        "status": "ontime",
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "flight_time": 105,
        "airline": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "aircraft": {
         "manufacturer": "Airbus",
         "model": "A321",
         "created_at": "2024-12-23T18:03:44+05:00",
         "updated_at": "2024-12-23T18:03:44+05:00"
        },
        "from_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "to_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "passengers": [
       {
        "ticket_number": null,
        "currency": {
         "name": "TEST TRAVELIAN",
         "code": "PKR",
         "symbol": "Rs",
         "decimal": 0,
         "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
         "created_at": "2025-04-30T08:20:52+05:00",
         "updated_at": "2025-04-30T08:20:52+05:00"
        },
        "base_price": "13030.00",
        "surcharge": "0.00",
        "fee": "8875.00",
        "taxes": "2145.00",
        "service_charges": "0.00",
        "ancillaries_charges": "0.00",
        "total_price": "24050.00",
        "status": "not_issued",
        "baggage_policies": [
         {
          "title": "Value",
          "description": "1 hand bag(s), up to 7 kg",
          "type": "carry",
          "pieces": 1,
          "weight_limit": 7,
          "weight_unit": "kg",
          "traveler_type": null,
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         },
         {
          "title": "Value",
          "description": "Checked baggage not included",
          "type": "checked",
          "pieces": 0,
          "weight_limit": 0,
          "weight_unit": "kg",
          "traveler_type": null,
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "fare_policies": [
         {
          "title": "Value",
          "description": "<0 hours before departure",
          "type": "exchange",
          "price": "8000.00",
          "price_type": "fixed",
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "traveler_type": null,
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         },
         {
          "title": "Value",
          "description": "<48 hours before departure",
          "type": "exchange",
          "price": "6500.00",
          "price_type": "fixed",
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "traveler_type": null,
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         },
         {
          "title": "Value",
          "description": ">48 hours before departure",
          "type": "exchange",
          "price": "5500.00",
          "price_type": "fixed",
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "traveler_type": null,
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "seats": [],
        "baggages": [],
        "meals": [],
        "ssrs": [],
        "passenger": {
         "title": "mr",
         "first_name": "TEST",
         "middle_name": "",
         "last_name": "TRAVELIAN",
         "gender": "male",
         "dob": "1990-01-01T00:00:00+05:00",
         "nic": "<dummy CNIC>",
         "passport_number": null,
         "passport_expiry": null,
         "traveler_type": "adult",
         "relation_type": null,
         "nationality": {
          "name": "TEST TRAVELIAN",
          "alpha_2": "PK",
          "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
          "url": "https://api.sooperfare.com/pk",
          "created_at": "2025-04-30T02:39:15+05:00",
          "updated_at": "2026-04-05T12:23:54+05:00"
         },
         "passport_country": null,
         "created_at": "2026-11-19T21:23:49+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "created_at": "2026-11-19T21:23:49+05:00",
      "updated_at": "2026-11-19T21:23:49+05:00"
     }
    ],
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   }
  ],
  "created_at": "2026-11-19T21:23:49+05:00",
  "updated_at": "2026-11-19T21:23:49+05:00"
 }
}
```

### 5. `POST /v1/booking/list` at 2026-11-19T16:23:49Z (964 ms, ok)

Request:

```json
{}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": [
  {
   "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
   "booking": {
    "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
    "service_type": "airline",
    "traveler_types": [
     "adult"
    ],
    "total_passengers": {
     "adult": 1
    },
    "order_ref_id": "spa-0rj",
    "trip_type": "one_way",
    "cabin_class": "economy",
    "trip_nature": "domestic",
    "name": "TEST TRAVELIAN",
    "email_address": "<test email>",
    "phone_number": "<Travelian support number>",
    "nic": null,
    "currency": {
     "name": "TEST TRAVELIAN",
     "code": "PKR",
     "symbol": "Rs",
     "decimal": 0,
     "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
     "created_at": "2025-04-30T08:20:52+05:00",
     "updated_at": "2025-04-30T08:20:52+05:00"
    },
    "base_price": "13030.00",
    "surcharge": "0.00",
    "fee": "8875.00",
    "taxes": "2145.00",
    "service_charges": "0.00",
    "ancillaries_charges": "0.00",
    "total_price": "24050.00",
    "discount": "0.00",
    "platform_fee": "20.00",
    "billable_price": "24070.00",
    "reservation_status": "active",
    "expiry_at": "2026-11-20T21:23:32+05:00",
    "providers": [
     {
      "pnr": "OFZOJG",
      "from_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "LHE",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "LHE",
        "created_at": "2025-04-30T07:39:19+05:00",
        "updated_at": "2025-10-01T18:26:44+05:00"
       },
       "created_at": "2025-04-30T03:21:44+05:00",
       "updated_at": "2026-11-19T21:23:49+05:00"
      },
      "to_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "KHI",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "KHI",
        "created_at": "2025-04-30T07:39:31+05:00",
        "updated_at": "2025-10-01T15:12:56+05:00"
       },
       "created_at": "2025-04-30T03:21:38+05:00",
       "updated_at": "2026-11-19T21:17:17+05:00"
      },
      "first_departure_at": "2026-12-10T11:15:00+05:00",
      "last_arrival_at": "2026-12-10T13:00:00+05:00",
      "cabin_class": "economy",
      "base_price": "13030.00",
      "surcharge": "0.00",
      "fee": "8875.00",
      "taxes": "2145.00",
      "service_charges": "0.00",
      "ancillaries_charges": "0.00",
      "total_price": "24050.00",
      "discount": "0.00",
      "billable_price": "24050.00",
      "expiry_at": "2026-11-20T21:24:32+05:00",
      "provider": {
       "identifier": "PA",
       "created_at": "2026-01-09T11:13:01+05:00",
       "updated_at": "2026-11-18T20:48:03+05:00"
      },
      "airline": {
       "name": "TEST TRAVELIAN",
       "iata": "PA",
       "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
       "created_at": "2025-05-07T17:41:55+05:00",
       "updated_at": "2026-11-19T21:23:07+05:00"
      },
      "legs": [
       {
        "name": "TEST TRAVELIAN",
        "class": "EV",
        "has_layovers": false,
        "airline": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "from_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "to_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "segments": [
         {
          "airline_pnr": "OFZOJG",
          "flight_number": "PA401",
          "cabin_class": "economy",
          "status": "ontime",
          "departure_at": "2026-12-10T11:15:00+05:00",
          "arrival_at": "2026-12-10T13:00:00+05:00",
          "flight_time": 105,
          "airline": {
           "name": "TEST TRAVELIAN",
           "iata": "PA",
           "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
           "created_at": "2025-05-07T17:41:55+05:00",
           "updated_at": "2026-11-19T21:23:07+05:00"
          },
          "aircraft": {
           "manufacturer": "Airbus",
           "model": "A321",
           "created_at": "2024-12-23T18:03:44+05:00",
           "updated_at": "2024-12-23T18:03:44+05:00"
          },
          "from_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "LHE",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "LHE",
            "created_at": "2025-04-30T07:39:19+05:00",
            "updated_at": "2025-10-01T18:26:44+05:00"
           },
           "created_at": "2025-04-30T03:21:44+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "to_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "KHI",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "KHI",
            "created_at": "2025-04-30T07:39:31+05:00",
            "updated_at": "2025-10-01T15:12:56+05:00"
           },
           "created_at": "2025-04-30T03:21:38+05:00",
           "updated_at": "2026-11-19T21:17:17+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "passengers": [
         {
          "ticket_number": null,
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "base_price": "13030.00",
          "surcharge": "0.00",
          "fee": "8875.00",
          "taxes": "2145.00",
          "service_charges": "0.00",
          "ancillaries_charges": "0.00",
          "total_price": "24050.00",
          "status": "not_issued",
          "baggage_policies": [
           {
            "title": "Value",
            "description": "1 hand bag(s), up to 7 kg",
            "type": "carry",
            "pieces": 1,
            "weight_limit": 7,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "Checked baggage not included",
            "type": "checked",
            "pieces": 0,
            "weight_limit": 0,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "fare_policies": [
           {
            "title": "Value",
            "description": "<0 hours before departure",
            "type": "exchange",
            "price": "8000.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "<48 hours before departure",
            "type": "exchange",
            "price": "6500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": ">48 hours before departure",
            "type": "exchange",
            "price": "5500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "seats": [],
          "baggages": [],
          "meals": [],
          "ssrs": [],
          "passenger": {
           "title": "mr",
           "first_name": "TEST",
           "middle_name": "",
           "last_name": "TRAVELIAN",
           "gender": "male",
           "dob": "1990-01-01T00:00:00+05:00",
           "nic": "<dummy CNIC>",
           "passport_number": null,
           "passport_expiry": null,
           "traveler_type": "adult",
           "relation_type": null,
           "nationality": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "passport_country": null,
           "created_at": "2026-11-19T21:23:49+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "created_at": "2026-11-19T21:23:49+05:00",
      "updated_at": "2026-11-19T21:23:49+05:00"
     }
    ],
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   },
   "additional": {
    "is_unpaid": true,
    "is_paid": false,
    "is_cancelled": false,
    "is_refunded": false,
    "is_refund_requested": false,
    "is_issued": false,
    "has_extras": true
   },
   "created_at": "2026-11-19T21:23:49+05:00",
   "updated_at": "2026-11-19T21:23:49+05:00"
  },
  "... 6 other bookings on the account"
 ]
}
```

### 6. `POST /v1/booking/cancel` at 2026-11-19T16:23:50Z (944 ms, ok)

Request:

```json
{
 "booking_ref_id": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
 "txn_ref_id": "TRAVELIAN-TEST-CANCEL",
 "amount": 24070.0,
 "currency": "PKR",
 "remarks": "Travelian integration test: cancel this unpaid test reservation"
}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": {
  "transaction": {
   "uuid": "b6be2769-9d71-42de-be13-e5cfc83b7c08",
   "txn_ref_id": "txn-016",
   "booking": {
    "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   },
   "description": "Cancel For Airline Booking.",
   "actual_price": "24070.00",
   "mdr": "0.00",
   "wallet_credits": "0.00",
   "discount": "0.00",
   "billable_price": "24070.00",
   "status": "unpaid",
   "type": "booking",
   "action_at": "2026-11-19T21:23:51+05:00",
   "currency": {
    "name": "TEST TRAVELIAN",
    "code": "PKR",
    "symbol": "Rs",
    "decimal": 0,
    "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
    "created_at": "2025-04-30T08:20:52+05:00",
    "updated_at": "2025-04-30T08:20:52+05:00"
   },
   "transaction": {
    "name": "TEST TRAVELIAN",
    "partner": {
     "name": "TEST TRAVELIAN",
     "email_address": "<test email>",
     "phone_number": "<Travelian support number>",
     "phone_number_country": "pk",
     "created_at": "2026-08-17T16:12:24+05:00",
     "updated_at": "2026-11-18T20:56:08+05:00"
    },
    "created_at": "2026-08-17T17:05:30+05:00",
    "updated_at": "2026-11-18T20:56:08+05:00"
   },
   "created_at": "2026-11-19T21:23:51+05:00",
   "updated_at": "2026-11-19T21:23:51+05:00"
  },
  "booking": {
   "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
   "booking": {
    "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
    "service_type": "airline",
    "traveler_types": [
     "adult"
    ],
    "total_passengers": {
     "adult": 1
    },
    "order_ref_id": "spa-0rj",
    "trip_type": "one_way",
    "cabin_class": "economy",
    "trip_nature": "domestic",
    "name": "TEST TRAVELIAN",
    "email_address": "<test email>",
    "phone_number": "<Travelian support number>",
    "nic": null,
    "currency": {
     "name": "TEST TRAVELIAN",
     "code": "PKR",
     "symbol": "Rs",
     "decimal": 0,
     "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
     "created_at": "2025-04-30T08:20:52+05:00",
     "updated_at": "2025-04-30T08:20:52+05:00"
    },
    "base_price": "13030.00",
    "surcharge": "0.00",
    "fee": "8875.00",
    "taxes": "2145.00",
    "service_charges": "0.00",
    "ancillaries_charges": "0.00",
    "total_price": "24050.00",
    "discount": "0.00",
    "platform_fee": "20.00",
    "billable_price": "24070.00",
    "reservation_status": "active",
    "expiry_at": "2026-11-20T21:23:32+05:00",
    "providers": [
     {
      "pnr": "OFZOJG",
      "from_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "LHE",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "LHE",
        "created_at": "2025-04-30T07:39:19+05:00",
        "updated_at": "2025-10-01T18:26:44+05:00"
       },
       "created_at": "2025-04-30T03:21:44+05:00",
       "updated_at": "2026-11-19T21:23:49+05:00"
      },
      "to_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "KHI",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "KHI",
        "created_at": "2025-04-30T07:39:31+05:00",
        "updated_at": "2025-10-01T15:12:56+05:00"
       },
       "created_at": "2025-04-30T03:21:38+05:00",
       "updated_at": "2026-11-19T21:17:17+05:00"
      },
      "first_departure_at": "2026-12-10T11:15:00+05:00",
      "last_arrival_at": "2026-12-10T13:00:00+05:00",
      "cabin_class": "economy",
      "base_price": "13030.00",
      "surcharge": "0.00",
      "fee": "8875.00",
      "taxes": "2145.00",
      "service_charges": "0.00",
      "ancillaries_charges": "0.00",
      "total_price": "24050.00",
      "discount": "0.00",
      "billable_price": "24050.00",
      "expiry_at": "2026-11-20T21:24:32+05:00",
      "provider": {
       "identifier": "PA",
       "created_at": "2026-01-09T11:13:01+05:00",
       "updated_at": "2026-11-18T20:48:03+05:00"
      },
      "airline": {
       "name": "TEST TRAVELIAN",
       "iata": "PA",
       "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
       "created_at": "2025-05-07T17:41:55+05:00",
       "updated_at": "2026-11-19T21:23:07+05:00"
      },
      "legs": [
       {
        "name": "TEST TRAVELIAN",
        "class": "EV",
        "has_layovers": false,
        "airline": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "from_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "to_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "segments": [
         {
          "airline_pnr": "OFZOJG",
          "flight_number": "PA401",
          "cabin_class": "economy",
          "status": "ontime",
          "departure_at": "2026-12-10T11:15:00+05:00",
          "arrival_at": "2026-12-10T13:00:00+05:00",
          "flight_time": 105,
          "airline": {
           "name": "TEST TRAVELIAN",
           "iata": "PA",
           "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
           "created_at": "2025-05-07T17:41:55+05:00",
           "updated_at": "2026-11-19T21:23:07+05:00"
          },
          "aircraft": {
           "manufacturer": "Airbus",
           "model": "A321",
           "created_at": "2024-12-23T18:03:44+05:00",
           "updated_at": "2024-12-23T18:03:44+05:00"
          },
          "from_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "LHE",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "LHE",
            "created_at": "2025-04-30T07:39:19+05:00",
            "updated_at": "2025-10-01T18:26:44+05:00"
           },
           "created_at": "2025-04-30T03:21:44+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "to_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "KHI",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "KHI",
            "created_at": "2025-04-30T07:39:31+05:00",
            "updated_at": "2025-10-01T15:12:56+05:00"
           },
           "created_at": "2025-04-30T03:21:38+05:00",
           "updated_at": "2026-11-19T21:17:17+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "passengers": [
         {
          "ticket_number": null,
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "base_price": "13030.00",
          "surcharge": "0.00",
          "fee": "8875.00",
          "taxes": "2145.00",
          "service_charges": "0.00",
          "ancillaries_charges": "0.00",
          "total_price": "24050.00",
          "status": "not_issued",
          "baggage_policies": [
           {
            "title": "Value",
            "description": "1 hand bag(s), up to 7 kg",
            "type": "carry",
            "pieces": 1,
            "weight_limit": 7,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "Checked baggage not included",
            "type": "checked",
            "pieces": 0,
            "weight_limit": 0,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "fare_policies": [
           {
            "title": "Value",
            "description": "<0 hours before departure",
            "type": "exchange",
            "price": "8000.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "<48 hours before departure",
            "type": "exchange",
            "price": "6500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": ">48 hours before departure",
            "type": "exchange",
            "price": "5500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "seats": [],
          "baggages": [],
          "meals": [],
          "ssrs": [],
          "passenger": {
           "title": "mr",
           "first_name": "TEST",
           "middle_name": "",
           "last_name": "TRAVELIAN",
           "gender": "male",
           "dob": "1990-01-01T00:00:00+05:00",
           "nic": "<dummy CNIC>",
           "passport_number": null,
           "passport_expiry": null,
           "traveler_type": "adult",
           "relation_type": null,
           "nationality": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "passport_country": null,
           "created_at": "2026-11-19T21:23:49+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "created_at": "2026-11-19T21:23:49+05:00",
      "updated_at": "2026-11-19T21:23:49+05:00"
     }
    ],
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   },
   "additional": {
    "is_unpaid": true,
    "is_paid": false,
    "is_cancelled": false,
    "is_refunded": false,
    "is_refund_requested": false,
    "is_issued": false,
    "has_extras": true
   },
   "created_at": "2026-11-19T21:23:49+05:00",
   "updated_at": "2026-11-19T21:23:49+05:00"
  },
  "message": null,
  "status": null
 }
}
```

### 7. `POST /v1/booking/list` at 2026-11-19T16:23:53Z (950 ms, ok)

Request:

```json
{}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": [
  {
   "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
   "booking": {
    "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
    "service_type": "airline",
    "traveler_types": [
     "adult"
    ],
    "total_passengers": {
     "adult": 1
    },
    "order_ref_id": "spa-0rj",
    "trip_type": "one_way",
    "cabin_class": "economy",
    "trip_nature": "domestic",
    "name": "TEST TRAVELIAN",
    "email_address": "<test email>",
    "phone_number": "<Travelian support number>",
    "nic": null,
    "currency": {
     "name": "TEST TRAVELIAN",
     "code": "PKR",
     "symbol": "Rs",
     "decimal": 0,
     "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
     "created_at": "2025-04-30T08:20:52+05:00",
     "updated_at": "2025-04-30T08:20:52+05:00"
    },
    "base_price": "13030.00",
    "surcharge": "0.00",
    "fee": "8875.00",
    "taxes": "2145.00",
    "service_charges": "0.00",
    "ancillaries_charges": "0.00",
    "total_price": "24050.00",
    "discount": "0.00",
    "platform_fee": "20.00",
    "billable_price": "24070.00",
    "reservation_status": "active",
    "expiry_at": "2026-11-20T21:23:32+05:00",
    "providers": [
     {
      "pnr": "OFZOJG",
      "from_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "LHE",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "LHE",
        "created_at": "2025-04-30T07:39:19+05:00",
        "updated_at": "2025-10-01T18:26:44+05:00"
       },
       "created_at": "2025-04-30T03:21:44+05:00",
       "updated_at": "2026-11-19T21:23:49+05:00"
      },
      "to_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "KHI",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "KHI",
        "created_at": "2025-04-30T07:39:31+05:00",
        "updated_at": "2025-10-01T15:12:56+05:00"
       },
       "created_at": "2025-04-30T03:21:38+05:00",
       "updated_at": "2026-11-19T21:17:17+05:00"
      },
      "first_departure_at": "2026-12-10T11:15:00+05:00",
      "last_arrival_at": "2026-12-10T13:00:00+05:00",
      "cabin_class": "economy",
      "base_price": "13030.00",
      "surcharge": "0.00",
      "fee": "8875.00",
      "taxes": "2145.00",
      "service_charges": "0.00",
      "ancillaries_charges": "0.00",
      "total_price": "24050.00",
      "discount": "0.00",
      "billable_price": "24050.00",
      "expiry_at": "2026-11-20T21:24:32+05:00",
      "provider": {
       "identifier": "PA",
       "created_at": "2026-01-09T11:13:01+05:00",
       "updated_at": "2026-11-18T20:48:03+05:00"
      },
      "airline": {
       "name": "TEST TRAVELIAN",
       "iata": "PA",
       "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
       "created_at": "2025-05-07T17:41:55+05:00",
       "updated_at": "2026-11-19T21:23:07+05:00"
      },
      "legs": [
       {
        "name": "TEST TRAVELIAN",
        "class": "EV",
        "has_layovers": false,
        "airline": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "from_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "to_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "segments": [
         {
          "airline_pnr": "OFZOJG",
          "flight_number": "PA401",
          "cabin_class": "economy",
          "status": "ontime",
          "departure_at": "2026-12-10T11:15:00+05:00",
          "arrival_at": "2026-12-10T13:00:00+05:00",
          "flight_time": 105,
          "airline": {
           "name": "TEST TRAVELIAN",
           "iata": "PA",
           "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
           "created_at": "2025-05-07T17:41:55+05:00",
           "updated_at": "2026-11-19T21:23:07+05:00"
          },
          "aircraft": {
           "manufacturer": "Airbus",
           "model": "A321",
           "created_at": "2024-12-23T18:03:44+05:00",
           "updated_at": "2024-12-23T18:03:44+05:00"
          },
          "from_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "LHE",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "LHE",
            "created_at": "2025-04-30T07:39:19+05:00",
            "updated_at": "2025-10-01T18:26:44+05:00"
           },
           "created_at": "2025-04-30T03:21:44+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "to_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "KHI",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "KHI",
            "created_at": "2025-04-30T07:39:31+05:00",
            "updated_at": "2025-10-01T15:12:56+05:00"
           },
           "created_at": "2025-04-30T03:21:38+05:00",
           "updated_at": "2026-11-19T21:17:17+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "passengers": [
         {
          "ticket_number": null,
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "base_price": "13030.00",
          "surcharge": "0.00",
          "fee": "8875.00",
          "taxes": "2145.00",
          "service_charges": "0.00",
          "ancillaries_charges": "0.00",
          "total_price": "24050.00",
          "status": "not_issued",
          "baggage_policies": [
           {
            "title": "Value",
            "description": "1 hand bag(s), up to 7 kg",
            "type": "carry",
            "pieces": 1,
            "weight_limit": 7,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "Checked baggage not included",
            "type": "checked",
            "pieces": 0,
            "weight_limit": 0,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "fare_policies": [
           {
            "title": "Value",
            "description": "<0 hours before departure",
            "type": "exchange",
            "price": "8000.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "<48 hours before departure",
            "type": "exchange",
            "price": "6500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": ">48 hours before departure",
            "type": "exchange",
            "price": "5500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "seats": [],
          "baggages": [],
          "meals": [],
          "ssrs": [],
          "passenger": {
           "title": "mr",
           "first_name": "TEST",
           "middle_name": "",
           "last_name": "TRAVELIAN",
           "gender": "male",
           "dob": "1990-01-01T00:00:00+05:00",
           "nic": "<dummy CNIC>",
           "passport_number": null,
           "passport_expiry": null,
           "traveler_type": "adult",
           "relation_type": null,
           "nationality": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "passport_country": null,
           "created_at": "2026-11-19T21:23:49+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "created_at": "2026-11-19T21:23:49+05:00",
      "updated_at": "2026-11-19T21:23:49+05:00"
     }
    ],
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   },
   "additional": {
    "is_unpaid": true,
    "is_paid": false,
    "is_cancelled": false,
    "is_refunded": false,
    "is_refund_requested": false,
    "is_issued": false,
    "has_extras": true
   },
   "created_at": "2026-11-19T21:23:49+05:00",
   "updated_at": "2026-11-19T21:23:49+05:00"
  },
  "... 6 other bookings on the account"
 ]
}
```

### 8. `POST /v1/booking/list` at 2026-11-19T16:24:02Z (1030 ms, ok)

Request:

```json
{}
```

Response:

```json
{
 "status": true,
 "message": "Operation Successfull",
 "type": "json",
 "data": [
  {
   "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
   "booking": {
    "uuid": "49c85ade-bdfe-48a9-b8f1-2424635b702c",
    "service_type": "airline",
    "traveler_types": [
     "adult"
    ],
    "total_passengers": {
     "adult": 1
    },
    "order_ref_id": "spa-0rj",
    "trip_type": "one_way",
    "cabin_class": "economy",
    "trip_nature": "domestic",
    "name": "TEST TRAVELIAN",
    "email_address": "<test email>",
    "phone_number": "<Travelian support number>",
    "nic": null,
    "currency": {
     "name": "TEST TRAVELIAN",
     "code": "PKR",
     "symbol": "Rs",
     "decimal": 0,
     "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
     "created_at": "2025-04-30T08:20:52+05:00",
     "updated_at": "2025-04-30T08:20:52+05:00"
    },
    "base_price": "13030.00",
    "surcharge": "0.00",
    "fee": "8875.00",
    "taxes": "2145.00",
    "service_charges": "0.00",
    "ancillaries_charges": "0.00",
    "total_price": "24050.00",
    "discount": "0.00",
    "platform_fee": "20.00",
    "billable_price": "24070.00",
    "reservation_status": "active",
    "expiry_at": "2026-11-20T21:23:32+05:00",
    "providers": [
     {
      "pnr": "OFZOJG",
      "from_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "LHE",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "LHE",
        "created_at": "2025-04-30T07:39:19+05:00",
        "updated_at": "2025-10-01T18:26:44+05:00"
       },
       "created_at": "2025-04-30T03:21:44+05:00",
       "updated_at": "2026-11-19T21:23:49+05:00"
      },
      "to_airport": {
       "name": "TEST TRAVELIAN",
       "iata": "KHI",
       "city": {
        "name": "TEST TRAVELIAN",
        "code": "KHI",
        "created_at": "2025-04-30T07:39:31+05:00",
        "updated_at": "2025-10-01T15:12:56+05:00"
       },
       "created_at": "2025-04-30T03:21:38+05:00",
       "updated_at": "2026-11-19T21:17:17+05:00"
      },
      "first_departure_at": "2026-12-10T11:15:00+05:00",
      "last_arrival_at": "2026-12-10T13:00:00+05:00",
      "cabin_class": "economy",
      "base_price": "13030.00",
      "surcharge": "0.00",
      "fee": "8875.00",
      "taxes": "2145.00",
      "service_charges": "0.00",
      "ancillaries_charges": "0.00",
      "total_price": "24050.00",
      "discount": "0.00",
      "billable_price": "24050.00",
      "expiry_at": "2026-11-20T21:24:32+05:00",
      "provider": {
       "identifier": "PA",
       "created_at": "2026-01-09T11:13:01+05:00",
       "updated_at": "2026-11-18T20:48:03+05:00"
      },
      "airline": {
       "name": "TEST TRAVELIAN",
       "iata": "PA",
       "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
       "created_at": "2025-05-07T17:41:55+05:00",
       "updated_at": "2026-11-19T21:23:07+05:00"
      },
      "legs": [
       {
        "name": "TEST TRAVELIAN",
        "class": "EV",
        "has_layovers": false,
        "airline": {
         "name": "TEST TRAVELIAN",
         "iata": "PA",
         "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
         "created_at": "2025-05-07T17:41:55+05:00",
         "updated_at": "2026-11-19T21:23:07+05:00"
        },
        "from_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "LHE",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "LHE",
          "created_at": "2025-04-30T07:39:19+05:00",
          "updated_at": "2025-10-01T18:26:44+05:00"
         },
         "created_at": "2025-04-30T03:21:44+05:00",
         "updated_at": "2026-11-19T21:23:49+05:00"
        },
        "to_airport": {
         "name": "TEST TRAVELIAN",
         "iata": "KHI",
         "city": {
          "name": "TEST TRAVELIAN",
          "code": "KHI",
          "created_at": "2025-04-30T07:39:31+05:00",
          "updated_at": "2025-10-01T15:12:56+05:00"
         },
         "created_at": "2025-04-30T03:21:38+05:00",
         "updated_at": "2026-11-19T21:17:17+05:00"
        },
        "departure_at": "2026-12-10T11:15:00+05:00",
        "arrival_at": "2026-12-10T13:00:00+05:00",
        "segments": [
         {
          "airline_pnr": "OFZOJG",
          "flight_number": "PA401",
          "cabin_class": "economy",
          "status": "ontime",
          "departure_at": "2026-12-10T11:15:00+05:00",
          "arrival_at": "2026-12-10T13:00:00+05:00",
          "flight_time": 105,
          "airline": {
           "name": "TEST TRAVELIAN",
           "iata": "PA",
           "logo": "https://cdn.sooperfare.com/assets/client/images/airlines/PA.png",
           "created_at": "2025-05-07T17:41:55+05:00",
           "updated_at": "2026-11-19T21:23:07+05:00"
          },
          "aircraft": {
           "manufacturer": "Airbus",
           "model": "A321",
           "created_at": "2024-12-23T18:03:44+05:00",
           "updated_at": "2024-12-23T18:03:44+05:00"
          },
          "from_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "LHE",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "LHE",
            "created_at": "2025-04-30T07:39:19+05:00",
            "updated_at": "2025-10-01T18:26:44+05:00"
           },
           "created_at": "2025-04-30T03:21:44+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "to_airport": {
           "name": "TEST TRAVELIAN",
           "iata": "KHI",
           "city": {
            "name": "TEST TRAVELIAN",
            "code": "KHI",
            "created_at": "2025-04-30T07:39:31+05:00",
            "updated_at": "2025-10-01T15:12:56+05:00"
           },
           "created_at": "2025-04-30T03:21:38+05:00",
           "updated_at": "2026-11-19T21:17:17+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "passengers": [
         {
          "ticket_number": null,
          "currency": {
           "name": "TEST TRAVELIAN",
           "code": "PKR",
           "symbol": "Rs",
           "decimal": 0,
           "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
           "created_at": "2025-04-30T08:20:52+05:00",
           "updated_at": "2025-04-30T08:20:52+05:00"
          },
          "base_price": "13030.00",
          "surcharge": "0.00",
          "fee": "8875.00",
          "taxes": "2145.00",
          "service_charges": "0.00",
          "ancillaries_charges": "0.00",
          "total_price": "24050.00",
          "status": "not_issued",
          "baggage_policies": [
           {
            "title": "Value",
            "description": "1 hand bag(s), up to 7 kg",
            "type": "carry",
            "pieces": 1,
            "weight_limit": 7,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "Checked baggage not included",
            "type": "checked",
            "pieces": 0,
            "weight_limit": 0,
            "weight_unit": "kg",
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "fare_policies": [
           {
            "title": "Value",
            "description": "<0 hours before departure",
            "type": "exchange",
            "price": "8000.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": "<48 hours before departure",
            "type": "exchange",
            "price": "6500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           },
           {
            "title": "Value",
            "description": ">48 hours before departure",
            "type": "exchange",
            "price": "5500.00",
            "price_type": "fixed",
            "currency": {
             "name": "TEST TRAVELIAN",
             "code": "PKR",
             "symbol": "Rs",
             "decimal": 0,
             "flag": "https://cdn.sooperfare.com/assets/client/images/flags/currency/PKR.png",
             "created_at": "2025-04-30T08:20:52+05:00",
             "updated_at": "2025-04-30T08:20:52+05:00"
            },
            "traveler_type": null,
            "created_at": "2026-11-19T21:23:49+05:00",
            "updated_at": "2026-11-19T21:23:49+05:00"
           }
          ],
          "seats": [],
          "baggages": [],
          "meals": [],
          "ssrs": [],
          "passenger": {
           "title": "mr",
           "first_name": "TEST",
           "middle_name": "",
           "last_name": "TRAVELIAN",
           "gender": "male",
           "dob": "1990-01-01T00:00:00+05:00",
           "nic": "<dummy CNIC>",
           "passport_number": null,
           "passport_expiry": null,
           "traveler_type": "adult",
           "relation_type": null,
           "nationality": {
            "name": "TEST TRAVELIAN",
            "alpha_2": "PK",
            "flag": "https://cdn.sooperfare.com/assets/client/images/flags/png/pk.png",
            "url": "https://api.sooperfare.com/pk",
            "created_at": "2025-04-30T02:39:15+05:00",
            "updated_at": "2026-04-05T12:23:54+05:00"
           },
           "passport_country": null,
           "created_at": "2026-11-19T21:23:49+05:00",
           "updated_at": "2026-11-19T21:23:49+05:00"
          },
          "created_at": "2026-11-19T21:23:49+05:00",
          "updated_at": "2026-11-19T21:23:49+05:00"
         }
        ],
        "created_at": "2026-11-19T21:23:49+05:00",
        "updated_at": "2026-11-19T21:23:49+05:00"
       }
      ],
      "created_at": "2026-11-19T21:23:49+05:00",
      "updated_at": "2026-11-19T21:23:49+05:00"
     }
    ],
    "created_at": "2026-11-19T21:23:49+05:00",
    "updated_at": "2026-11-19T21:23:49+05:00"
   },
   "additional": {
    "is_unpaid": true,
    "is_paid": false,
    "is_cancelled": false,
    "is_refunded": false,
    "is_refund_requested": false,
    "is_issued": false,
    "has_extras": true
   },
   "created_at": "2026-11-19T21:23:49+05:00",
   "updated_at": "2026-11-19T21:23:49+05:00"
  },
  "... 6 other bookings on the account"
 ]
}
```
