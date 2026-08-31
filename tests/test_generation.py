from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import generation as generation_module
from app.generation import (
    AppServices,
    AuthenticatedOwner,
    AzureFoundryAIClient,
    CardGenerationService,
    InMemoryAssetStore,
    InMemoryAuditRepository,
    InMemoryCardRepository,
    InMemorySharedCardAuditRepository,
    MockAIClient,
    StoredCard,
    UpstreamServiceError,
    client_ip_from_request,
    create_services,
)
from app.main import create_app
from app.problems import ProblemDetails
from app.settings import load_app_settings
from tests.conftest import TEST_OWNER_ID, extract_hidden_value, make_authenticated_client


def test_app_shell_renders_generation_form(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/app")

    assert response.status_code == 200
    assert 'hx-post="/ui/cards/generate"' in response.text
    assert 'name="csrf_token"' in response.text
    assert 'name="idempotency_key"' in response.text


def test_api_requires_authentication() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.post(
        "/api/v1/cards/generate",
        json={"prompt": "create a safe fantasy knight"},
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errorCode"] == "unauthorized"


def test_api_rejects_missing_csrf(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={"prompt": "create a safe fantasy knight", "idempotencyKey": "idem-auth-a"},
    )

    assert response.status_code == 403
    assert response.json()["errorCode"] == "csrf_failed"


def test_generate_card_success_persists_card_and_private_asset(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-success",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["imageUrl"].startswith("/cards/")
    assert payload["requestId"] == response.headers["x-request-id"]

    image_response = authenticated_client.get(payload["imageUrl"])
    assert image_response.status_code == 200
    assert image_response.headers["content-type"].startswith("image/png")
    assert image_response.content.startswith(b"\x89PNG")

    services = authenticated_client.app.state.services
    card = services.card_repository._records.get((payload["ownerId"], payload["cardId"]))
    assert card is not None
    assert card.status == "completed"
    assert card.prompt == "create a safe fantasy knight with a moonlit shield"
    assert card.blob_name is not None
    assert "sas" not in str(card.to_document()).lower()


def test_card_document_omits_ttl_when_no_item_expiry_is_intended() -> None:
    card = StoredCard(
        id="card-123",
        document_type="card",
        owner_id=TEST_OWNER_ID,
        request_id="req-card-ttl",
        idempotency_key="idem-card-ttl",
        request_hash="hash-card-ttl",
        status="processing",
    )

    document = card.to_document()

    assert "ttl" not in document


def test_audit_document_serializes_positive_ttl() -> None:
    ttl_seconds = 30 * 24 * 60 * 60
    audit = StoredCard(
        id="card-123",
        document_type="generation-audit",
        owner_id=TEST_OWNER_ID,
        request_id="req-audit-ttl",
        idempotency_key="idem-audit-ttl",
        request_hash="hash-audit-ttl",
        status="audit_processing",
        ttl_seconds=ttl_seconds,
    )

    document = audit.to_document()

    assert document["ttl"] == ttl_seconds


def test_audit_reservation_uses_retention_ttl() -> None:
    ttl_seconds = 30 * 24 * 60 * 60
    repository = InMemoryAuditRepository(audit_ttl_seconds=ttl_seconds)

    reserved, created = asyncio.run(
        repository.reserve_audit(
            owner_id=TEST_OWNER_ID,
            card_id="card-123",
            request_hash="hash-audit-reservation",
            idempotency_key="idem-audit-reservation",
            request_id="req-audit-reservation",
        )
    )

    assert created is True
    assert reserved is not None
    assert reserved.ttl_seconds == ttl_seconds
    assert reserved.to_document()["ttl"] == ttl_seconds


def test_live_mode_foundry_client_sends_real_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")

    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"name":"Knight of Dawn","cardType":"hero","rarity":"rare",'
                                '"manaCost":4,"attack":5,"health":4,'
                                '"rulesText":"Charge into the dawning light.",'
                                '"flavorText":"Dawn follows.",'
                                '"artBrief":"A radiant knight raising a silver shield.",'
                                '"schemaVersion":1}'
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def build_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(generation_module.httpx, "AsyncClient", build_client)
    client = AzureFoundryAIClient(load_app_settings())
    client._credential = SimpleNamespace(  # type: ignore[attr-defined]
        get_token=lambda *_args, **_kwargs: SimpleNamespace(token="live-access-token")
    )

    result = asyncio.run(
        client.generate_card(
            "create a safe fantasy knight with a moonlit shield",
            request_id="req-live",
        )
    )

    assert captured["authorization"] == "Bearer live-access-token"
    assert captured["url"] == (
        "https://foundry.example/openai/deployments/gpt-5-5/chat/completions"
        "?api-version=2025-03-01-preview"
    )
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "temperature" not in payload
    response_format = payload["response_format"]
    schema = response_format["json_schema"]["schema"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert set(schema["required"]) == set(schema["properties"])
    assert "schemaVersion" in schema["required"]
    assert result.metadata.mode == "live"


def test_foundry_400_raises_sanitized_azure_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")

    sensitive_prompt = "private prompt that must not be logged"
    sensitive_output = "private output that must not be logged"
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "unsupported_value",
                    "message": (
                        "Unsupported value: 'temperature' does not support 0.2 "
                        "with this model.\nOnly the default (1) value is supported. "
                        f"Rejected prompt: {sensitive_prompt}"
                    ),
                    "param": "temperature",
                    "type": "invalid_request_error",
                },
                "prompt": sensitive_prompt,
                "output": sensitive_output,
                "access_token": "sensitive-token",
            },
            request=request,
        )

    def build_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(generation_module.httpx, "AsyncClient", build_client)
    client = AzureFoundryAIClient(load_app_settings())
    client._credential = SimpleNamespace(  # type: ignore[attr-defined]
        get_token=lambda *_args, **_kwargs: SimpleNamespace(token="live-access-token")
    )

    with pytest.raises(UpstreamServiceError) as raised:
        asyncio.run(
            client._post(
                "/openai/deployments/gpt-5-5/chat/completions",
                {"prompt": sensitive_prompt},
                api_version="2025-03-01-preview",
                service_name="foundry-text",
            )
        )

    error = raised.value
    assert error.status_code == 400
    assert error.retryable is False
    assert error.error_code == "unsupported_value"
    assert error.diagnostic_message == (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported. Rejected prompt: <redacted>"
    )
    assert sensitive_prompt not in str(error)
    assert sensitive_output not in str(error)
    assert "sensitive-token" not in str(error)


def test_rate_limits_ignore_untrusted_forwarded_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_USER_REQUESTS", "5")
    monkeypatch.setenv("RATE_LIMIT_IP_REQUESTS", "1")
    client = make_authenticated_client(monkeypatch)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    first = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight with a silver banner",
            "idempotencyKey": "idem-limit-1",
            "csrfToken": csrf_token,
        },
    )
    second = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy druid with a crystal branch",
            "idempotencyKey": "idem-limit-2",
            "csrfToken": csrf_token,
        },
        headers={"x-forwarded-for": "203.0.113.10"},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"


def test_trusted_proxy_uses_rightmost_forwarded_hop() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/cards/generate",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.10, 198.51.100.7"),
                (b"x-forwarded-proto", b"https"),
            ],
            "client": ("10.0.0.4", 443),
        }
    )

    assert client_ip_from_request(request, trusted_proxy_hops=1) == "198.51.100.7"


def test_pre_moderation_rejection_records_sanitized_audit(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")
    prompt = "create a fantasy hero in the style of a living artist"

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": prompt,
            "idempotencyKey": "idem-pre-block",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "prompt_rejected"

    services = authenticated_client.app.state.services
    assert prompt not in response.text
    assert len(services.audit_repository._records) == 1
    audit = next(iter(services.audit_repository._records.values()))
    assert audit is not None
    assert audit.prompt is None
    assert audit.ttl_seconds == 30 * 24 * 60 * 60
    assert audit.error_code == "living-artist-imitation"
    assert services.card_repository._records == {}


def test_invalid_model_output_is_rejected_and_not_persisted(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight [[mock:text-invalid-extra]]",
            "idempotencyKey": "idem-invalid-extra",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502
    assert response.json()["errorCode"] == "invalid_model_output"


def test_model_bounds_violation_is_rejected(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight [[mock:text-invalid-bounds]]",
            "idempotencyKey": "idem-invalid-bounds",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502
    assert response.json()["errorCode"] == "invalid_model_output"


def test_retryable_text_upstream_failure_is_retried_successfully(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:text-429-once]]",
            "idempotencyKey": "idem-text-retry",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_non_retryable_text_upstream_failure_returns_bad_gateway(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:text-500]]",
            "idempotencyKey": "idem-text-500",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 502
    assert response.json()["errorCode"] == "upstream_failure"


def test_upstream_timeout_returns_gateway_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "0")
    client = make_authenticated_client(monkeypatch)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:text-timeout]]",
            "idempotencyKey": "idem-timeout",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 504
    assert response.json()["errorCode"] == "upstream_timeout"


def test_idempotent_replay_returns_same_completed_card(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")
    payload = {
        "prompt": "create a safe fantasy knight with a radiant crown",
        "idempotencyKey": "idem-replay",
        "csrfToken": csrf_token,
    }

    first = authenticated_client.post("/api/v1/cards/generate", json=payload)
    second = authenticated_client.post("/api/v1/cards/generate", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cardId"] == second.json()["cardId"]
    assert first.json()["imageUrl"] == second.json()["imageUrl"]


def test_idempotency_key_conflict_rejects_different_prompt(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")
    idem_key = "idem-conflict"
    first = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotencyKey": idem_key,
            "csrfToken": csrf_token,
        },
    )
    second = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy druid with a radiant crown",
            "idempotencyKey": idem_key,
            "csrfToken": csrf_token,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["errorCode"] == "idempotency_conflict"


def test_image_failure_returns_partial_card_and_retry_action(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight [[mock:image-500]]",
            "idempotencyKey": "idem-image-failure",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "awaiting_artwork_retry"
    assert payload["imageUrl"] is None
    assert payload["actions"][0]["type"] == "retry_artwork"


def test_ui_overall_timeout_during_image_returns_partial_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("IMAGE_MAX_RETRIES", "0")
    client = make_authenticated_client(monkeypatch)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight [[mock:image-timeout]]",
            "idempotency_key": "idem-ui-image-timeout",
            "csrf_token": csrf_token,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "Artwork is not ready yet" in response.text
    assert "Retry artwork" in response.text


def test_retry_artwork_completes_partial_card(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")
    initial = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight [[mock:image-500]]",
            "idempotencyKey": "idem-art-retry-start",
            "csrfToken": csrf_token,
        },
    )
    assert initial.status_code == 200
    assert initial.json()["status"] == "awaiting_artwork_retry"

    services = authenticated_client.app.state.services
    record = services.card_repository._records.get(
        (initial.json()["ownerId"], initial.json()["cardId"])
    )
    assert record is not None
    record.derived_art_prompt = record.derived_art_prompt.replace("[[mock:image-500]]", "")
    asyncio.run(services.card_repository.save(record))

    retry = authenticated_client.post(
        f"/api/v1/cards/{initial.json()['cardId']}/artwork/retry",
        json={"idempotencyKey": "idem-art-retry-finish", "csrfToken": csrf_token},
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "completed"
    assert retry.json()["imageUrl"]


def test_htmx_flow_renders_error_panel(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a fantasy hero in the style of a living artist",
            "idempotency_key": "idem-ui-block",
            "csrf_token": csrf_token,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 422
    assert "error-panel" in response.text
    assert "Prompt Rejected" in response.text


def test_persistence_cleanup_deletes_orphaned_blob(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingCardRepository(InMemoryCardRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail_completed_save = True

        async def save(self, record: StoredCard) -> StoredCard:
            if record.status == "completed" and self.fail_completed_save:
                raise RuntimeError("cosmos unavailable")
            return await super().save(record)

    class TrackingAssetStore(InMemoryAssetStore):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        async def delete(self, blob_name: str) -> None:
            self.deleted.append(blob_name)
            await super().delete(blob_name)

    settings = load_app_settings()
    defaults = create_services(settings)
    services = AppServices(
        settings=settings,
        card_repository=FailingCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=TrackingAssetStore(),
        ai_client=MockAIClient(settings),
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
    )
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy guardian with emerald armor",
            "idempotencyKey": "idem-persist-fail",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "persistence_failure"
    assert services.asset_store.deleted
    assert services.card_repository._records == {}
    assert "stage=cosmos-write" in caplog.text
    assert "stage=compensation-delete" in caplog.text


def test_blob_upload_failure_logs_safe_azure_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BlobAuthorizationError(RuntimeError):
        status_code = 403
        error_code = "AuthorizationFailure"

    class FailingAssetStore(InMemoryAssetStore):
        async def upload(
            self,
            blob_name: str,
            payload: bytes,
            content_type: str,
        ) -> dict[str, object]:
            raise BlobAuthorizationError("sensitive SAS URL and image bytes")

    settings = load_app_settings()
    defaults = create_services(settings)
    services = AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=FailingAssetStore(),
        ai_client=MockAIClient(settings),
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
    )
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy guardian with a golden shield",
            "idempotencyKey": "idem-blob-auth-fail",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    assert services.card_repository._records == {}
    assert "stage=blob-upload" in caplog.text
    assert "azure_status=403" in caplog.text
    assert "azure_error_code=AuthorizationFailure" in caplog.text
    assert "sensitive SAS URL" not in caplog.text


def test_failed_blob_compensation_does_not_mask_persistence_problem(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingCardRepository(InMemoryCardRepository):
        async def save(self, record: StoredCard) -> StoredCard:
            if record.status == "completed":
                raise RuntimeError("cosmos unavailable")
            return await super().save(record)

    class CleanupAuthorizationError(RuntimeError):
        status_code = 403
        error_code = "AuthorizationFailure"

    class FailingCleanupAssetStore(InMemoryAssetStore):
        async def delete(self, blob_name: str) -> None:
            raise CleanupAuthorizationError("sensitive cleanup detail")

    settings = load_app_settings()
    defaults = create_services(settings)
    services = AppServices(
        settings=settings,
        card_repository=FailingCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=FailingCleanupAssetStore(),
        ai_client=MockAIClient(settings),
        moderation_service=defaults.moderation_service,
        rate_limiter=defaults.rate_limiter,
        csrf_protector=defaults.csrf_protector,
    )
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy guardian with a sapphire spear",
            "idempotencyKey": "idem-cleanup-fail",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "persistence_failure"
    assert services.card_repository._records == {}
    assert "event" not in response.text
    assert "compensation-failed" in caplog.text
    assert "stage=compensation-delete" in caplog.text
    assert "azure_status=403" in caplog.text
    assert "sensitive cleanup detail" not in caplog.text


def test_concurrent_duplicates_do_not_duplicate_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowMockAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.text_calls = 0
            self.image_calls = 0

        async def generate_card(self, prompt: str, *, request_id: str):
            self.text_calls += 1
            await asyncio.sleep(0.45)
            return await super().generate_card(prompt, request_id=request_id)

        async def generate_image(self, art_prompt: str, *, request_id: str):
            self.image_calls += 1
            return await super().generate_image(art_prompt, request_id=request_id)

    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "1.6")

    async def run_concurrency() -> tuple[list[dict[str, str]], int, int]:
        settings = load_app_settings()
        ai_client = SlowMockAIClient(settings)
        defaults = create_services(settings)
        services = AppServices(
            settings=settings,
            card_repository=InMemoryCardRepository(),
            audit_repository=InMemoryAuditRepository(),
            asset_store=InMemoryAssetStore(),
            ai_client=ai_client,
            moderation_service=defaults.moderation_service,
            rate_limiter=defaults.rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        service = CardGenerationService(services)
        owner = AuthenticatedOwner(
            owner_id=TEST_OWNER_ID,
            tenant_id=None,
            object_id=None,
            subject="sub",
            display_name="Aragorn",
            email="aragorn@example.com",
        )
        results = await asyncio.gather(
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-concurrent",
                request_id="req-1",
                client_ip="127.0.0.1",
            ),
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-concurrent",
                request_id="req-2",
                client_ip="127.0.0.1",
            ),
        )
        return (
            [result.model_dump() for result in results],
            ai_client.text_calls,
            ai_client.image_calls,
        )

    results, text_calls, image_calls = asyncio.run(run_concurrency())

    assert results[0]["cardId"] == results[1]["cardId"]
    assert text_calls == 1
    assert image_calls == 1


def test_concurrent_duplicate_replays_rate_limit_rejection_instead_of_timing_out() -> None:
    class BlockingRateLimiter:
        def __init__(self) -> None:
            self.first_request_reserved = asyncio.Event()
            self.release_first_request = asyncio.Event()
            self.calls = 0

        async def enforce(self, key: str, _settings, *, error_suffix: str) -> None:
            self.calls += 1
            if self.calls == 1:
                self.first_request_reserved.set()
                await self.release_first_request.wait()
                raise ProblemDetails(
                    status_code=429,
                    title="Too Many Requests",
                    detail=(f"Rate limit exceeded for {error_suffix}. Retry after " "60 seconds."),
                    type="/problems/rate-limit",
                    error_code="rate_limit_exceeded",
                    headers={"Retry-After": "60"},
                )

    async def run_concurrency() -> tuple[list[ProblemDetails], InMemoryAuditRepository]:
        settings = load_app_settings()
        defaults = create_services(settings)
        audit_repository = InMemoryAuditRepository()
        rate_limiter = BlockingRateLimiter()
        services = AppServices(
            settings=settings,
            card_repository=InMemoryCardRepository(),
            audit_repository=audit_repository,
            asset_store=InMemoryAssetStore(),
            ai_client=MockAIClient(settings),
            moderation_service=defaults.moderation_service,
            rate_limiter=rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        service = CardGenerationService(services)
        owner = AuthenticatedOwner(
            owner_id=TEST_OWNER_ID,
            tenant_id=None,
            object_id=None,
            subject="sub",
            display_name="Legolas",
            email="legolas@example.com",
        )

        first = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-concurrent-rate-limit",
                request_id="req-rate-limit-1",
                client_ip="127.0.0.1",
            )
        )
        await rate_limiter.first_request_reserved.wait()

        second = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-concurrent-rate-limit",
                request_id="req-rate-limit-2",
                client_ip="127.0.0.1",
            )
        )

        await asyncio.sleep(0.1)
        rate_limiter.release_first_request.set()

        results = await asyncio.gather(first, second, return_exceptions=True)
        problems = [result for result in results if isinstance(result, ProblemDetails)]
        return problems, audit_repository

    problems, audit_repository = asyncio.run(run_concurrency())

    assert len(problems) == 2
    assert [problem.status_code for problem in problems] == [429, 429]
    assert [problem.error_code for problem in problems] == [
        "rate_limit_exceeded",
        "rate_limit_exceeded",
    ]
    assert [problem.headers.get("Retry-After") for problem in problems] == ["60", "60"]

    audit = next(iter(audit_repository._records.values()))
    assert audit.status == "audit_failed"
    assert audit.failure_status_code == 429
    assert audit.failure_headers == {"Retry-After": "60"}


def test_shared_repository_topology_preserves_audit_for_duplicate_rate_limit_replay() -> None:
    class BlockingRateLimiter:
        def __init__(self) -> None:
            self.first_request_reserved = asyncio.Event()
            self.release_first_request = asyncio.Event()
            self.calls = 0

        async def enforce(self, key: str, _settings, *, error_suffix: str) -> None:
            self.calls += 1
            if self.calls == 1:
                self.first_request_reserved.set()
                await self.release_first_request.wait()
                raise ProblemDetails(
                    status_code=429,
                    title="Too Many Requests",
                    detail=(f"Rate limit exceeded for {error_suffix}. Retry after " "60 seconds."),
                    type="/problems/rate-limit",
                    error_code="rate_limit_exceeded",
                    headers={"Retry-After": "60"},
                )

    async def run_concurrency() -> tuple[list[ProblemDetails], InMemorySharedCardAuditRepository]:
        settings = load_app_settings()
        defaults = create_services(settings)
        shared_repository = InMemorySharedCardAuditRepository()
        rate_limiter = BlockingRateLimiter()
        services = AppServices(
            settings=settings,
            card_repository=shared_repository,
            audit_repository=shared_repository,
            asset_store=InMemoryAssetStore(),
            ai_client=MockAIClient(settings),
            moderation_service=defaults.moderation_service,
            rate_limiter=rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        service = CardGenerationService(services)
        owner = AuthenticatedOwner(
            owner_id=TEST_OWNER_ID,
            tenant_id=None,
            object_id=None,
            subject="sub",
            display_name="Samwise",
            email="samwise@example.com",
        )

        first = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-shared-rate-limit",
                request_id="req-shared-rate-limit-1",
                client_ip="127.0.0.1",
            )
        )
        await rate_limiter.first_request_reserved.wait()

        second = asyncio.create_task(
            service.generate_card(
                owner=owner,
                prompt="create a safe fantasy knight with a moonlit shield",
                idempotency_key="idem-shared-rate-limit",
                request_id="req-shared-rate-limit-2",
                client_ip="127.0.0.1",
            )
        )

        await asyncio.sleep(0.1)
        rate_limiter.release_first_request.set()

        results = await asyncio.gather(first, second, return_exceptions=True)
        problems = [result for result in results if isinstance(result, ProblemDetails)]
        return problems, shared_repository

    problems, shared_repository = asyncio.run(run_concurrency())

    assert len(problems) == 2
    assert [problem.status_code for problem in problems] == [429, 429]
    assert [problem.error_code for problem in problems] == [
        "rate_limit_exceeded",
        "rate_limit_exceeded",
    ]
    assert [problem.headers.get("Retry-After") for problem in problems] == ["60", "60"]
    card_id = generation_module.deterministic_card_id(
        TEST_OWNER_ID,
        "idem-shared-rate-limit",
    )
    assert (TEST_OWNER_ID, f"audit:{card_id}") in shared_repository._records
    assert (TEST_OWNER_ID, f"card:{card_id}") not in shared_repository._records

    audit = asyncio.run(shared_repository.get_audit(TEST_OWNER_ID, card_id))
    assert audit is not None
    assert audit.status == "audit_failed"
    assert audit.failure_status_code == 429
    assert audit.failure_headers == {"Retry-After": "60"}
    assert asyncio.run(shared_repository.get(TEST_OWNER_ID, card_id)) is None


def test_concurrent_artwork_retries_do_not_duplicate_image_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowRetryAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.initial_image_calls = 0
            self.retry_image_calls = 0

        async def generate_image(self, art_prompt: str, *, request_id: str):
            if "[[mock:image-500]]" in art_prompt:
                self.initial_image_calls += 1
            else:
                self.retry_image_calls += 1
            await asyncio.sleep(0.45)
            return await super().generate_image(art_prompt, request_id=request_id)

    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.8")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "1.6")

    async def run_concurrency() -> tuple[list[dict[str, str]], int, int]:
        settings = load_app_settings()
        ai_client = SlowRetryAIClient(settings)
        defaults = create_services(settings)
        services = AppServices(
            settings=settings,
            card_repository=InMemoryCardRepository(),
            audit_repository=InMemoryAuditRepository(),
            asset_store=InMemoryAssetStore(),
            ai_client=ai_client,
            moderation_service=defaults.moderation_service,
            rate_limiter=defaults.rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        service = CardGenerationService(services)
        owner = AuthenticatedOwner(
            owner_id=TEST_OWNER_ID,
            tenant_id=None,
            object_id=None,
            subject="sub",
            display_name="Gimli",
            email="gimli@example.com",
        )
        partial = await service.generate_card(
            owner=owner,
            prompt="create a safe fantasy knight [[mock:image-500]]",
            idempotency_key="idem-artwork-partial",
            request_id="req-partial",
            client_ip="127.0.0.1",
        )
        record = await services.card_repository.get(owner.owner_id, partial.cardId)
        assert record is not None
        record.derived_art_prompt = record.derived_art_prompt.replace("[[mock:image-500]]", "")
        await services.card_repository.save(record)

        results = await asyncio.gather(
            service.retry_artwork(
                owner=owner,
                card_id=partial.cardId,
                idempotency_key="idem-artwork-retry",
                request_id="req-retry-1",
                client_ip="127.0.0.1",
            ),
            service.retry_artwork(
                owner=owner,
                card_id=partial.cardId,
                idempotency_key="idem-artwork-retry",
                request_id="req-retry-2",
                client_ip="127.0.0.1",
            ),
        )
        return (
            [result.model_dump() for result in results],
            ai_client.initial_image_calls,
            ai_client.retry_image_calls,
        )

    results, initial_image_calls, retry_image_calls = asyncio.run(run_concurrency())

    assert results[0]["cardId"] == results[1]["cardId"]
    assert results[0]["status"] == "completed"
    assert results[1]["status"] == "completed"
    assert initial_image_calls == 1
    assert retry_image_calls == 1
