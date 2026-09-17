"""Agent-only card text generation contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import generation as generation_module
from app.foundry_agent_client import FoundryAgentClient, FoundryAgentInvocationResult
from app.generation import (
    AppServices,
    AuthenticatedOwner,
    CardGenerationService,
    GeneratedCardModel,
    InMemoryAssetStore,
    InMemoryAuditRepository,
    InMemoryCardRepository,
    MockAgentClient,
    MockAIClient,
    ModerationDecision,
    StoredCard,
    create_services,
)
from app.health import DependencyHealthResult
from app.main import create_app
from app.problems import ProblemDetails
from app.settings import SettingsError, load_app_settings
from tests.conftest import FakeOAuthClient, begin_login, extract_hidden_value


class HealthyAgentProbe:
    name = "agent"

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        del timeout_seconds
        return DependencyHealthResult("agent", "ok", 1)


class FailingAgentProbe:
    name = "agent"

    async def check(self, timeout_seconds: float) -> DependencyHealthResult:
        del timeout_seconds
        return DependencyHealthResult("agent", "unauthorized", 1, "unauthorized")


def _card(*, rules_text: str | None = None) -> GeneratedCardModel:
    return GeneratedCardModel(
        schemaVersion=1,
        name="Frost Warden",
        cardType="hero",
        rarity="rare",
        manaCost=4,
        attack=5,
        health=8,
        rulesText=rules_text or "When played, freeze all enemy creatures until your next turn.",
        flavorText="Cold logic, colder blade.",
        artBrief="A heavily armoured knight standing in a blizzard.",
    )


def _success(*, card: GeneratedCardModel | None = None) -> FoundryAgentInvocationResult:
    return FoundryAgentInvocationResult(
        status="completed",
        success=True,
        schema_valid=True,
        response_id="response-123",
        request_id="request-123",
        agent_version="1.0.0",
        card=card or _card(),
        art_prompt="Original frost guardian artwork in a blizzard.",
    )


def _live_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/my-project",
    )
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "card-orchestrator")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "1")
    return load_app_settings()


def _services(
    monkeypatch: pytest.MonkeyPatch,
    agent_client: Any,
    *,
    ai_client: Any | None = None,
    agent_probe: Any | None = None,
) -> AppServices:
    settings = _live_settings(monkeypatch)
    defaults = create_services(settings)
    return AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=InMemoryAssetStore(),
        ai_client=ai_client or MockAIClient(settings),
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
        cosmos_health_probe=defaults.cosmos_health_probe,
        blob_health_probe=defaults.blob_health_probe,
        agent_client=agent_client,
        agent_health_probe=agent_probe or HealthyAgentProbe(),
        saved_photo_repository=defaults.saved_photo_repository,
        profile_photo_import_state_repository=defaults.profile_photo_import_state_repository,
        photo_asset_store=defaults.photo_asset_store,
        photo_moderation_service=defaults.photo_moderation_service,
        deletion_audit_repository=defaults.deletion_audit_repository,
    )


def _client(monkeypatch: pytest.MonkeyPatch, services: AppServices) -> TestClient:
    import app.main as main_module

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(services=services), base_url="https://testserver")
    begin_login(client)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    return client


def _generate(client: TestClient, *, key: str = "agent-only-test"):
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")
    return client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "A safe original frost guardian hero with a silver shield",
            "idempotencyKey": key,
            "csrfToken": csrf_token,
        },
    )


class StageModeration:
    def __init__(self, refused_stage: str | None = None) -> None:
        self.refused_stage = refused_stage
        self.calls: list[str] = []

    async def moderate_text(self, text: str, *, stage: str) -> ModerationDecision:
        del text
        self.calls.append(stage)
        refused = stage == self.refused_stage
        return ModerationDecision(
            stage=stage,
            allowed=not refused,
            reasonCode="graphic-violence" if refused else "allowed",
            details="bounded test decision",
        )

    async def moderate_image(self, image: Any) -> ModerationDecision:
        del image
        stage = "post_image"
        self.calls.append(stage)
        refused = stage == self.refused_stage
        return ModerationDecision(
            stage=stage,
            allowed=not refused,
            reasonCode="unsafe-generated-image" if refused else "allowed",
            details="bounded test decision",
        )


class RefusalPersistenceBombCardRepository(InMemoryCardRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_on_mutation = True

    async def reserve_document(self, **kwargs: Any):
        if self.fail_on_mutation:
            raise AssertionError("refusal must not reserve a card document")
        return await super().reserve_document(**kwargs)

    async def save(self, record: StoredCard) -> StoredCard:
        if self.fail_on_mutation:
            raise AssertionError("refusal must not save a card document")
        return await super().save(record)

    async def delete(self, owner_id: str, card_id: str) -> None:
        del owner_id, card_id
        raise RuntimeError("simulated Cosmos delete failure")


class RefusalPersistenceBombAuditRepository(InMemoryAuditRepository):
    async def reserve_audit(self, **kwargs: Any):
        del kwargs
        raise AssertionError("refusal must not reserve an audit document")

    async def save_audit(self, record: StoredCard) -> None:
        del record
        raise AssertionError("refusal must not save an audit document")

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        del owner_id, card_id
        raise RuntimeError("simulated Cosmos audit delete failure")


def test_live_mode_requires_agent_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)

    with pytest.raises(SettingsError, match="FOUNDRY_PROJECT_ENDPOINT"):
        load_app_settings()

    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/my-project",
    )
    with pytest.raises(SettingsError, match="FOUNDRY_AGENT_NAME"):
        load_app_settings()


def test_agent_flag_cannot_disable_live_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "false")
    settings = _live_settings(monkeypatch)

    services = create_services(settings)

    assert services.agent_client is not None
    assert services.agent_health_probe is not None
    assert not hasattr(settings, "agent_generation_enabled")


@pytest.mark.parametrize("app_env", ["test", "development", "production"])
def test_environment_cannot_select_mock_clients(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("AI_MODE", "mock")

    settings = _live_settings(monkeypatch)
    services = create_services(settings)

    assert not hasattr(settings, "ai_mode")
    assert not isinstance(services.ai_client, MockAIClient)
    assert not isinstance(services.agent_client, MockAgentClient)


def test_live_mode_requires_mandatory_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_settings(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    with pytest.raises(SettingsError, match="TELEMETRY_ENABLED=true"):
        load_app_settings()


def test_agent_success_preserves_card_contract_image_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            calls.append(query)
            return _success()

    services = _services(monkeypatch, Agent())
    response = _generate(_client(monkeypatch, services))

    assert response.status_code == 200
    assert response.json() == {
        "schemaVersion": 1,
        "cardId": response.json()["cardId"],
        "status": "completed",
        "requestId": response.json()["requestId"],
        "idempotencyKey": "agent-only-test",
        "ownerId": response.json()["ownerId"],
        "name": "Frost Warden",
        "cardType": "hero",
        "rarity": "rare",
        "manaCost": 4,
        "attack": 5,
        "health": 8,
        "rulesText": "When played, freeze all enemy creatures until your next turn.",
        "flavorText": "Cold logic, colder blade.",
        "imageUrl": f"/cards/{response.json()['cardId']}/image",
        "actions": [],
    }
    assert calls == ["A safe original frost guardian hero with a silver shield"]
    assert len(services.card_repository._records) == 1
    assert len(services.asset_store._assets) == 1


@pytest.mark.parametrize(
    ("result", "status_code", "error_code"),
    [
        (
            FoundryAgentInvocationResult(
                status="transient_error", retryable=True, error_code="timeout"
            ),
            504,
            "upstream_timeout",
        ),
        (
            FoundryAgentInvocationResult(
                status="transient_error", retryable=True, error_code="rate_limited"
            ),
            503,
            "agent_unavailable",
        ),
        (
            FoundryAgentInvocationResult(
                status="routing_defer", schema_valid=True, error_code="agent_routing_defer"
            ),
            503,
            "agent_unavailable",
        ),
        (
            FoundryAgentInvocationResult(status="auth_error", error_code="credential_unavailable"),
            503,
            "configuration_error",
        ),
        (
            FoundryAgentInvocationResult(
                status="configuration_error", error_code="invalid_configuration"
            ),
            503,
            "configuration_error",
        ),
        (
            FoundryAgentInvocationResult(
                status="invalid_response", error_code="schema_validation_failed"
            ),
            502,
            "invalid_model_output",
        ),
        (
            FoundryAgentInvocationResult(
                status="failed",
                error_code="card_runtime_failure",
                runtime_failure_stage="concept",
                runtime_failure_reason="timeout",
                runtime_http_type="api_status",
                runtime_http_status="http_504",
            ),
            504,
            "upstream_timeout",
        ),
        (
            FoundryAgentInvocationResult(
                status="failed",
                error_code="card_runtime_failure",
                runtime_failure_stage="lore",
                runtime_failure_reason="rate_limited",
                runtime_http_type="rate_limit",
                runtime_http_status="http_429",
            ),
            503,
            "agent_unavailable",
        ),
        (
            FoundryAgentInvocationResult(
                status="failed",
                error_code="card_runtime_failure",
                runtime_failure_stage="specialist_setup",
                runtime_failure_reason="authorization",
                runtime_http_type="permission_denied",
                runtime_http_status="http_403",
            ),
            503,
            "configuration_error",
        ),
        (
            FoundryAgentInvocationResult(
                status="failed",
                error_code="card_runtime_failure",
                runtime_failure_stage="orchestration",
                runtime_failure_reason="invalid_response",
                runtime_http_type="none",
                runtime_http_status="none",
            ),
            502,
            "invalid_model_output",
        ),
    ],
)
def test_agent_failures_are_structured_and_never_fallback(
    monkeypatch: pytest.MonkeyPatch,
    result: FoundryAgentInvocationResult,
    status_code: int,
    error_code: str,
) -> None:
    direct_calls = 0

    class NoDirectTextClient(MockAIClient):
        async def generate_card(self, prompt: str, *, request_id: str):
            nonlocal direct_calls
            direct_calls += 1
            raise AssertionError("legacy direct text generation must never run")

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return result

    settings = _live_settings(monkeypatch)
    services = _services(
        monkeypatch,
        Agent(),
        ai_client=NoDirectTextClient(settings),
    )
    response = _generate(_client(monkeypatch, services), key=f"failure-{status_code}-{error_code}")

    assert response.status_code == status_code
    assert response.json()["errorCode"] == error_code
    assert direct_calls == 0
    assert not services.card_repository._records
    assert not services.asset_store._assets
    assert len(services.audit_repository._records) == 1
    assert "silver shield" not in response.text


def test_hosted_agent_http_408_returns_public_upstream_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Credential:
        def get_token(self, *scopes: str) -> str:
            del scopes
            return "fake-token"

    settings = _live_settings(monkeypatch)
    agent = FoundryAgentClient(
        settings,
        credential=Credential(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                408,
                json={"error": {"code": "request_timeout"}},
                request=request,
            )
        ),
    )
    services = _services(monkeypatch, agent)

    with _client(monkeypatch, services) as client:
        response = _generate(client, key="hosted-http-408")

    assert response.status_code == 504
    assert response.json()["errorCode"] == "upstream_timeout"
    assert not services.card_repository._records
    assert not services.asset_store._assets


def test_input_moderation_refusal_persists_nothing_and_stops_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            nonlocal calls
            calls += 1
            return _success()

    services = _services(monkeypatch, Agent())
    moderation = StageModeration("pre_prompt")
    services.moderation_service = moderation
    client = _client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")
    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "Create graphic gore in the style of a living artist",
            "idempotencyKey": "input-moderation",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "prompt_rejected"
    assert calls == 0
    assert moderation.calls == ["pre_prompt"]
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_output_moderation_refusal_persists_nothing_and_stops_before_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_calls = 0

    class ImageSpy(MockAIClient):
        async def generate_image(self, art_prompt: str, **kwargs: Any):
            nonlocal image_calls
            image_calls += 1
            return await super().generate_image(art_prompt, **kwargs)

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

    settings = _live_settings(monkeypatch)
    services = _services(monkeypatch, Agent(), ai_client=ImageSpy(settings))
    moderation = StageModeration("post_text")
    services.moderation_service = moderation
    response = _generate(_client(monkeypatch, services), key="output-moderation")

    assert response.status_code == 422
    assert response.json()["errorCode"] == "generated_text_rejected"
    assert image_calls == 0
    assert moderation.calls == ["pre_prompt", "post_text"]
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_art_prompt_moderation_refusal_persists_nothing_and_stops_before_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_calls = 0

    class ImageSpy(MockAIClient):
        async def generate_image(self, art_prompt: str, **kwargs: Any):
            nonlocal image_calls
            image_calls += 1
            return await super().generate_image(art_prompt, **kwargs)

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

    settings = _live_settings(monkeypatch)
    services = _services(monkeypatch, Agent(), ai_client=ImageSpy(settings))
    moderation = StageModeration("post_art_prompt")
    services.moderation_service = moderation
    response = _generate(_client(monkeypatch, services), key="art-prompt-moderation")

    assert response.status_code == 422
    assert response.json()["errorCode"] == "generated_art_rejected"
    assert image_calls == 0
    assert moderation.calls == ["pre_prompt", "post_text", "post_art_prompt"]
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_agent_policy_refusal_persists_nothing_and_stops_before_output_moderation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_calls = 0

    class ImageSpy(MockAIClient):
        async def generate_image(self, art_prompt: str, **kwargs: Any):
            nonlocal image_calls
            image_calls += 1
            return await super().generate_image(art_prompt, **kwargs)

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return FoundryAgentInvocationResult(
                status="refused",
                schema_valid=True,
                error_code="agent_refused",
            )

    settings = _live_settings(monkeypatch)
    services = _services(monkeypatch, Agent(), ai_client=ImageSpy(settings))
    moderation = StageModeration()
    services.moderation_service = moderation
    response = _generate(_client(monkeypatch, services), key="agent-refused")

    assert response.status_code == 422
    assert response.json()["errorCode"] == "prompt_rejected"
    assert image_calls == 0
    assert moderation.calls == ["pre_prompt"]
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_image_moderation_refusal_persists_nothing_and_stops_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_calls = 0

    class ImageSpy(MockAIClient):
        async def generate_image(self, art_prompt: str, **kwargs: Any):
            nonlocal image_calls
            image_calls += 1
            return await super().generate_image(art_prompt, **kwargs)

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

    settings = _live_settings(monkeypatch)
    services = _services(monkeypatch, Agent(), ai_client=ImageSpy(settings))
    moderation = StageModeration("post_image")
    services.moderation_service = moderation
    response = _generate(_client(monkeypatch, services), key="image-moderation")

    assert response.status_code == 422
    assert response.json()["errorCode"] == "generated_art_rejected"
    assert image_calls == 1
    assert moderation.calls == [
        "pre_prompt",
        "post_text",
        "post_art_prompt",
        "post_image",
    ]
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


@pytest.mark.parametrize(
    ("refusal_point", "expected_error"),
    [
        ("pre_prompt", "prompt_rejected"),
        ("agent", "prompt_rejected"),
        ("post_text", "generated_text_rejected"),
        ("post_art_prompt", "generated_art_rejected"),
        ("post_image", "generated_art_rejected"),
    ],
)
def test_refusal_never_requires_repository_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    refusal_point: str,
    expected_error: str,
) -> None:
    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            del query
            if refusal_point == "agent":
                return FoundryAgentInvocationResult(
                    status="refused",
                    schema_valid=True,
                    error_code="agent_refused",
                )
            return _success()

    services = _services(monkeypatch, Agent())
    services.card_repository = RefusalPersistenceBombCardRepository()
    services.audit_repository = RefusalPersistenceBombAuditRepository()
    services.moderation_service = StageModeration(
        None if refusal_point == "agent" else refusal_point
    )

    response = _generate(
        _client(monkeypatch, services),
        key=f"repository-bomb-{refusal_point}",
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == expected_error
    assert not services.card_repository._records
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_artwork_retry_moderation_refusal_preserves_safe_partial_without_retry_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

    services = _services(monkeypatch, Agent())
    card_repository = RefusalPersistenceBombCardRepository()
    card_repository.fail_on_mutation = False
    services.card_repository = card_repository
    services.audit_repository = RefusalPersistenceBombAuditRepository()
    moderation = StageModeration("post_image")
    services.moderation_service = moderation
    owner = AuthenticatedOwner(
        owner_id="tenant:object",
        tenant_id="tenant",
        object_id="object",
        subject="subject",
        display_name=None,
        email=None,
    )
    record = StoredCard(
        id="partial-card",
        document_type="card",
        owner_id=owner.owner_id,
        request_id="initial-request",
        idempotency_key="initial-key",
        request_hash="initial-hash",
        status="awaiting_artwork_retry",
        validated_payload=_card().model_dump(),
        derived_art_prompt="Safe original fantasy artwork.",
        moderation=[],
        image_quality="low",
    )
    asyncio.run(services.card_repository.save(record))
    card_repository.fail_on_mutation = True

    with pytest.raises(ProblemDetails) as raised:
        asyncio.run(
            CardGenerationService(services).retry_artwork(
                owner=owner,
                card_id=record.id,
                idempotency_key="retry-key",
                request_id="retry-request",
                client_ip="127.0.0.1",
            )
        )

    assert raised.value.status_code == 422
    assert raised.value.error_code == "generated_art_rejected"
    assert moderation.calls == ["post_image"]
    persisted = asyncio.run(services.card_repository.get(owner.owner_id, record.id))
    assert persisted is record
    assert persisted.status == "awaiting_artwork_retry"
    assert not services.audit_repository._records
    assert not services.asset_store._assets


def test_concurrent_duplicate_refusals_share_bounded_in_memory_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingRefusalAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            del query
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return FoundryAgentInvocationResult(
                status="refused",
                schema_valid=True,
                error_code="agent_refused",
            )

    async def scenario() -> tuple[list[BaseException], int]:
        agent = BlockingRefusalAgent()
        services = _services(monkeypatch, agent)
        services.card_repository = RefusalPersistenceBombCardRepository()
        services.audit_repository = RefusalPersistenceBombAuditRepository()
        service = CardGenerationService(services)
        owner = AuthenticatedOwner(
            owner_id="tenant:object",
            tenant_id="tenant",
            object_id="object",
            subject="subject",
            display_name=None,
            email=None,
        )
        first = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="A safe original frost guardian hero with a silver shield",
                idempotency_key="concurrent-refusal",
                request_id="request-one",
                client_ip="127.0.0.1",
            )
        )
        await agent.started.wait()
        second = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="A safe original frost guardian hero with a silver shield",
                idempotency_key="concurrent-refusal",
                request_id="request-two",
                client_ip="127.0.0.1",
            )
        )
        await asyncio.sleep(0)
        agent.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        return [result for result in results if isinstance(result, BaseException)], agent.calls

    problems, calls = asyncio.run(scenario())

    assert calls == 1
    assert len(problems) == 2
    assert all(isinstance(problem, ProblemDetails) for problem in problems)
    assert [problem.status_code for problem in problems] == [422, 422]
    assert [problem.error_code for problem in problems] == [
        "prompt_rejected",
        "prompt_rejected",
    ]


def test_startup_does_not_wait_for_remote_agent_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedProbe:
        name = "agent"

        async def check(self, timeout_seconds: float) -> DependencyHealthResult:
            del timeout_seconds
            raise AssertionError("startup must not run the remote agent preflight")

    services = _services(monkeypatch, object(), agent_probe=UnexpectedProbe())
    app = create_app(services=services)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(scenario())


def test_livez_remains_responsive_while_readiness_waits_for_agent_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedAgentProbe:
        name = "agent"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def check(self, timeout_seconds: float) -> DependencyHealthResult:
            del timeout_seconds
            self.started.set()
            await self.release.wait()
            return DependencyHealthResult("agent", "ok", 70_000)

    probe = DelayedAgentProbe()
    services = _services(monkeypatch, object(), agent_probe=probe)
    app = create_app(services=services)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://testserver",
            ) as client:
                readiness = asyncio.create_task(client.get("/healthz"))
                await asyncio.wait_for(probe.started.wait(), timeout=0.1)

                live = await asyncio.wait_for(client.get("/livez"), timeout=0.1)
                assert live.status_code == 200
                assert live.json() == {"status": "ok"}
                assert not readiness.done()

                probe.release.set()
                ready = await readiness
                assert ready.status_code == 200
                assert ready.json()["dependencies"]["agent"]["status"] == "ok"

    asyncio.run(scenario())


def test_readiness_fails_closed_when_agent_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(monkeypatch, object(), agent_probe=FailingAgentProbe())

    with TestClient(create_app(services=services), base_url="https://testserver") as client:
        assert client.get("/livez").status_code == 200
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["dependencies"]["agent"] == {
        "status": "unauthorized",
        "durationMs": 1,
        "errorCategory": "unauthorized",
    }


def test_agent_client_is_closed_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    class Agent:
        closed = False

        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

        async def aclose(self) -> None:
            self.closed = True

    agent = Agent()
    services = _services(monkeypatch, agent)

    with TestClient(create_app(services=services), base_url="https://testserver"):
        pass

    assert agent.closed is True


def test_agent_invocation_telemetry_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class Agent:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return _success()

    monkeypatch.setattr(
        generation_module,
        "add_event",
        lambda name, attributes=None: events.append((name, attributes or {})),
    )
    response = _generate(_client(monkeypatch, _services(monkeypatch, Agent())), key="telemetry")

    assert response.status_code == 200
    invocation = next(attributes for name, attributes in events if name == "agent.invocation")
    assert invocation["fcg.dependency"] == "foundry_agent"
    assert invocation["fcg.generation_path"] == "agent"
    assert invocation["fcg.outcome"] == "completed"
    serialized = repr(invocation)
    assert "silver shield" not in serialized
    assert "Frost Warden" not in serialized


def test_test_runtime_mock_remains_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AI_MODE", "mock")
    settings = load_app_settings()

    first = create_services(
        settings,
        ai_client=MockAIClient(settings),
        agent_client=MockAgentClient(),
    )
    second = create_services(
        settings,
        ai_client=MockAIClient(settings),
        agent_client=MockAgentClient(),
    )

    assert isinstance(first.agent_client, MockAgentClient)
    assert isinstance(second.agent_client, MockAgentClient)
    assert not hasattr(replace(settings), "ai_mode")
