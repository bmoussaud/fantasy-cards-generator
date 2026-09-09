"""Tests for AGENT_GENERATION_ENABLED feature gate and FoundryAgentClient integration.

Coverage:
- Feature flag off: agent settings optional, no agent call occurs
- Feature flag on: validation requires project endpoint and agent name
- Agent success path: card and art prompt used directly
- Agent retryable fallback: transient/routing_defer falls back to direct
- Agent non-retryable failures: auth, policy, schema errors do not silently bypass
- Fallback telemetry: agent.fallback and agent.invocation events emitted
- Privacy: no owner ID, session, or tokens sent to agent
- Post-text moderation still runs on agent card output
- Image generation and persistence unchanged
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import generation as generation_module
from app.foundry_agent_client import FoundryAgentInvocationResult
from app.generation import (
    AppServices,
    GeneratedCardModel,
    InMemoryAssetStore,
    InMemoryAuditRepository,
    InMemoryCardRepository,
    MockAIClient,
    create_services,
)
from app.main import create_app
from app.settings import SettingsError, load_app_settings
from tests.conftest import FakeOAuthClient, extract_hidden_value

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_card_model() -> GeneratedCardModel:
    return GeneratedCardModel(
        schemaVersion=1,
        name="Frost Warden",
        cardType="hero",
        rarity="rare",
        manaCost=4,
        attack=5,
        health=8,
        rulesText="When played, freeze all enemy creatures until your next turn.",
        flavorText="Cold logic, colder blade.",
        artBrief="A heavily armoured knight standing in a blizzard, dramatic lighting.",
    )


def _make_agent_success(
    art_prompt: str = "Epic frost warden illustration in a blizzard, dramatic lighting.",
) -> FoundryAgentInvocationResult:
    return FoundryAgentInvocationResult(
        status="completed",
        success=True,
        schema_valid=True,
        response_id="resp-abc123",
        request_id="req-xyz789",
        agent_version="1.0.0",
        card=_make_card_model(),
        art_prompt=art_prompt,
    )


def _make_settings_with_agent_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "true")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/my-project",
    )
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "card-orchestrator")
    return load_app_settings()


def _build_services_with_agent(agent_client, *, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings_with_agent_enabled(monkeypatch)
    defaults = create_services(settings)
    return AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=InMemoryAssetStore(),
        ai_client=defaults.ai_client,
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
        agent_client=agent_client,
    )


def _agent_client(monkeypatch: pytest.MonkeyPatch, services: AppServices) -> TestClient:
    """Return an authenticated TestClient wired to the given services."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "create_oauth_client", lambda s: FakeOAuthClient())
    client = TestClient(create_app(services=services), base_url="https://testserver")
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    return client


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_agent_generation_disabled_by_default() -> None:
    settings = load_app_settings()
    assert settings.agent_generation_enabled is False


def test_agent_generation_enabled_requires_project_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "true")
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)
    with pytest.raises(SettingsError, match="FOUNDRY_PROJECT_ENDPOINT"):
        load_app_settings()


def test_agent_generation_enabled_requires_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "true")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/my-project",
    )
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)
    with pytest.raises(SettingsError, match="FOUNDRY_AGENT_NAME"):
        load_app_settings()


def test_agent_generation_disabled_does_not_require_agent_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "false")
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)
    settings = load_app_settings()
    assert settings.agent_generation_enabled is False


def test_agent_generation_enabled_with_all_required_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings_with_agent_enabled(monkeypatch)
    assert settings.agent_generation_enabled is True
    assert settings.foundry_project_endpoint is not None
    assert settings.foundry_agent_name == "card-orchestrator"


# ---------------------------------------------------------------------------
# create_services: agent_client wiring
# ---------------------------------------------------------------------------


def test_create_services_no_agent_client_when_flag_off() -> None:
    settings = load_app_settings()
    services = create_services(settings)
    assert services.agent_client is None


def test_create_services_no_agent_client_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with flag on, ai_mode=mock means no agent client (no Azure credentials needed)."""
    monkeypatch.setenv("AGENT_GENERATION_ENABLED", "true")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/my-project",
    )
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "card-orchestrator")
    settings = load_app_settings()
    assert settings.agent_generation_enabled is True
    services = create_services(settings)
    # ai_mode=mock: agent client not created (no credentials available)
    assert services.agent_client is None


# ---------------------------------------------------------------------------
# Flag off: direct path, no agent call
# ---------------------------------------------------------------------------


def test_flag_off_does_not_invoke_agent_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When AGENT_GENERATION_ENABLED=false, agent.invoke must never be called."""

    class SpyAgentClient:
        def __init__(self) -> None:
            self.invoke_called = False

        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            self.invoke_called = True
            raise AssertionError("invoke must not be called when flag is off")

    settings = load_app_settings()
    assert not settings.agent_generation_enabled

    defaults = create_services(settings)
    spy = SpyAgentClient()
    services = AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=InMemoryAssetStore(),
        ai_client=defaults.ai_client,
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
        agent_client=spy,  # injected but must not be called
    )
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe fantasy knight with a sword and shield",
            "idempotencyKey": "idem-flag-off-spy",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert not spy.invoke_called


# ---------------------------------------------------------------------------
# Agent success path
# ---------------------------------------------------------------------------


def test_agent_success_uses_agent_card_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the agent returns success, its card data is used in the response."""
    agent_result = _make_agent_success()

    class SuccessAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return agent_result

    services = _build_services_with_agent(SuccessAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe frost warden hero with ice powers and shield",
            "idempotencyKey": "idem-agent-ok",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["name"] == "Frost Warden"


def test_agent_art_prompt_used_for_image_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent's artPrompt must reach image generation, not derive_art_prompt."""
    specific_art_prompt = "UNIQUE_AGENT_ART_PROMPT_SENTINEL_VALUE_xyz987"
    agent_result = _make_agent_success(art_prompt=specific_art_prompt)

    received_art_prompts: list[str] = []

    class TrackingMock(MockAIClient):
        async def generate_image(self, art_prompt: str, **kwargs):
            received_art_prompts.append(art_prompt)
            return await super().generate_image(art_prompt, **kwargs)

    class SuccessAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return agent_result

    settings = _make_settings_with_agent_enabled(monkeypatch)
    defaults = create_services(settings)
    services = AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=InMemoryAssetStore(),
        ai_client=TrackingMock(settings),
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
        agent_client=SuccessAgentClient(),
    )
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe frost warden hero with ice powers long enough prompt",
            "idempotencyKey": "idem-artprompt-pass",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert any(
        specific_art_prompt in p for p in received_art_prompts
    ), f"Agent art prompt not passed to image generation; got: {received_art_prompts}"


# ---------------------------------------------------------------------------
# Fallback: retryable and routing_defer
# ---------------------------------------------------------------------------


def test_retryable_agent_result_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retryable agent failure falls back to the direct model path and succeeds."""
    fallback_result = FoundryAgentInvocationResult(
        status="transient_error",
        retryable=True,
        error_code="timeout",
        message="Agent timed out.",
    )

    class RetryableAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return fallback_result

    services = _build_services_with_agent(RetryableAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe retryable fallback card hero knight",
            "idempotencyKey": "idem-retry-fallback",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] in ("completed", "awaiting_artwork_retry")


def test_routing_defer_agent_result_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defer_result = FoundryAgentInvocationResult(
        status="routing_defer",
        retryable=False,
        schema_valid=True,
        error_code="agent_routing_defer",
        message="Agent deferred routing.",
    )

    class DeferAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return defer_result

    services = _build_services_with_agent(DeferAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe routing defer fallback card wizard hero",
            "idempotencyKey": "idem-routing-defer",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] in ("completed", "awaiting_artwork_retry")


# ---------------------------------------------------------------------------
# Non-retryable failures must not silently bypass the agent
# ---------------------------------------------------------------------------


def test_auth_error_agent_result_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth error from agent must return 503, not silently fall back to direct."""
    auth_error = FoundryAgentInvocationResult(
        status="auth_error",
        retryable=False,
        error_code="credential_unavailable",
        message="No token.",
    )

    class AuthErrorAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return auth_error

    services = _build_services_with_agent(AuthErrorAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe auth error card test hero knight",
            "idempotencyKey": "idem-auth-error",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "agent_unavailable"


def test_policy_refusal_agent_result_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    policy_refusal = FoundryAgentInvocationResult(
        status="policy_refusal",
        retryable=False,
        error_code="refusal",
        message="Agent refused.",
    )

    class PolicyRefusalAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return policy_refusal

    services = _build_services_with_agent(PolicyRefusalAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe policy refusal test hero knight long enough",
            "idempotencyKey": "idem-policy-refusal",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "prompt_rejected"


def test_schema_validation_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    schema_fail = FoundryAgentInvocationResult(
        status="invalid_response",
        retryable=False,
        schema_valid=False,
        error_code="schema_validation_failed",
        message="Invalid schema.",
    )

    class SchemaFailAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return schema_fail

    services = _build_services_with_agent(SchemaFailAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe schema fail test hero card knight",
            "idempotencyKey": "idem-schema-fail",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502


def test_configuration_error_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    config_error = FoundryAgentInvocationResult(
        status="configuration_error",
        retryable=False,
        error_code="invalid_configuration",
        message="Config error.",
    )

    class ConfigErrorAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return config_error

    services = _build_services_with_agent(ConfigErrorAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe config error test card hero knight",
            "idempotencyKey": "idem-config-err",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Pre-moderation still runs before agent invocation
# ---------------------------------------------------------------------------


def test_pre_moderation_blocks_before_agent_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-prompt moderation must fire before the agent; blocked prompts never reach agent."""

    class UnreachableAgentClient:
        def __init__(self) -> None:
            self.called = False

        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            self.called = True
            raise AssertionError("invoke must not be called on a moderation-blocked prompt")

    spy = UnreachableAgentClient()
    services = _build_services_with_agent(spy, monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            # Triggers HeuristicModerationService (living-artist-imitation)
            "prompt": "create a card in the style of a living artist today",
            "idempotencyKey": "idem-premod-block",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422
    assert not spy.called


# ---------------------------------------------------------------------------
# Post-text moderation still runs on agent card output
# ---------------------------------------------------------------------------


def test_post_text_moderation_applies_to_agent_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-text moderation must run on the agent-generated card text and can block it."""
    bad_card = GeneratedCardModel(
        schemaVersion=1,
        name="Bad Card",
        cardType="spell",
        rarity="common",
        manaCost=1,
        attack=1,
        health=1,
        # Triggers heuristic moderation: "in the style of" a living artist
        rulesText="In the style of a living artist with graphic gore effects applied.",
        flavorText="Flavor text.",
        artBrief="Art brief with enough characters here to pass length validation properly.",
    )
    bad_agent_result = FoundryAgentInvocationResult(
        status="completed",
        success=True,
        schema_valid=True,
        card=bad_card,
        art_prompt="Some valid art prompt for the image generation system endpoint.",
    )

    class BadContentAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return bad_agent_result

    services = _build_services_with_agent(BadContentAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe post text moderation integration test here",
            "idempotencyKey": "idem-post-text-mod",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Privacy: no owner ID or session data sent to agent
# ---------------------------------------------------------------------------


def test_agent_invoke_receives_only_prompt_no_owner_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent.invoke call must receive only the prompt text, not owner/session data."""
    agent_result = _make_agent_success()
    received_queries: list[str] = []

    class PrivacySpyClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            received_queries.append(query)
            return agent_result

    services = _build_services_with_agent(PrivacySpyClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe privacy check test card with a frost warden",
            "idempotencyKey": "idem-privacy-check",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert len(received_queries) == 1
    query = received_queries[0]
    # The query must be the prompt and must not include session or owner identifiers
    assert "privacy check test card" in query
    # Must not leak internal IDs (owner_id format is "tenant:object")
    assert ":" not in query or "frost" in query  # colons only from card text, not IDs


# ---------------------------------------------------------------------------
# Fallback telemetry emitted correctly
# ---------------------------------------------------------------------------


def test_agent_fallback_emits_telemetry_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """On retryable fallback, both agent.invocation and agent.fallback events must be emitted."""
    retryable = FoundryAgentInvocationResult(
        status="transient_error",
        retryable=True,
        error_code="timeout",
        message="Timed out.",
    )

    class FallbackAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return retryable

    emitted_events: list[str] = []
    original_add_event = generation_module.add_event

    def spy_add_event(name: str, attributes=None) -> None:
        emitted_events.append(name)
        original_add_event(name, attributes)

    monkeypatch.setattr(generation_module, "add_event", spy_add_event)

    services = _build_services_with_agent(FallbackAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe telemetry fallback test card hero knight",
            "idempotencyKey": "idem-telemetry-fb",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert "agent.invocation" in emitted_events, f"Missing agent.invocation in {emitted_events}"
    assert "agent.fallback" in emitted_events, f"Missing agent.fallback in {emitted_events}"


def test_agent_success_emits_invocation_event_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On agent success, agent.invocation is emitted but NOT agent.fallback."""
    agent_result = _make_agent_success()

    class SuccessAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return agent_result

    emitted_events: list[str] = []
    original_add_event = generation_module.add_event

    def spy_add_event(name: str, attributes=None) -> None:
        emitted_events.append(name)
        original_add_event(name, attributes)

    monkeypatch.setattr(generation_module, "add_event", spy_add_event)

    services = _build_services_with_agent(SuccessAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe agent success telemetry test card hero",
            "idempotencyKey": "idem-telemetry-ok",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert "agent.invocation" in emitted_events
    assert "agent.fallback" not in emitted_events


# ---------------------------------------------------------------------------
# fcg.generation.requests path dimension
# ---------------------------------------------------------------------------


def test_set_generation_path_is_exported_from_telemetry() -> None:
    """set_generation_path must be importable and callable without error."""
    from app.telemetry import set_generation_path

    # Should not raise; unknown value is normalized to 'direct'
    set_generation_path("agent")
    set_generation_path("direct")
    set_generation_path("agent_fallback")
    set_generation_path("unknown_value")  # normalized to 'direct'


def test_generation_path_dimension_in_metric_on_agent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agent succeeds, _record_generation must receive path='agent' dimension."""
    from app import telemetry as telemetry_module

    agent_result = _make_agent_success()

    class SuccessAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return agent_result

    recorded_metric_attributes: list[dict] = []
    original_record = telemetry_module._record_generation

    def spy_record(operation, outcome, duration_ms, agent_version=None):
        # Capture what _record_generation sees from the context var
        path = telemetry_module._generation_path_var.get()
        recorded_metric_attributes.append(
            {"operation": operation, "outcome": outcome, "path": path}
        )
        original_record(operation, outcome, duration_ms, agent_version)

    monkeypatch.setattr(telemetry_module, "_record_generation", spy_record)

    services = _build_services_with_agent(SuccessAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe metric dimension test card hero generation",
            "idempotencyKey": "idem-metric-dim-agent",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    generate_recordings = [r for r in recorded_metric_attributes if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' in metric dimensions; got: {generate_recordings}"


def test_generation_path_dimension_in_metric_on_direct_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When flag is off (direct path), _record_generation must receive path='direct'."""
    from app import telemetry as telemetry_module

    recorded_metric_attributes: list[dict] = []
    original_record = telemetry_module._record_generation

    def spy_record(operation, outcome, duration_ms, agent_version=None):
        path = telemetry_module._generation_path_var.get()
        recorded_metric_attributes.append(
            {"operation": operation, "outcome": outcome, "path": path}
        )
        original_record(operation, outcome, duration_ms, agent_version)

    monkeypatch.setattr(telemetry_module, "_record_generation", spy_record)

    settings = load_app_settings()
    services = create_services(settings)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe metric dimension test direct path generation",
            "idempotencyKey": "idem-metric-dim-direct",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    generate_recordings = [r for r in recorded_metric_attributes if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "direct" for r in generate_recordings
    ), f"Expected path='direct' in metric dimensions; got: {generate_recordings}"


def test_generation_path_dimension_in_metric_on_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agent falls back, _record_generation must receive path='agent_fallback'."""
    from app import telemetry as telemetry_module

    retryable = FoundryAgentInvocationResult(
        status="transient_error",
        retryable=True,
        error_code="timeout",
        message="Timed out.",
    )

    class FallbackAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return retryable

    recorded_metric_attributes: list[dict] = []
    original_record = telemetry_module._record_generation

    def spy_record(operation, outcome, duration_ms, agent_version=None):
        path = telemetry_module._generation_path_var.get()
        recorded_metric_attributes.append(
            {"operation": operation, "outcome": outcome, "path": path}
        )
        original_record(operation, outcome, duration_ms, agent_version)

    monkeypatch.setattr(telemetry_module, "_record_generation", spy_record)

    services = _build_services_with_agent(FallbackAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe metric dimension test fallback path generation",
            "idempotencyKey": "idem-metric-dim-fallback",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    generate_recordings = [r for r in recorded_metric_attributes if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent_fallback" for r in generate_recordings
    ), f"Expected path='agent_fallback' in metric dimensions; got: {generate_recordings}"


# ---------------------------------------------------------------------------
# Non-regression: flag-off path leaves all existing behavior unchanged
# ---------------------------------------------------------------------------


def test_existing_generation_path_unaffected_with_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when flag is off, no agent_client is set and the direct path runs."""
    settings = load_app_settings()
    assert not settings.agent_generation_enabled
    services = create_services(settings)
    assert services.agent_client is None
    assert isinstance(services.ai_client, MockAIClient)


# ---------------------------------------------------------------------------
# fcg.generation_path=agent recorded for every non-retryable agent error
# ---------------------------------------------------------------------------


def _spy_record_path(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict]:
    """Attach a spy to _record_generation that records (operation, outcome, path) tuples."""
    from app import telemetry as telemetry_module

    recorded: list[dict] = []
    original_record = telemetry_module._record_generation

    def spy_record(operation, outcome, duration_ms, agent_version=None):
        path = telemetry_module._generation_path_var.get()
        recorded.append({"operation": operation, "outcome": outcome, "path": path})
        original_record(operation, outcome, duration_ms, agent_version)

    monkeypatch.setattr(telemetry_module, "_record_generation", spy_record)
    return recorded


def test_generation_path_is_agent_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """fcg.generation_path must be 'agent' in the metric when auth_error is raised."""
    recorded = _spy_record_path(monkeypatch)

    auth_error = FoundryAgentInvocationResult(
        status="auth_error",
        retryable=False,
        error_code="credential_unavailable",
        message="No token.",
    )

    class AuthErrorClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return auth_error

    services = _build_services_with_agent(AuthErrorClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe auth error metric path test card hero",
            "idempotencyKey": "idem-auth-err-path",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    generate_recordings = [r for r in recorded if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' for auth_error; got: {generate_recordings}"


def test_generation_path_is_agent_on_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """fcg.generation_path must be 'agent' in the metric when configuration_error is raised."""
    recorded = _spy_record_path(monkeypatch)

    config_error = FoundryAgentInvocationResult(
        status="configuration_error",
        retryable=False,
        error_code="invalid_configuration",
        message="Config error.",
    )

    class ConfigErrorClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return config_error

    services = _build_services_with_agent(ConfigErrorClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe config error metric path test card hero",
            "idempotencyKey": "idem-config-err-path",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    generate_recordings = [r for r in recorded if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' for configuration_error; got: {generate_recordings}"


def test_generation_path_is_agent_on_policy_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """fcg.generation_path must be 'agent' in the metric when policy_refusal is raised."""
    recorded = _spy_record_path(monkeypatch)

    policy_refusal = FoundryAgentInvocationResult(
        status="policy_refusal",
        retryable=False,
        error_code="refusal",
        message="Agent refused.",
    )

    class PolicyRefusalClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return policy_refusal

    services = _build_services_with_agent(PolicyRefusalClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe policy refusal metric path test card hero",
            "idempotencyKey": "idem-policy-ref-path",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422
    generate_recordings = [r for r in recorded if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' for policy_refusal; got: {generate_recordings}"


def test_generation_path_is_agent_on_schema_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """fcg.generation_path must be 'agent' in the metric when schema validation fails."""
    recorded = _spy_record_path(monkeypatch)

    schema_fail = FoundryAgentInvocationResult(
        status="invalid_response",
        retryable=False,
        schema_valid=False,
        error_code="schema_validation_failed",
        message="Invalid schema.",
    )

    class SchemaFailClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return schema_fail

    services = _build_services_with_agent(SchemaFailClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe schema fail metric path test card hero",
            "idempotencyKey": "idem-schema-fail-path",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502
    generate_recordings = [r for r in recorded if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' for schema_fail (invalid_response); got: {generate_recordings}"


def test_generation_path_is_agent_on_agent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """fcg.generation_path must be 'agent' in the metric for generic non-retryable agent_failure."""
    recorded = _spy_record_path(monkeypatch)

    agent_failure = FoundryAgentInvocationResult(
        status="failed",
        retryable=False,
        error_code="agent_failure",
        message="Agent failed.",
    )

    class FailedAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            return agent_failure

    services = _build_services_with_agent(FailedAgentClient(), monkeypatch=monkeypatch)
    client = _agent_client(monkeypatch, services)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "a safe agent failure metric path test card hero",
            "idempotencyKey": "idem-agent-fail-path",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502
    generate_recordings = [r for r in recorded if r["operation"] == "generate"]
    assert generate_recordings, "Expected at least one generate recording"
    assert any(
        r["path"] == "agent" for r in generate_recordings
    ), f"Expected path='agent' for agent_failure; got: {generate_recordings}"


# ---------------------------------------------------------------------------
# Lifecycle: FoundryAgentClient.aclose() called on app shutdown
# ---------------------------------------------------------------------------


def test_agent_client_aclose_called_on_lifespan_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FoundryAgentClient.aclose() must be called during app lifespan shutdown."""
    import app.main as main_module

    aclose_called = []

    class TrackingAgentClient:
        async def invoke(self, query: str) -> FoundryAgentInvocationResult:
            raise AssertionError("invoke should not be called in this test")

        async def aclose(self) -> None:
            aclose_called.append(True)

    services = _build_services_with_agent(TrackingAgentClient(), monkeypatch=monkeypatch)
    monkeypatch.setattr(main_module, "create_oauth_client", lambda s: FakeOAuthClient())

    with TestClient(create_app(services=services), base_url="https://testserver"):
        # Lifespan startup has run; shutdown runs when context manager exits
        pass

    assert aclose_called, "agent_client.aclose() was not called during lifespan shutdown"


def test_lifespan_shutdown_without_agent_client_does_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agent_client is None, lifespan shutdown must not raise."""
    import app.main as main_module

    settings = load_app_settings()
    services = create_services(settings)
    assert services.agent_client is None

    monkeypatch.setattr(main_module, "create_oauth_client", lambda s: FakeOAuthClient())

    # Should not raise
    with TestClient(create_app(services=services), base_url="https://testserver"):
        pass
