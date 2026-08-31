from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, base_url="https://testserver")


def test_healthz() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
