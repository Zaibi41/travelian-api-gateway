"""The 48 RateHawk operations, as documented in docs/postman (Travelian — RateHawk Sandbox — Complete API,
from RateHawk's B2B v3 + Content v1 OpenAPI). One row each; nothing here is invented.

`send`:
- "json":  POST with a JSON body.
- "query": GET with the payload as JSON in `?data=` (RateHawk's GET convention).
- "none":  GET with no payload.

`writes`: the call changes supplier state (booking, cancel, order groups, profiles). The gateway only
sends these when RATEHAWK_WRITE_ENABLED is on, and never retries them.
`download`: the answer is a file (PDF), not JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Operation:
    key: str
    title: str
    group: str
    method: Literal["GET", "POST"]
    path: str
    send: Literal["json", "query", "none"]
    writes: bool = False
    download: bool = False


_B2B = "/api/b2b/v3"
_CONTENT = "/api/content/v1"

OPERATIONS: tuple[Operation, ...] = (
    # 01 Booking flow
    Operation("overview", "Check access", "booking", "GET", f"{_B2B}/overview/", "none"),
    Operation("hotel_info", "Hotel content", "booking", "POST", f"{_B2B}/hotel/info/", "json"),
    Operation("search_hotels", "Search hotels", "booking", "POST", f"{_B2B}/search/serp/hotels/", "json"),
    Operation("hotel_page", "Hotelpage", "booking", "POST", f"{_B2B}/search/hp/", "json"),
    Operation("prebook", "Prebook", "booking", "POST", f"{_B2B}/hotel/prebook/", "json"),
    Operation("booking_form", "Create booking process", "booking", "POST", f"{_B2B}/hotel/order/booking/form/", "json", writes=True),
    Operation("booking_finish", "Start booking", "booking", "POST", f"{_B2B}/hotel/order/booking/finish/", "json", writes=True),
    Operation("booking_status", "Check booking status", "booking", "POST", f"{_B2B}/hotel/order/booking/finish/status/", "json"),
    Operation("order_info", "Retrieve booking", "booking", "POST", f"{_B2B}/hotel/order/info/", "json"),
    Operation("voucher_download", "Download voucher", "booking", "GET", f"{_B2B}/hotel/order/document/voucher/download/", "query", download=True),
    Operation("order_cancel", "Cancel booking", "booking", "POST", f"{_B2B}/hotel/order/cancel/", "json", writes=True),
    # 02 Other search methods
    Operation("hotel_sort", "Sort hotels", "search", "POST", f"{_B2B}/search/hotelsort/", "json"),
    Operation("lookup_rate", "Retrieve rate info", "search", "POST", f"{_B2B}/search/lookuprate/", "json"),
    Operation("multicomplete", "Suggest hotels and regions", "search", "POST", f"{_B2B}/search/multicomplete/", "json"),
    Operation("search_geo", "Search by coordinates", "search", "POST", f"{_B2B}/search/serp/geo/", "json"),
    Operation("search_region", "Search by region", "search", "POST", f"{_B2B}/search/serp/region/", "json"),
    Operation("serp_prebook", "Prebook from search", "search", "POST", f"{_B2B}/serp/prebook/", "json"),
    # 03 Static data and content
    Operation("custom_dump", "Custom hotel dump", "content", "POST", f"{_B2B}/hotel/custom/dump/", "json"),
    Operation("incremental_reviews_dump", "Incremental reviews dump", "content", "POST", f"{_B2B}/hotel/incremental_reviews/dump/", "json"),
    Operation("hotel_dump", "Hotel dump", "content", "POST", f"{_B2B}/hotel/info/dump/", "json"),
    Operation("incremental_hotel_dump", "Incremental hotel dump", "content", "POST", f"{_B2B}/hotel/info/incremental_dump/", "json"),
    Operation("poi_dump", "Points of interest dump", "content", "POST", f"{_B2B}/hotel/poi/dump/", "json"),
    Operation("region_dump", "Regions dump", "content", "POST", f"{_B2B}/hotel/region/dump/", "json"),
    Operation("reviews_dump", "Hotel reviews dump", "content", "POST", f"{_B2B}/hotel/reviews/dump/", "json"),
    Operation("hotel_static", "Hotel static reference data", "content", "GET", f"{_B2B}/hotel/static/", "none"),
    Operation("content_filter_values", "Content filter values", "content", "GET", f"{_CONTENT}/filter_values/", "none"),
    Operation("content_by_ids", "Hotels content by IDs", "content", "POST", f"{_CONTENT}/hotel_content_by_ids/", "json"),
    Operation("content_ids_by_filter", "Hotel IDs by filter", "content", "POST", f"{_CONTENT}/hotel_ids_by_filter/", "json"),
    Operation("content_reviews_by_ids", "Hotel reviews by IDs", "content", "POST", f"{_CONTENT}/hotel_reviews_by_ids/", "json"),
    # 04 Contracts and documents
    Operation("contract_info", "Contract information", "documents", "GET", f"{_B2B}/general/contract/data/info/", "none"),
    Operation("closing_documents_download", "Download closing documents", "documents", "GET", f"{_B2B}/general/document/closing_documents/download/", "query", download=True),
    Operation("closing_documents_info", "Closing documents info", "documents", "GET", f"{_B2B}/general/document/closing_documents/info/", "query"),
    Operation("financial_info", "Financial information", "documents", "GET", f"{_B2B}/general/financial/info/", "none"),
    Operation("invoice_info_download", "Download booking invoice info", "documents", "GET", f"{_B2B}/hotel/order/document/info_invoice/download/", "query", download=True),
    Operation("single_act_download", "Download single act", "documents", "GET", f"{_B2B}/hotel/order/document/single_act/download/", "query", download=True),
    Operation("order_group_invoice_download", "Download order group invoice", "documents", "GET", f"{_B2B}/ordergroup/document/invoice/download/", "query", download=True),
    # 05 Order groups
    Operation("order_group_create", "Create order group", "order_groups", "GET", f"{_B2B}/ordergroup/create/", "query", writes=True),
    Operation("order_group_disband", "Disband order group", "order_groups", "GET", f"{_B2B}/ordergroup/disband/", "query", writes=True),
    Operation("order_group_info", "Retrieve order groups", "order_groups", "POST", f"{_B2B}/ordergroup/info/", "json"),
    Operation("order_group_add", "Add booking to group", "order_groups", "GET", f"{_B2B}/ordergroup/order/add/", "query", writes=True),
    Operation("order_group_remove", "Remove booking from group", "order_groups", "GET", f"{_B2B}/ordergroup/order/remove/", "query", writes=True),
    Operation("order_group_pay_overpay", "Pay order group overpay", "order_groups", "GET", f"{_B2B}/ordergroup/pay/overpay/", "query", writes=True),
    # 06 Profiles
    Operation("profile_create", "Create profile", "profiles", "POST", f"{_B2B}/profiles/create/", "json", writes=True),
    Operation("profile_list", "List profiles", "profiles", "GET", f"{_B2B}/profiles/list/", "none"),
    Operation("profile_edit", "Edit profile", "profiles", "POST", f"{_B2B}/profiles/edit/", "json", writes=True),
    Operation("profile_disable", "Disable profile", "profiles", "POST", f"{_B2B}/profiles/disable/", "json", writes=True),
    Operation("profile_restore", "Restore profile", "profiles", "POST", f"{_B2B}/profiles/restore/", "json", writes=True),
    Operation("profile_delete", "Delete profile", "profiles", "POST", f"{_B2B}/profiles/delete/", "json", writes=True),
)

BY_KEY: dict[str, Operation] = {operation.key: operation for operation in OPERATIONS}
