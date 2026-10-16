import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import TypeVar

from app.core.config import settings
from app.schemas.flights import VendorIssue, VendorResult

T = TypeVar("T")
logger = logging.getLogger(__name__)


async def gather_vendor_calls(
    calls: list[tuple[str, Callable[[], Awaitable[T]]]],
    *,
    empty_response_code: str = "empty_response",
    allow_empty_result: bool = False,
) -> tuple[dict[str, T], list[VendorIssue], list[VendorResult]]:
    """Run independent vendor calls concurrently and retain an outcome for each."""
    semaphore = asyncio.Semaphore(settings.vendor_max_concurrency)

    async def invoke(name: str, call: Callable[[], Awaitable[T]]):
        started = perf_counter()
        try:
            async with semaphore:
                result = await asyncio.wait_for(call(), timeout=settings.vendor_timeout_seconds)
            if result is None or (result == [] and not allow_empty_result):
                return name, None, "failed", empty_response_code, int((perf_counter() - started) * 1000)
            return name, result, "success", None, int((perf_counter() - started) * 1000)
        except TimeoutError:
            return name, None, "timeout", "timeout", int((perf_counter() - started) * 1000)
        except Exception as exc:
            error_code = getattr(exc, "error_code", "upstream_error")
            public_message = getattr(exc, "public_message", "Supplier request failed.")
            logger.warning(
                "Vendor operation failed",
                extra={"vendor": name, "exception_type": type(exc).__name__, "error_code": error_code},
            )
            return name, None, "failed", error_code, int((perf_counter() - started) * 1000), public_message

    outcomes = await asyncio.gather(*(invoke(name, call) for name, call in calls))
    values: dict[str, T] = {}
    issues: list[VendorIssue] = []
    results: list[VendorResult] = []
    for outcome in outcomes:
        name, value, status, code, duration_ms = outcome[:5]
        if status == "success":
            values[name] = value
            count = len(value) if hasattr(value, "__len__") else 1
            results.append(VendorResult(vendor=name, status="success", duration_ms=duration_ms, offer_count=count))
        else:
            message = outcome[5] if len(outcome) > 5 else {
                "timeout": "Supplier did not respond before the timeout.",
                "empty_response": "Supplier returned no records.",
                "upstream_error": "Supplier request failed.",
            }.get(code, "Supplier request failed.")
            issues.append(VendorIssue(vendor=name, code=code or "upstream_error", message=message))
            results.append(
                VendorResult(
                    vendor=name,
                    status=status,
                    duration_ms=duration_ms,
                    offer_count=0,
                    error_code=code,
                )
            )
    return values, issues, results
