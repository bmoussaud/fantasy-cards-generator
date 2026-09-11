from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from app import rotation_observability as rotation
from app import telemetry
from app.auth import EntraOAuthClientManager, load_auth_settings
from app.secrets import EnvSecretProvider, SecretProviderError, run_secret_refresh_worker
from tests.test_secret_rotation_runtime import ControlledSleep, pair_provider
from tests.test_secrets import FakeSecretBundle, make_version
from tests.test_telemetry import telemetry_settings

SENSITIVE = "NEVER-EXPORT-user@example.com-secret-token"
NAME = "APP_SESSION_SECRET_KEY"
VAULT_NAME = "app-session-secret-key"
CONTRACT_KEYS = {
    "event",
    "schema_version",
    "observed_at",
    "sequence",
    "pid",
    "incarnation",
    "revision",
    "replica",
    "logical_secret",
    "source",
    "stage",
    "result",
    "version_hash",
    "error_category",
}


@pytest.fixture(autouse=True)
def isolated_rotation_logger(monkeypatch):
    monkeypatch.setattr(rotation._logger, "handlers", [])
    monkeypatch.setattr(rotation._logger, "propagate", False)
    monkeypatch.setattr(rotation._logger, "level", logging.NOTSET)
    monkeypatch.setattr(rotation, "_pid", None)
    monkeypatch.setattr(rotation, "_incarnation", None)
    monkeypatch.setattr(rotation, "_sequence", 0)
    monkeypatch.setenv("CONTAINER_APP_REVISION", "app--revision-1")
    monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", "app--revision-1-replica-1")


@pytest.fixture
def sdk_pipeline(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    handler = LoggingHandler(logger_provider=provider)
    monkeypatch.setattr(telemetry._logger, "handlers", [])
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry, "_tracer", None)
    monkeypatch.setattr(telemetry, "_instrument_httpx", lambda: None)
    monkeypatch.setattr(telemetry, "_initialize_metrics", lambda: None)
    import azure.monitor.opentelemetry

    def configure(**kwargs):
        assert kwargs["logger_name"] == telemetry.LOGGER_NAME
        for processor in kwargs["log_record_processors"]:
            provider.add_log_record_processor(processor)
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        logging.getLogger(kwargs["logger_name"]).addHandler(handler)

    monkeypatch.setattr(azure.monitor.opentelemetry, "configure_azure_monitor", configure)
    assert telemetry.configure_telemetry(
        telemetry_settings(enabled=True, connection_string="InstrumentationKey=synthetic")
    )
    yield exporter
    provider.shutdown()


def emit(**changes):
    fields = {
        "name": NAME,
        "source": "azure",
        "stage": "provider",
        "result": "adopted",
        "version": "synthetic-version",
    }
    fields.update(changes)
    rotation.emit_observation(**fields)


def exported_events(exporter):
    return [json.loads(item.log_record.body) for item in exporter.get_finished_logs()]


def test_actual_sdk_export_and_console_have_identical_complete_contract(sdk_pipeline, capsys):
    before = datetime.now(UTC)
    emit(version=SENSITIVE)
    after = datetime.now(UTC)
    console = json.loads(capsys.readouterr().err)
    records = sdk_pipeline.get_finished_logs()
    assert len(records) == 1
    record = records[0].log_record
    assert json.loads(record.body) == console
    assert dict(record.attributes) == {f"rotation.{k}": v for k, v in console.items()}
    assert set(console) == CONTRACT_KEYS
    assert console["event"] == rotation.EVENT_NAME
    assert console["schema_version"] == 1
    assert console["pid"] == os.getpid()
    assert console["sequence"] == 1
    assert len(console["incarnation"]) == 32
    assert console["version_hash"] == hashlib.sha256(SENSITIVE.encode()).hexdigest()[:12]
    assert console["revision"] == "app--revision-1"
    assert console["replica"] == "app--revision-1-replica-1"
    assert before <= datetime.fromisoformat(console["observed_at"]) <= after
    assert SENSITIVE not in repr(records) + json.dumps(console)


def test_disabled_telemetry_still_emits_single_json_without_root_propagation(monkeypatch, capsys):
    monkeypatch.setattr(telemetry, "_enabled", False)
    for _ in range(3):
        rotation.configure_rotation_logging()
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    logging.getLogger().addHandler(root_handler)
    try:
        emit(source="env", version=None)
    finally:
        logging.getLogger().removeHandler(root_handler)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["version_hash"] == "unknown"
    assert event["source"] == "env"
    assert sum(isinstance(h, rotation._ConsoleHandler) for h in rotation._logger.handlers) == 1
    assert root_stream.getvalue() == ""


def test_repeated_export_registration_does_not_duplicate(sdk_pipeline):
    handler = telemetry._logger.handlers[0]
    rotation.configure_rotation_logging(handler)
    rotation.configure_rotation_logging(handler)
    emit()
    assert len(sdk_pipeline.get_finished_logs()) == 1
    assert rotation._logger.handlers.count(handler) == 1
    assert sum(isinstance(h, rotation._ConsoleHandler) for h in rotation._logger.handlers) == 1


@pytest.mark.parametrize(
    "value",
    [SENSITIVE, "https://vault.example/secret", "line\nbreak", "x" * 129, "", "has spaces", "é"],
)
def test_untrusted_platform_metadata_is_unknown_in_both_routes(
    value, sdk_pipeline, monkeypatch, capsys
):
    monkeypatch.setenv("CONTAINER_APP_REVISION", value)
    monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", value)
    emit(source=SENSITIVE, result="failed", version=None, error_category=SENSITIVE)
    event = exported_events(sdk_pipeline)[0]
    assert event["revision"] == event["replica"] == "unknown"
    assert event["source"] == event["error_category"] == "unknown"
    assert json.loads(capsys.readouterr().err) == event


def test_missing_metadata_is_explicit_unknown(monkeypatch, capsys):
    monkeypatch.delenv("CONTAINER_APP_REVISION")
    monkeypatch.delenv("CONTAINER_APP_REPLICA_NAME")
    emit()
    event = json.loads(capsys.readouterr().err)
    assert event["revision"] == event["replica"] == "unknown"


def test_last_mile_revalidates_body_and_strips_extra_attributes_and_exceptions(
    sdk_pipeline, capsys
):
    emit()
    original = exported_events(sdk_pipeline)[0]
    contaminated = dict(original, token=SENSITIVE, email=SENSITIVE)
    try:
        raise RuntimeError(SENSITIVE)
    except RuntimeError:
        rotation._logger.info(
            json.dumps(contaminated),
            extra={"authorization": SENSITIVE, "rotation.pid": SENSITIVE},
            exc_info=True,
        )
    records = sdk_pipeline.get_finished_logs()
    assert json.loads(records[-1].log_record.body) == original
    assert SENSITIVE not in repr(records)
    assert [json.loads(line) for line in capsys.readouterr().err.splitlines()] == [
        original,
        original,
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("event", SENSITIVE),
        ("source", SENSITIVE),
        ("error_category", SENSITIVE),
        ("logical_secret", SENSITIVE),
        ("stage", SENSITIVE),
        ("stage", "entra_binding"),
        ("result", SENSITIVE),
        ("result", "stale"),
        ("error_category", "network_error"),
        ("schema_version", 2),
        ("sequence", -1),
        ("sequence", 2**63),
        ("pid", True),
        ("incarnation", SENSITIVE),
        ("observed_at", SENSITIVE),
        ("observed_at", "2026-99-99T99:99:99.000000Z"),
        ("revision", SENSITIVE),
        ("replica", "a" * 129),
        ("version_hash", SENSITIVE),
    ],
)
def test_invalid_contract_is_rejected_by_actual_sdk_privacy_processor(
    key, value, sdk_pipeline, capsys
):
    emit()
    event = exported_events(sdk_pipeline)[0]
    capsys.readouterr()
    event[key] = value
    rotation._logger.info(json.dumps(event), extra={"token": SENSITIVE})
    record = sdk_pipeline.get_finished_logs()[-1].log_record
    assert record.body == "rotation.observation_rejected"
    assert dict(record.attributes) == {}
    assert SENSITIVE not in repr(record) + capsys.readouterr().err


def test_rotation_attributes_are_not_general_metric_or_log_allowlist(sdk_pipeline):
    assert telemetry.safe_attributes({"rotation.pid": 42, "rotation.version_hash": "a" * 12}) == {}
    telemetry._logger.log(
        logging.WARNING, "generation.failed", extra={"rotation.pid": 42, "token": SENSITIVE}
    )
    record = sdk_pipeline.get_finished_logs()[-1].log_record
    assert not dict(record.attributes)


def test_sequence_is_monotonic_across_concurrent_emitters(sdk_pipeline):
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: emit(), range(80)))
    events = exported_events(sdk_pipeline)
    assert [event["sequence"] for event in events] == list(range(1, 81))
    assert len({event["incarnation"] for event in events}) == 1
    assert {event["pid"] for event in events} == {os.getpid()}


def test_process_change_starts_new_incarnation_and_sequence(monkeypatch, capsys):
    emit()
    first = json.loads(capsys.readouterr().err)
    monkeypatch.setattr(rotation.os, "getpid", lambda: first["pid"] + 1)
    emit()
    second = json.loads(capsys.readouterr().err)
    assert second["incarnation"] != first["incarnation"]
    assert second["sequence"] == 1
    assert second["pid"] == first["pid"] + 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is a Unix process lifecycle")
def test_fork_resets_identity_and_inherited_locked_mutex(capsys):
    emit()
    parent = json.loads(capsys.readouterr().err)
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with rotation._lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(2)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)

    def child():
        emit()
        sender.send((rotation._pid, rotation._incarnation, rotation._sequence))
        sender.close()

    process = context.Process(target=child)
    try:
        process.start()
        assert receiver.poll(5), "Child emission blocked on a pre-fork mutex"
        pid, incarnation, sequence = receiver.recv()
        process.join(5)
        assert process.exitcode == 0
        assert pid == process.pid and pid != parent["pid"]
        assert incarnation != parent["incarnation"]
        assert sequence == 1
    finally:
        release.set()
        thread.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        receiver.close()
        sender.close()


def test_console_io_failure_reports_fixed_diagnostic_without_exception(monkeypatch, capsys):
    class BrokenStream:
        def write(self, message):
            raise OSError(SENSITIVE)

    handler = rotation._ConsoleHandler()
    # Exercise StreamHandler's standard error path using the sanitized record.
    handler.stream = BrokenStream()
    record = logging.LogRecord(rotation.LOGGER_NAME, logging.INFO, "", 0, "safe", (), None)
    logging.StreamHandler.emit(handler, record)
    assert capsys.readouterr().err == '{"event":"rotation.observability_delivery_failed"}\n'


def test_provider_publishes_adopted_unchanged_stale_failed_and_recovery(sdk_pipeline):
    async def scenario():
        clock, kv, provider = pair_provider(cache_ttl=10, max_stale=20)
        await provider.get_secret(NAME)
        await provider.refresh_secret(NAME)
        clock.advance(11)
        kv.set_response(VAULT_NAME, None, ServiceRequestError(SENSITIVE))
        await provider.refresh_secret(NAME)
        clock.advance(20)
        with pytest.raises(SecretProviderError):
            await provider.refresh_secret(NAME)
        clock.advance(5)
        latest = make_version(clock, SENSITIVE)
        kv._versions[VAULT_NAME] = [latest]
        kv.set_response(VAULT_NAME, None, FakeSecretBundle(SENSITIVE, latest))
        await provider.refresh_secret(NAME)
        assert provider.snapshot_for_read(NAME).current.version == SENSITIVE
        events = exported_events(sdk_pipeline)
        assert [event["result"] for event in events] == [
            "adopted",
            "unchanged",
            "stale",
            "failed",
            "adopted",
        ]
        assert {event["stage"] for event in events} == {"provider"}
        assert events[2]["error_category"] == events[3]["error_category"] == "network_error"
        assert SENSITIVE not in repr(sdk_pipeline.get_finished_logs())
        await provider.aclose()

    asyncio.run(scenario())


def test_binding_evidence_requires_assignment_and_survives_idle_worker(sdk_pipeline):
    async def scenario():
        clock, kv, provider = pair_provider()
        old = kv._versions[VAULT_NAME][0]
        kv._versions["entra-client-secret"] = [old]
        kv.set_response("entra-client-secret", None, FakeSecretBundle("synthetic-old", old))
        fail_factory = False

        def factory(settings):
            if fail_factory:
                raise RuntimeError(SENSITIVE)
            return object()

        manager = EntraOAuthClientManager(
            settings=load_auth_settings(),
            secret_provider=provider,
            clock=clock.now,
            client_factory=factory,
        )
        await manager.get_client()
        initial = manager._current_binding
        assert [e["stage"] for e in exported_events(sdk_pipeline)] == [
            "provider",
            "entra_binding",
        ]
        clock.advance(20)
        latest = make_version(clock, "synthetic-next")
        kv._versions["entra-client-secret"] = [latest]
        kv.set_response("entra-client-secret", None, FakeSecretBundle(SENSITIVE, latest))
        await provider.refresh_secret("ENTRA_CLIENT_SECRET")
        assert manager._current_binding is initial
        fail_factory = True
        with pytest.raises(RuntimeError, match=SENSITIVE):
            await manager.get_client()
        failed = exported_events(sdk_pipeline)[-1]
        assert failed["stage"] == "entra_binding"
        assert failed["result"] == "failed"
        assert failed["error_category"] == "binding_error"
        assert failed["version_hash"] == rotation.version_hash(initial.secret.version)
        assert manager._current_binding is initial
        fail_factory = False
        sleep = ControlledSleep()
        worker = asyncio.create_task(
            run_secret_refresh_worker(
                provider,
                "ENTRA_CLIENT_SECRET",
                on_refresh=manager.get_client,
                sleep=sleep,
                monotonic=lambda: clock.now().timestamp(),
            )
        )
        try:
            for expected in ("adopted", "unchanged"):
                _, tick = await sleep.waiting()
                clock.advance(20)
                tick.set_result(None)
                # A queued next sleep proves the callback completed, without HTTP traffic.
                delay, next_tick = await sleep.waiting()
                events = exported_events(sdk_pipeline)
                assert events[-2]["stage"] == "provider"
                assert events[-2]["result"] == "unchanged"
                assert events[-1]["stage"] == "entra_binding"
                assert events[-1]["result"] == expected
                assert events[-1]["version_hash"] == rotation.version_hash(latest.version)
                assert manager._current_binding.secret.version == latest.version
                sleep.calls.put_nowait((delay, next_tick))
            assert SENSITIVE not in repr(sdk_pipeline.get_finished_logs())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await provider.aclose()

    asyncio.run(scenario())


def test_binding_reports_stale_then_failed_without_claiming_success(sdk_pipeline):
    async def scenario():
        clock, kv, provider = pair_provider(cache_ttl=10, max_stale=20)
        old = kv._versions[VAULT_NAME][0]
        kv._versions["entra-client-secret"] = [old]
        kv.set_response("entra-client-secret", None, FakeSecretBundle(SENSITIVE, old))
        manager = EntraOAuthClientManager(
            settings=load_auth_settings(), secret_provider=provider, clock=clock.now
        )
        original = await manager.get_client()
        clock.advance(11)
        kv.set_response("entra-client-secret", None, ServiceRequestError(SENSITIVE))
        assert await manager.get_client() is original
        clock.advance(20)
        with pytest.raises(SecretProviderError):
            await manager.get_client()
        binding_events = [
            event for event in exported_events(sdk_pipeline) if event["stage"] == "entra_binding"
        ]
        assert [event["result"] for event in binding_events] == ["adopted", "stale", "failed"]
        assert binding_events[-1]["error_category"] == "network_error"
        assert SENSITIVE not in repr(sdk_pipeline.get_finished_logs())
        await provider.aclose()

    asyncio.run(scenario())


def test_observations_follow_snapshot_publication_and_binding_assignment(monkeypatch, capsys):
    async def scenario():
        from app import auth, secrets

        clock, kv, provider = pair_provider()
        old = kv._versions[VAULT_NAME][0]
        kv._versions["entra-client-secret"] = [old]
        kv.set_response("entra-client-secret", None, FakeSecretBundle(SENSITIVE, old))
        manager = EntraOAuthClientManager(
            settings=load_auth_settings(), secret_provider=provider, clock=clock.now
        )
        seen = []

        def inspect_published_state(**fields):
            if fields["stage"] == "provider":
                published = provider.snapshot_for_read(fields["name"])
                assert published.error_category is None
                assert published.current.version == fields["version"]
            else:
                assert manager._current_binding.secret.version == fields["version"]
                assert manager._current_binding.client.client_secret == SENSITIVE
            seen.append(fields["stage"])
            rotation.emit_observation(**fields)

        monkeypatch.setattr(secrets, "emit_observation", inspect_published_state)
        monkeypatch.setattr(auth, "emit_observation", inspect_published_state)
        await manager.get_client()
        await provider.refresh_secret("ENTRA_CLIENT_SECRET")
        await manager.get_client()
        assert seen == ["provider", "entra_binding", "provider", "entra_binding"]
        await provider.aclose()

    asyncio.run(scenario())
    assert SENSITIVE not in capsys.readouterr().err


def test_env_provider_has_unknown_version_and_never_hashes_values(sdk_pipeline):
    async def scenario():
        env = {NAME: SENSITIVE}
        provider = EnvSecretProvider(environ=env)
        await provider.get_secret(NAME)
        await provider.get_secret(NAME)
        del env[NAME]
        with pytest.raises(SecretProviderError):
            await provider.get_secret(NAME)
        assert [event["result"] for event in exported_events(sdk_pipeline)] == [
            "adopted",
            "unchanged",
            "failed",
        ]
        assert {event["version_hash"] for event in exported_events(sdk_pipeline)} == {"unknown"}
        assert SENSITIVE not in repr(sdk_pipeline.get_finished_logs())

    asyncio.run(scenario())
