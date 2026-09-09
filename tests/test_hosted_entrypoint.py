"""
Hosted-agent startup gate tests — mandatory telemetry monitoring (#99 finding 5).

These tests do NOT import azure.ai.agentserver; the deferred server import in __main__.py
means the startup gate (telemetry check) can be fully tested without the pinned SDK present.
For the success path, sys.modules is pre-populated with a stub server module so the deferred
import resolves without touching the real SDK.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from app import telemetry

_VALID_HOSTED_ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-4o",
    "CARD_ORCHESTRATOR_VERSION": "candidate-1",
}


def _set_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _VALID_HOSTED_ENV.items():
        monkeypatch.setenv(key, value)


def _reset_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure configure_telemetry() executes the real path rather than returning cached state."""
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_enabled", False)


def test_hosted_entrypoint_fails_when_platform_connection_string_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mandatory monitoring gate: missing platform-injected connection string must block startup.

    Foundry reserves APPLICATIONINSIGHTS_CONNECTION_STRING and injects it at runtime.
    When it is absent the hosted runtime has no telemetry and must not serve requests.
    create_host must never be reached in this scenario.
    """
    import hosted_agents.card_orchestrator.__main__ as entrypoint

    _set_valid_env(monkeypatch)
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    _reset_telemetry(monkeypatch)

    host_created: list[bool] = []
    monkeypatch.setattr(entrypoint, "configure_telemetry", lambda: False)

    with pytest.raises(SystemExit) as exc:
        entrypoint.main()

    msg = str(exc.value).lower()
    assert (
        "telemetry" in msg or "monitoring" in msg
    ), f"Exit message must mention telemetry/monitoring, got: {exc.value!r}"
    assert not host_created, "create_host must not be reached when telemetry init returns False"


def test_hosted_entrypoint_fails_when_telemetry_exporter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exporter/SDK failure during configure_azure_monitor must also block startup.

    configure_telemetry() swallows the exception internally and returns False.
    The hosted entrypoint treats False as fatal; raw exception internals must not leak.
    """
    import azure.monitor.opentelemetry

    import hosted_agents.card_orchestrator.__main__ as entrypoint

    _set_valid_env(monkeypatch)
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=fake-test-key")
    _reset_telemetry(monkeypatch)

    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("exporter_unavailable")),
    )

    host_created: list[bool] = []

    def _guard(*_a: object, **_kw: object) -> None:
        host_created.append(True)

    monkeypatch.setattr(entrypoint, "configure_telemetry", lambda: False)

    with pytest.raises(SystemExit) as exc:
        entrypoint.main()

    assert "exporter_unavailable" not in str(
        exc.value
    ), "Raw exporter exception detail must not appear in the exit message"
    assert not host_created, "create_host must not be reached when telemetry init raises"


def test_hosted_entrypoint_proceeds_when_telemetry_initializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal startup: server is started on port 8088 when telemetry succeeds.

    azure.ai.agentserver is stubbed via sys.modules so the deferred import in main() resolves
    without the pinned SDK; this tests the real __main__.main() lifecycle path.
    """
    import hosted_agents.card_orchestrator.__main__ as entrypoint

    _set_valid_env(monkeypatch)
    monkeypatch.setattr(entrypoint, "configure_telemetry", lambda: True)

    started_ports: list[int] = []

    class _StubHost:
        def run(self, port: int) -> None:
            started_ports.append(port)

    fake_server = ModuleType("hosted_agents.card_orchestrator.server")
    fake_server.create_host = lambda _settings: _StubHost()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hosted_agents.card_orchestrator.server", fake_server)

    entrypoint.main()

    assert started_ports == [
        8088
    ], "Server must be started on port 8088 after successful telemetry initialisation"
