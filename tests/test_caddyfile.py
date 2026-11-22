"""deploy/Caddyfile: retired legacy Lambda routes answer 410 at Caddy and never reach the Lambda (gateway#82, B14).

Caddy is not installed in CI, so these tests read the Caddyfile and apply Caddy's documented `path` matcher rules
(exact or `*` prefix/suffix, case-insensitive, matched on the unescaped path with repeated slashes merged). After
deploying the file the owner confirms the real behaviour with the curl checks in the PR.
"""

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

CADDYFILE = Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile"


def _routes_block() -> str:
    text = CADDYFILE.read_text()
    start = text.index("(travelian_routes) {")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("travelian_routes snippet is not closed")


def _matchers(block: str) -> dict[str, list[str]]:
    found = {}
    for name, paths in re.findall(r"^\s*@(\w+)\s+path\s+(.+)$", block, flags=re.MULTILINE):
        found[name] = paths.split()
    return found


def _handle_blocks(block: str) -> list[tuple[str, str]]:
    """(matcher, body) for each top-level handle in the snippet, in file order; matcher '' is the catch-all."""
    handles = []
    for match in re.finditer(r"^\thandle(?:\s+(@\w+|/\S*))?\s*\{", block, flags=re.MULTILINE):
        depth, index = 0, match.end() - 1
        for index in range(match.end() - 1, len(block)):
            if block[index] == "{":
                depth += 1
            elif block[index] == "}":
                depth -= 1
                if depth == 0:
                    break
        handles.append((match.group(1) or "", block[match.end():index]))
    return handles


def _caddy_path_matches(pattern: str, request_path: str) -> bool:
    path = re.sub(r"/{2,}", "/", unquote(request_path)).lower()
    pattern = pattern.lower()
    if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 1:
        return pattern[1:-1] in path
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    if pattern.startswith("*"):
        return path.endswith(pattern[1:])
    return path == pattern


def _first_handle(request_path: str) -> str:
    """Which handle serves a path: Caddy runs the first handle (in order) whose matcher matches."""
    block = _routes_block()
    matchers = _matchers(block)
    for matcher, _body in _handle_blocks(block):
        if not matcher:
            return "legacy"
        patterns = matchers[matcher[1:]] if matcher.startswith("@") else [matcher]
        if any(_caddy_path_matches(pattern, request_path) for pattern in patterns):
            return matcher
    raise AssertionError("no catch-all handle")


BLOCKED_CONTENT = [
    "/content/bookings",
    "/content/bookings/",
    "/content/bookings/TRV-123",
    "/content/bookings/TRV-123/",
    "/content/bookings%2F",
    "/content/bookings%2FTRV-123",
    "//content/bookings",
    "/content//bookings/TRV-123",
    "/Content/Bookings",
    "/CONTENT/BOOKING-REQUESTS",
    "/content/booking-requests",
    "/content/booking-requests/",
    "/content/booking-requests/req-1",
]

# Still served by the legacy Lambda (other screens and the legacy search) or by the gateway.
NOT_BLOCKED = [
    "/content/blog-posts",
    "/content/blog-posts/some-slug",
    "/content/packages",
    "/content/hotels",
    "/content/tickets",
    "/content/pricing-rules",
    "/content/notification-settings",
    "/content/flight-search-logs",
    "/content/group-ticketing-collections/public",
    "/flights/search",
    "/hotels/search",
    "/health",
]


@pytest.mark.parametrize("request_path", BLOCKED_CONTENT)
def test_legacy_content_bookings_are_retired(request_path):
    assert _first_handle(request_path) == "@retired_content"


@pytest.mark.parametrize("request_path", NOT_BLOCKED)
def test_other_legacy_routes_still_reach_the_lambda(request_path):
    assert _first_handle(request_path) == "legacy"


@pytest.mark.parametrize("request_path", ["/v1/admin/bookings", "/v1/admin/bookings/abc", "/v1/public/bookings/TRV-1"])
def test_gateway_bookings_are_not_affected(request_path):
    assert _first_handle(request_path) == "/v1/*"


@pytest.mark.parametrize("request_path", ["/payments/payfast/session", "/payments/payfast/callback",
                                          "/group-ticketing", "/group-ticketing/groups"])
def test_earlier_retired_routes_stay_retired(request_path):
    assert _first_handle(request_path) == "@retired"


def test_retired_content_answers_410_json_without_proxying():
    body = dict(_handle_blocks(_routes_block()))["@retired_content"]
    assert "reverse_proxy" not in body
    match = re.search(r"respond\s+`([^`]*)`\s+(\d{3})", body)
    assert match, "the retired handle must answer itself"
    assert match.group(2) == "410"
    assert json.loads(match.group(1))["error"] == "GONE"
    assert "header Content-Type application/json" in body


def test_retired_handles_come_before_the_legacy_catch_all():
    order = [matcher for matcher, _body in _handle_blocks(_routes_block())]
    assert order[-1] == ""
    assert order.index("@retired_content") < order.index("")


def test_both_sites_use_the_same_routes():
    text = CADDYFILE.read_text()
    for site in ("api.travelian.pk {", "gw-test.travelian.pk {"):
        site_block = text[text.index(site):]
        assert "import travelian_routes" in site_block.split("}", 1)[0]


# gateway#81: robots.txt comes from the gateway, not the legacy Lambda; every response gets X-Robots-Tag: noindex.
@pytest.mark.parametrize("request_path", ["/robots.txt", "/ROBOTS.TXT", "//robots.txt"])
def test_robots_txt_reaches_the_gateway(request_path):
    assert _first_handle(request_path) == "/robots.txt"
    body = dict(_handle_blocks(_routes_block()))["/robots.txt"]
    assert "reverse_proxy {$GATEWAY_UPSTREAM:127.0.0.1:8000}" in body


@pytest.mark.parametrize("request_path", ["/robots.txt.bak", "/robots", "/content/robots.txt"])
def test_only_the_exact_robots_path_moves(request_path):
    assert _first_handle(request_path) == "legacy"


def test_noindex_header_on_every_route():
    block = _routes_block()
    # Snippet level (outside any handle), so the legacy Lambda and 410 answers get it too. `?` sets it only when
    # the upstream did not, so gateway responses keep exactly one value.
    top_level = re.sub(r"\thandle[^\n]*\{.*?\n\t\}", "", block, flags=re.DOTALL)
    assert re.search(r"^\theader \?X-Robots-Tag noindex$", top_level, flags=re.MULTILINE)
