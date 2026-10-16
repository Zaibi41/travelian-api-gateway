import asyncio
import logging
from time import perf_counter
from uuid import uuid4

from app.core.config import settings
from app.schemas.flights import (
    FlightSearchRequest,
    FlightSearchResponse,
    VendorIssue,
    VendorResult,
)
from app.vendors.base import FlightVendor

logger = logging.getLogger(__name__)


class FlightSearchService:
    def __init__(self, vendors: list[FlightVendor]) -> None:
        self.vendors = vendors

    async def search(self, request: FlightSearchRequest) -> FlightSearchResponse:
        tasks = [
            asyncio.create_task(self._search_vendor(vendor, request), name=f"vendor:{vendor.name}")
            for vendor in self.vendors
        ]
        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        offers = []
        succeeded = []
        failed = []
        vendor_results = []
        for vendor, result in zip(self.vendors, results):
            if isinstance(result, _VendorCallFailure):
                logger.warning(
                    "Flight vendor call failed",
                    extra={"vendor": vendor.name, "error_code": result.code},
                )
                failed.append(
                    VendorIssue(
                        vendor=vendor.name,
                        code=result.code,
                        message=result.message,
                    )
                )
                vendor_results.append(
                    VendorResult(
                        vendor=vendor.name,
                        status="timeout" if result.code == "timeout" else "failed",
                        duration_ms=result.duration_ms,
                        error_code=result.code,
                    )
                )
            elif isinstance(result, Exception):
                # Defensive catch for errors escaping the vendor wrapper.
                logger.exception("Unexpected flight vendor error", extra={"vendor": vendor.name})
                failed.append(VendorIssue(vendor=vendor.name, code="internal_error", message="Supplier search failed."))
                vendor_results.append(VendorResult(vendor=vendor.name, status="failed", duration_ms=0, error_code="internal_error"))
            elif result.offers:
                offers.extend(result.offers)
                succeeded.append(vendor.name)
                vendor_results.append(
                    VendorResult(
                        vendor=vendor.name,
                        status="success",
                        duration_ms=result.duration_ms,
                        offer_count=len(result.offers),
                    )
                )
            else:
                failed.append(VendorIssue(vendor=vendor.name, code="empty_response", message="Supplier returned no offers."))
                vendor_results.append(
                    VendorResult(
                        vendor=vendor.name,
                        status="failed",
                        duration_ms=result.duration_ms,
                        offer_count=0,
                        error_code="empty_response",
                    )
                )

        offers.sort(key=lambda offer: (offer.price, offer.segments[0].departure_at))
        status = "complete" if not failed else "partial" if succeeded else "unavailable"
        return FlightSearchResponse(
            search_id=str(uuid4()),
            status=status,
            offers=offers,
            vendors_queried=[vendor.name for vendor in self.vendors],
            vendors_succeeded=succeeded,
            vendors_failed=failed,
            vendor_results=vendor_results,
        )

    @staticmethod
    async def _search_vendor(vendor: FlightVendor, request: FlightSearchRequest) -> "_VendorCallSuccess | _VendorCallFailure":
        started = perf_counter()
        try:
            offers = await asyncio.wait_for(
                vendor.search(request), timeout=settings.vendor_timeout_seconds
            )
            return _VendorCallSuccess(offers=offers, duration_ms=int((perf_counter() - started) * 1000))
        except TimeoutError:
            return _VendorCallFailure("timeout", "Supplier did not respond before the timeout.", int((perf_counter() - started) * 1000))
        except Exception as exc:
            # Log exception detail internally; never expose upstream payloads or secrets to clients.
            logger.warning("Vendor adapter raised an exception", extra={"vendor": vendor.name, "exception_type": type(exc).__name__})
            return _VendorCallFailure("upstream_error", "Supplier search failed.", int((perf_counter() - started) * 1000))


class _VendorCallSuccess:
    def __init__(self, offers, duration_ms: int) -> None:
        self.offers = offers
        self.duration_ms = duration_ms


class _VendorCallFailure:
    def __init__(self, code: str, message: str, duration_ms: int) -> None:
        self.code = code
        self.message = message
        self.duration_ms = duration_ms
