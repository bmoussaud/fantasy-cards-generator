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


def test_home_contains_htmx_button() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'hx-get="/partials/ping"' in response.text
    assert "Sign in with Microsoft Entra External ID to generate cards." in response.text
