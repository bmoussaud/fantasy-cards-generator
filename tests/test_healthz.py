from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from threading import Event
from typing import Any
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from fastapi.testclient import TestClient

from app.generation import (
    AppServices,
    AzureBlobAssetStore,
    AzureCosmosCardRepository,
    create_services,
)
from app.health import AzureBlobHealthProbe, AzureCosmosHealthProbe, DependencyHealthResult
from app.main import create_app
from app.settings import load_app_settings


class StaticProbe:
    def __init__(self, result: DependencyHealthResult) -> None:
        self.name = result.name
        self.result = result

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        del timeout_seconds
        return self.result


class PendingTimeoutProbe:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        start = time.perf_counter()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return DependencyHealthResult(
                name=self.name,  # type: ignore[arg-type]
                status="timeout",
                duration_ms=max(0, round((time.perf_counter() - start) * 1000)),
                error_category="timeout",
            )
        raise AssertionError("probe unexpectedly completed")


class RaisingProbe:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        del timeout_seconds
        raise RuntimeError(self.message)


class CoordinatedProbe:
    def __init__(self, name: str, started: Event, peer_started: Event) -> None:
        self.name = name
        self.started = started
        self.peer_started = peer_started

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        start = time.perf_counter()
        self.started.set()
        deadline = time.perf_counter() + timeout_seconds
        while not self.peer_started.is_set():
            if time.perf_counter() >= deadline:
                return DependencyHealthResult(
                    name=self.name,  # type: ignore[arg-type]
                    status="timeout",
                    duration_ms=max(0, round((time.perf_counter() - start) * 1000)),
                    error_category="timeout",
                )
            await asyncio.sleep(0)
        return DependencyHealthResult(
            name=self.name,  # type: ignore[arg-type]
            status="ok",
            duration_ms=max(0, round((time.perf_counter() - start) * 1000)),
        )


def make_client(
    *,
    persistence_mode: str = "memory",
    cosmos_probe: Any = None,
    blob_probe: Any = None,
    cosmos_timeout_ms: int = 25,
    blob_timeout_ms: int = 25,
) -> TestClient:
    base_settings = load_app_settings()
    defaults = create_services(base_settings)
    settings = replace(
        base_settings,
        persistence_mode=persistence_mode,  # type: ignore[arg-type]
        healthz_cosmos_timeout_ms=cosmos_timeout_ms,
        healthz_blob_timeout_ms=blob_timeout_ms,
    )
    services = AppServices(
        settings=settings,
        card_repository=defaults.card_repository,
        audit_repository=defaults.audit_repository,
        asset_store=defaults.asset_store,
        ai_client=defaults.ai_client,
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
        cosmos_health_probe=cosmos_probe,
        blob_health_probe=blob_probe,
    )
    return TestClient(create_app(services=services), base_url="https://testserver")


def test_healthy_dependencies_return_200() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(DependencyHealthResult("cosmos", "ok", 3)),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "ok", 4)),
    )

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ok",
        "dependencies": {
            "cosmos": {"status": "ok", "durationMs": 3, "errorCategory": "none"},
            "blob": {"status": "ok", "durationMs": 4, "errorCategory": "none"},
        },
    }


@pytest.mark.parametrize("status_name", ["unavailable", "unauthorized"])
def test_cosmos_failure_statuses_return_503(status_name: str) -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(
            DependencyHealthResult("cosmos", status_name, 2, status_name)  # type: ignore[arg-type]
        ),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "ok", 1)),
    )

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dependencies"]["cosmos"] == {
        "status": status_name,
        "durationMs": 2,
        "errorCategory": status_name,
    }


def test_cosmos_timeout_returns_503() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=PendingTimeoutProbe("cosmos"),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "ok", 1)),
        cosmos_timeout_ms=10,
    )

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dependencies"]["cosmos"]["status"] == "timeout"
    assert response.json()["dependencies"]["cosmos"]["errorCategory"] == "timeout"


@pytest.mark.parametrize("status_name", ["unavailable", "unauthorized"])
def test_blob_failure_statuses_return_503(status_name: str) -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(DependencyHealthResult("cosmos", "ok", 1)),
        blob_probe=StaticProbe(
            DependencyHealthResult("blob", status_name, 2, status_name)  # type: ignore[arg-type]
        ),
    )

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dependencies"]["blob"] == {
        "status": status_name,
        "durationMs": 2,
        "errorCategory": status_name,
    }


def test_blob_timeout_returns_503() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(DependencyHealthResult("cosmos", "ok", 1)),
        blob_probe=PendingTimeoutProbe("blob"),
        blob_timeout_ms=10,
    )

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dependencies"]["blob"]["status"] == "timeout"
    assert response.json()["dependencies"]["blob"]["errorCategory"] == "timeout"


def test_combined_failure_response_and_telemetry_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry_calls: list[tuple[str, dict[str, Any] | None, str | None]] = []

    def capture_safe_log(
        name: str,
        *,
        request_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        **_: object,
    ) -> None:
        telemetry_calls.append((name, attributes, request_id))

    monkeypatch.setattr("app.health.safe_log", capture_safe_log)
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=RaisingProbe(
            "cosmos",
            (
                "https://secret.documents.azure.com cards card-assets "
                "tenant=11111111-1111-1111-1111-111111111111"
            ),
        ),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "unauthorized", 4, "unauthorized")),
    )

    response = client.get("/healthz")
    serialized_telemetry = repr(telemetry_calls)

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["dependencies"]["cosmos"] == {
        "status": "unavailable",
        "durationMs": 0,
        "errorCategory": "unavailable",
    }
    for forbidden in [
        "secret.documents.azure.com",
        "cards",
        "card-assets",
        "tenant=",
        "RuntimeError",
    ]:
        assert forbidden not in response.text
        assert forbidden not in serialized_telemetry


def test_missing_configuration_returns_misconfigured_without_contacting_azure() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=AzureCosmosHealthProbe(
            get_container_client=lambda: _unexpected_async_call(),
            endpoint=None,
            database_name="appdb",
            container_name="cards",
        ),
        blob_probe=AzureBlobHealthProbe(
            get_container_client=lambda: _unexpected_sync_call(),
            endpoint=None,
            container_name="card-assets",
        ),
    )

    response = client.get("/healthz")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["dependencies"]["cosmos"]["status"] == "misconfigured"
    assert body["dependencies"]["cosmos"]["errorCategory"] == "misconfigured"
    assert body["dependencies"]["blob"]["status"] == "misconfigured"
    assert body["dependencies"]["blob"]["errorCategory"] == "misconfigured"


def test_memory_mode_reports_not_applicable_without_instantiating_azure_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_cosmos_init(self, settings: Any) -> None:
        raise AssertionError("AzureCosmosCardRepository should not be constructed in memory mode")

    def fail_blob_init(self, settings: Any) -> None:
        raise AssertionError("AzureBlobAssetStore should not be constructed in memory mode")

    monkeypatch.setattr(AzureCosmosCardRepository, "__init__", fail_cosmos_init)
    monkeypatch.setattr(AzureBlobAssetStore, "__init__", fail_blob_init)
    settings = replace(load_app_settings(), persistence_mode="memory")
    services = create_services(settings)
    client = TestClient(create_app(services=services), base_url="https://testserver")

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["dependencies"] == {
        "cosmos": {
            "status": "not_applicable",
            "durationMs": 0,
            "errorCategory": "none",
        },
        "blob": {
            "status": "not_applicable",
            "durationMs": 0,
            "errorCategory": "none",
        },
    }


def test_healthz_honors_and_generates_request_ids() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(DependencyHealthResult("cosmos", "ok", 1)),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "ok", 1)),
    )

    supplied = client.get("/healthz", headers={"X-Request-ID": "healthz-request-123"})
    generated = client.get("/healthz")

    assert supplied.headers["x-request-id"] == "healthz-request-123"
    assert generated.headers["x-request-id"]
    assert generated.headers["x-request-id"] != "healthz-request-123"


def test_healthz_runs_probes_concurrently_and_finishes_within_bound() -> None:
    cosmos_started = Event()
    blob_started = Event()
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=CoordinatedProbe("cosmos", cosmos_started, blob_started),
        blob_probe=CoordinatedProbe("blob", blob_started, cosmos_started),
        cosmos_timeout_ms=50,
        blob_timeout_ms=50,
    )

    start = time.perf_counter()
    response = client.get("/healthz")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 0.2
    assert cosmos_started.is_set()
    assert blob_started.is_set()


def test_only_existing_healthz_route_is_registered() -> None:
    client = make_client(
        persistence_mode="azure",
        cosmos_probe=StaticProbe(DependencyHealthResult("cosmos", "ok", 1)),
        blob_probe=StaticProbe(DependencyHealthResult("blob", "ok", 1)),
    )
    paths = [route.path for route in client.app.routes if hasattr(route, "path")]

    assert paths.count("/healthz") == 1
    assert client.get("/health").status_code == 404


def test_cosmos_probe_classifies_service_request_error_as_unavailable() -> None:
    container = AsyncMock()
    container.read.side_effect = ServiceRequestError("https://cosmos.example.invalid")
    probe = AzureCosmosHealthProbe(
        get_container_client=AsyncMock(return_value=container),
        endpoint="https://cosmos.example.invalid",
        database_name="appdb",
        container_name="cards",
    )

    result = asyncio.run(probe.check(timeout_seconds=0.01))

    assert result.status == "unavailable"
    assert result.error_category == "unavailable"


def test_blob_probe_classifies_auth_error_as_unauthorized() -> None:
    container = AsyncMock()
    container.get_container_properties.side_effect = ClientAuthenticationError(
        message="principal=00000000-0000-0000-0000-000000000000"
    )
    probe = AzureBlobHealthProbe(
        get_container_client=lambda: container,
        endpoint="https://blob.example.invalid",
        container_name="card-assets",
    )

    result = asyncio.run(probe.check(timeout_seconds=0.01))

    assert result.status == "unauthorized"
    assert result.error_category == "unauthorized"


async def _unexpected_async_call() -> Any:
    raise AssertionError("Azure dependency probe should not contact Azure when misconfigured")


def _unexpected_sync_call() -> Any:
    raise AssertionError("Azure dependency probe should not contact Azure when misconfigured")
