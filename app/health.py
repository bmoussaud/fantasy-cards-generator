from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)

from app.telemetry import safe_log

DependencyStatus = Literal[
    "ok",
    "unavailable",
    "unauthorized",
    "misconfigured",
    "timeout",
    "not_applicable",
]
DependencyErrorCategory = Literal["none", "unavailable", "unauthorized", "misconfigured", "timeout"]
HEALTHY_DEPENDENCY_STATUSES = {"ok", "not_applicable"}


@dataclass(frozen=True, slots=True)
class DependencyHealthResult:
    name: Literal["cosmos", "blob"]
    status: DependencyStatus
    duration_ms: int
    error_category: DependencyErrorCategory = "none"

    def as_dict(self) -> dict[str, str | int]:
        return {
            "status": self.status,
            "durationMs": self.duration_ms,
            "errorCategory": self.error_category,
        }


class HealthDependencyProbe(Protocol):
    name: Literal["cosmos", "blob"]

    async def check(self, timeout_seconds: float) -> DependencyHealthResult: ...


@dataclass(frozen=True, slots=True)
class NotApplicableHealthProbe:
    name: Literal["cosmos", "blob"]

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        del timeout_seconds
        return DependencyHealthResult(
            name=self.name,
            status="not_applicable",
            duration_ms=0,
        )


@dataclass(frozen=True, slots=True)
class AzureCosmosHealthProbe:
    get_container_client: Callable[[], Awaitable[Any]]
    endpoint: str | None
    database_name: str | None
    container_name: str | None
    name: Literal["cosmos"] = "cosmos"

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        start = time.perf_counter()
        if not self.endpoint or not self.database_name or not self.container_name:
            return _result(
                name=self.name,
                status="misconfigured",
                start=start,
                error_category="misconfigured",
            )

        try:
            await asyncio.wait_for(self._read_container_metadata(), timeout=timeout_seconds)
        except Exception as exc:
            return _result(
                name=self.name,
                status=_classify_status(exc),
                start=start,
                error_category=_classify_error_category(exc),
            )

        return _result(name=self.name, status="ok", start=start)

    async def _read_container_metadata(self) -> None:
        container = await self.get_container_client()
        await container.read()


@dataclass(frozen=True, slots=True)
class AzureBlobHealthProbe:
    get_container_client: Callable[[], Any]
    endpoint: str | None
    container_name: str | None
    name: Literal["blob"] = "blob"

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        start = time.perf_counter()
        if not self.endpoint or not self.container_name:
            return _result(
                name=self.name,
                status="misconfigured",
                start=start,
                error_category="misconfigured",
            )

        try:
            await asyncio.wait_for(self._read_container_metadata(), timeout=timeout_seconds)
        except Exception as exc:
            return _result(
                name=self.name,
                status=_classify_status(exc),
                start=start,
                error_category=_classify_error_category(exc),
            )

        return _result(name=self.name, status="ok", start=start)

    async def _read_container_metadata(self) -> None:
        container = self.get_container_client()
        await container.get_container_properties()


async def run_dependency_probes(
    *,
    probes: Sequence[tuple[HealthDependencyProbe, float]],
    request_id: str | None,
) -> dict[str, DependencyHealthResult]:
    results = await asyncio.gather(
        *[
            _run_dependency_probe(
                probe=probe,
                timeout_seconds=timeout_seconds,
                request_id=request_id,
            )
            for probe, timeout_seconds in probes
        ]
    )
    return {result.name: result for result in results}


def build_healthz_payload(
    results: dict[str, DependencyHealthResult],
) -> dict[str, str | dict[str, dict[str, str | int]]]:
    ordered_names = ("cosmos", "blob")
    ordered_results = {name: results[name].as_dict() for name in ordered_names if name in results}
    status = (
        "ok"
        if all(results.get(name) and is_dependency_healthy(results[name]) for name in ordered_names)
        else "unhealthy"
    )
    return {
        "status": status,
        "dependencies": ordered_results,
    }


def is_dependency_healthy(result: DependencyHealthResult) -> bool:
    return result.status in HEALTHY_DEPENDENCY_STATUSES


async def _run_dependency_probe(
    *,
    probe: HealthDependencyProbe,
    timeout_seconds: float,
    request_id: str | None,
) -> DependencyHealthResult:
    safe_log(
        "dependency.started",
        request_id=request_id,
        attributes={
            "fcg.dependency": probe.name,
            "fcg.outcome": "started",
        },
    )
    try:
        result = await probe.check(timeout_seconds)
    except Exception:
        result = DependencyHealthResult(
            name=probe.name,
            status="unavailable",
            duration_ms=0,
            error_category="unavailable",
        )
    safe_log(
        _event_name_for_status(result.status),
        request_id=request_id,
        attributes={
            "fcg.dependency": result.name,
            "fcg.outcome": _outcome_for_status(result.status),
            "fcg.error_code": result.error_category,
            "fcg.duration_ms": result.duration_ms,
        },
    )
    return result


def _event_name_for_status(status: DependencyStatus) -> str:
    if status == "timeout":
        return "dependency.timeout"
    if status in HEALTHY_DEPENDENCY_STATUSES:
        return "dependency.completed"
    return "dependency.failed"


def _outcome_for_status(status: DependencyStatus) -> str:
    if status == "timeout":
        return "timed_out"
    if status in HEALTHY_DEPENDENCY_STATUSES:
        return "completed"
    return "failed"


def _classify_status(
    exc: Exception,
) -> Literal["unavailable", "unauthorized", "misconfigured", "timeout"]:
    return _classify_error_category(exc)


def _classify_error_category(
    exc: Exception,
) -> Literal["unavailable", "unauthorized", "misconfigured", "timeout"]:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, ClientAuthenticationError):
        return "unauthorized"
    if isinstance(exc, ResourceNotFoundError):
        return "misconfigured"
    if isinstance(exc, (ServiceRequestError, ServiceResponseError)):
        return "unavailable"
    if isinstance(exc, HttpResponseError):
        status_code = getattr(exc, "status_code", None)
        if status_code in {400, 404}:
            return "misconfigured"
        if status_code in {401, 403}:
            return "unauthorized"
        return "unavailable"
    if isinstance(exc, (AttributeError, TypeError, ValueError)):
        return "misconfigured"
    return "unavailable"


def _result(
    *,
    name: Literal["cosmos", "blob"],
    status: DependencyStatus,
    start: float,
    error_category: DependencyErrorCategory = "none",
) -> DependencyHealthResult:
    duration_ms = max(0, round((time.perf_counter() - start) * 1000))
    return DependencyHealthResult(
        name=name,
        status=status,
        duration_ms=duration_ms,
        error_category=error_category,
    )
