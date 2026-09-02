import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, base_url="https://testserver")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def test_healthz() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert REQUEST_ID_PATTERN.fullmatch(response.headers["x-request-id"])


def test_valid_request_id_is_echoed() -> None:
    request_id = "client-request_42.trace"

    response = client.get("/healthz", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize(
    "request_id",
    [
        "contains spaces",
        "contains/slash",
        "contains?query=secret",
        "x" * 65,
    ],
)
def test_unsafe_request_id_is_replaced(request_id: str) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": request_id})

    replacement = response.headers["x-request-id"]
    assert response.status_code == 200
    assert replacement != request_id
    assert REQUEST_ID_PATTERN.fullmatch(replacement)


def test_home_renders_hero_and_preserves_auth_copy() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Sign in with Microsoft Entra External ID to generate cards." in response.text
    assert "Check HTMX wiring" not in response.text
    assert 'hx-get="/partials/ping"' not in response.text
    assert 'href="/static/css/app.css"' in response.text
    assert 'src="/static/js/app.js"' in response.text


def test_ping_partial_still_serves_htmx_check() -> None:
    response = client.get("/partials/ping")

    assert response.status_code == 200
    assert "HTMX is wired." in response.text


def test_static_stylesheet_is_mounted_and_served() -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert "--color-accent" in response.text


def test_card_preview_styles_preserve_generated_image_aspect_ratio() -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert ".card-portrait.is-broken {\n  aspect-ratio: 4 / 3;" in response.text
    assert ".card-portrait img {\n  width: 100%;\n  height: auto;" in response.text
    assert "object-fit: cover;" not in response.text
