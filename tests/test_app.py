from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_contains_htmx_button() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'hx-get="/partials/ping"' in response.text
