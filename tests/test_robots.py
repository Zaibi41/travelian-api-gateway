"""gateway#81: api.travelian.pk serves robots.txt (disallow all) and every response says X-Robots-Tag: noindex."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core.robots import NoIndexMiddleware
from app.main import app


@pytest.fixture
def client():
    with patch("app.main.load_parameter_store_settings"):
        with TestClient(app) as test_client:
            yield test_client


def test_robots_txt_disallows_everything(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response.text == "User-agent: *\nDisallow: /\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-robots-tag"] == "noindex"


def test_robots_txt_is_not_in_the_api_schema(client):
    assert "/robots.txt" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("path", ["/health", "/v1/health", "/v1/ready", "/docs", "/openapi.json"])
def test_responses_carry_noindex(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers.get_list("x-robots-tag") == ["noindex"]


def test_errors_carry_noindex(client):
    response = client.get("/v1/no-such-route")
    assert response.status_code == 404
    assert response.headers["x-robots-tag"] == "noindex"
    response = client.post("/v1/health")
    assert response.status_code == 405
    assert response.headers["x-robots-tag"] == "noindex"


def _cors_app() -> FastAPI:
    """Same middleware order as app.main: CORS first, then NoIndexMiddleware outermost."""
    test_app = FastAPI()
    test_app.add_middleware(CORSMiddleware, allow_origins=["https://travelian.pk"], allow_methods=["GET"],
                            allow_headers=["Authorization"], max_age=600)
    test_app.add_middleware(NoIndexMiddleware)

    @test_app.get("/v1/thing")
    async def thing():
        return {"ok": True}

    @test_app.get("/v1/stream")
    async def stream():
        return StreamingResponse(iter([b"a", b"b"]), media_type="text/plain",
                                 headers={"X-Robots-Tag": "index, follow"})

    return test_app


def test_cors_headers_are_kept():
    test_client = TestClient(_cors_app())
    response = test_client.get("/v1/thing", headers={"Origin": "https://travelian.pk"})
    assert response.headers["access-control-allow-origin"] == "https://travelian.pk"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["content-type"] == "application/json"
    preflight = test_client.options("/v1/thing", headers={"Origin": "https://travelian.pk",
                                                          "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://travelian.pk"
    assert preflight.headers["access-control-max-age"] == "600"
    assert preflight.headers["x-robots-tag"] == "noindex"
    refused = test_client.get("/v1/thing", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in refused.headers


def test_streaming_body_is_unchanged_and_header_is_not_duplicated():
    response = TestClient(_cors_app()).get("/v1/stream")
    assert response.text == "ab"
    assert response.headers.get_list("x-robots-tag") == ["noindex"]
