from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import generation as generation_module
from app.generation import (
    AppServices,
    AuthenticatedOwner,
    AzureFoundryAIClient,
    CardGenerateBody,
    CardGenerationService,
    InMemoryAssetStore,
    InMemoryAuditRepository,
    InMemoryCardRepository,
    InMemorySharedCardAuditRepository,
    MockAIClient,
    ReferenceImageUpload,
    StoredCard,
    UpstreamServiceError,
    client_ip_from_request,
    create_services,
)
from app.main import create_app
from app.problems import ProblemDetails
from app.settings import SettingsError, load_app_settings
from tests.conftest import TEST_OWNER_ID, extract_hidden_value, make_authenticated_client

VALID_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmJsAAAAASUVORK5CYII="
)


class AllowAllPhotoModerationService:
    async def assert_allowed(self, photo: ReferenceImageUpload) -> list[dict[str, object]]:
        return [
            {"category": "Hate", "severity": 0},
            {"category": "SelfHarm", "severity": 0},
            {"category": "Sexual", "severity": 0},
            {"category": "Violence", "severity": 0},
        ]


class RejectingPhotoModerationService:
    async def assert_allowed(self, photo: ReferenceImageUpload) -> list[dict[str, object]]:
        raise ProblemDetails(
            status_code=422,
            title="Saved Photo Rejected",
            detail=(
                "The uploaded photo could not be saved because it exceeded "
                "the allowed safety threshold (Sexual=4)."
            ),
            type="/problems/saved-photo-rejected",
            error_code="saved_photo_rejected",
        )


def build_services(monkeypatch: pytest.MonkeyPatch, **env_overrides: str) -> AppServices:
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    services = create_services(load_app_settings())
    services.photo_moderation_service = AllowAllPhotoModerationService()
    return services


def test_app_shell_renders_generation_form(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/app")

    assert response.status_code == 200
    assert 'hx-post="/ui/cards/generate"' in response.text
    assert 'hx-encoding="multipart/form-data"' in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert 'name="csrf_token"' in response.text
    assert 'name="idempotency_key"' in response.text
    assert "Image quality" not in response.text
    assert 'name="quality"' not in response.text
    assert 'name="photo"' in response.text
    assert 'name="saved_photo_id"' in response.text
    assert "data-saved-photo-id-input" in response.text
    assert 'name="save_photo"' in response.text
    assert 'name="photo_label"' in response.text
    assert 'accept="image/jpeg,image/png,image/webp"' in response.text
    assert "Reference photo preview" in response.text
    assert "up to 5 MB" in response.text
    assert "Pick from your library" in response.text
    assert "/my/photos/library" in response.text


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


def test_api_rejects_unsupported_photo_content_type(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-photo-type",
            "csrfToken": csrf_token,
        },
        files={"photo": ("portrait.gif", b"GIF89a", "image/gif")},
    )

    assert response.status_code == 415
    assert response.json()["errorCode"] == "unsupported_photo_type"


def test_api_rejects_oversized_photo_upload(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/api/v1/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-photo-size",
            "csrfToken": csrf_token,
        },
        files={"photo": ("portrait.png", b"x" * ((5 * 1024 * 1024) + 1), "image/png")},
    )

    assert response.status_code == 413
    assert response.json()["errorCode"] == "photo_too_large"


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


def test_generation_with_photo_uses_reference_image_edit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.text_calls = 0
            self.image_calls = 0
            self.image_edit_calls = 0
            self.reference_image: ReferenceImageUpload | None = None

        async def generate_card(self, prompt: str, *, request_id: str):
            self.text_calls += 1
            return await super().generate_card(prompt, request_id=request_id)

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_calls += 1
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

        async def generate_image_edit(
            self,
            art_prompt: str,
            *,
            reference_image: ReferenceImageUpload,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_edit_calls += 1
            self.reference_image = reference_image
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

    settings = load_app_settings()
    defaults = create_services(settings)
    ai_client = TrackingAIClient(settings)
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
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-photo-edit",
            "csrfToken": csrf_token,
        },
        files={"photo": ("portrait.webp", b"RIFFmockwebp", "image/webp")},
    )

    assert response.status_code == 200
    assert ai_client.text_calls == 1
    assert ai_client.image_calls == 0
    assert ai_client.image_edit_calls == 1
    assert ai_client.reference_image is not None
    assert ai_client.reference_image.content == b"RIFFmockwebp"
    assert ai_client.reference_image.content_type == "image/webp"


def test_generation_with_photo_returns_clear_error_when_edits_are_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedEditAIClient(MockAIClient):
        async def generate_image_edit(
            self,
            art_prompt: str,
            *,
            reference_image: ReferenceImageUpload,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            raise UpstreamServiceError(
                "foundry-image",
                "Image edits are unavailable on this deployment.",
                status_code=400,
                retryable=False,
                error_code="unsupported_operation",
            )

    settings = load_app_settings()
    defaults = create_services(settings)
    services = AppServices(
        settings=settings,
        card_repository=InMemoryCardRepository(),
        audit_repository=InMemoryAuditRepository(),
        asset_store=InMemoryAssetStore(),
        ai_client=UnsupportedEditAIClient(settings),
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
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-photo-unsupported",
            "csrfToken": csrf_token,
        },
        files={"photo": ("portrait.png", b"\x89PNG\r\n\x1a\nmock", "image/png")},
    )

    assert response.status_code == 502
    assert response.json()["errorCode"] == "reference_image_unsupported"
    assert services.card_repository._records == {}


def test_generation_without_photo_keeps_text_only_image_generation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.text_calls = 0
            self.image_calls = 0
            self.image_edit_calls = 0

        async def generate_card(self, prompt: str, *, request_id: str):
            self.text_calls += 1
            return await super().generate_card(prompt, request_id=request_id)

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_calls += 1
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

        async def generate_image_edit(
            self,
            art_prompt: str,
            *,
            reference_image: ReferenceImageUpload,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_edit_calls += 1
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

    settings = load_app_settings()
    defaults = create_services(settings)
    ai_client = TrackingAIClient(settings)
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
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-no-photo",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 200
    assert ai_client.text_calls == 1
    assert ai_client.image_calls == 1
    assert ai_client.image_edit_calls == 0


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


def test_live_mode_foundry_client_sends_real_bearer_token_for_text_requests(
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


def test_generation_paths_emit_dependency_moderation_partial_persistence_and_tokens(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies: list[dict[str, object]] = []
    retries: list[dict[str, object]] = []
    partials: list[str] = []
    moderation: list[dict[str, object]] = []
    persistence: list[dict[str, object]] = []
    tokens: list[tuple[str, object]] = []

    monkeypatch.setattr(
        generation_module,
        "record_dependency_attempt",
        lambda **attributes: dependencies.append(attributes),
    )
    monkeypatch.setattr(
        generation_module,
        "record_retry",
        lambda **attributes: retries.append(attributes),
    )
    monkeypatch.setattr(generation_module, "record_partial", partials.append)
    monkeypatch.setattr(
        generation_module,
        "record_moderation",
        lambda **attributes: moderation.append(attributes),
    )
    monkeypatch.setattr(
        generation_module,
        "record_persistence",
        lambda **attributes: persistence.append(attributes),
    )
    monkeypatch.setattr(
        generation_module,
        "record_token_usage",
        lambda operation, usage: tokens.append((operation, usage)),
    )
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    successful = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:text-429-once]]",
            "idempotencyKey": "idem-telemetry-retry",
            "csrfToken": csrf_token,
        },
    )
    partial = authenticated_client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:image-500]]",
            "idempotencyKey": "idem-telemetry-partial",
            "csrfToken": csrf_token,
        },
    )

    assert successful.status_code == 200
    assert partial.status_code == 200
    assert partial.json()["status"] == "awaiting_artwork_retry"
    assert any(
        item["dependency"] == "foundry-text" and item["outcome"] == "throttled"
        for item in dependencies
    )
    assert any(item["dependency"] == "foundry-text" for item in retries)
    assert any(
        item["dependency"] == "foundry-image" and item["outcome"] == "failed"
        for item in dependencies
    )
    assert "image_failure" in partials
    assert any(item["allowed"] is True for item in moderation)
    assert any(
        item["store"] == "card"
        and item["operation"] == "save_partial"
        and item["outcome"] == "completed"
        for item in persistence
    )
    assert {operation for operation, _ in tokens} == {"text", "image"}


def test_text_timeout_emits_bounded_dependency_timeout_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "0")
    dependencies: list[dict[str, object]] = []
    monkeypatch.setattr(
        generation_module,
        "record_dependency_attempt",
        lambda **attributes: dependencies.append(attributes),
    )
    client = make_authenticated_client(monkeypatch)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy ranger [[mock:text-timeout]]",
            "idempotencyKey": "idem-telemetry-timeout",
            "csrfToken": csrf_token,
        },
    )

    assert response.status_code == 504
    assert any(
        item["dependency"] == "foundry-text"
        and item["outcome"] == "timed_out"
        and item["error_code"] == "upstream_timeout"
        for item in dependencies
    )


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


def test_ui_generate_renders_composed_card_face_with_type_and_rarity_labels(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotency_key": "idem-ui-card-face",
            "csrf_token": csrf_token,
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    body = response.text
    assert 'class="card-face"' in body
    assert "data-rarity=" in body
    assert "data-type=" in body
    # Rarity/type meaning is conveyed by a visible text label, not color alone.
    assert any(label in body for label in ("Common", "Uncommon", "Rare", "Legendary"))
    assert any(label in body for label in ("Hero", "Creature", "Artifact", "Spell"))
    assert 'hx-swap-oob="true"' in body


def test_ui_generate_ignores_blank_optional_form_fields(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotency_key": "   ",
            "csrf_token": csrf_token,
            "saved_photo_id": "",
            "photo_label": "   ",
            "quality": "   ",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert 'class="card-face"' in response.text

    services = authenticated_client.app.state.services
    assert len(services.card_repository._records) == 1
    stored_card = next(iter(services.card_repository._records.values()))
    assert stored_card.idempotency_key
    assert stored_card.prompt == "create a safe fantasy knight with a radiant crown"


def test_ui_generate_blank_csrf_token_still_fails_csrf(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotency_key": "idem-ui-blank-csrf",
            "csrf_token": "   ",
            "saved_photo_id": "",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 403
    assert "error-panel" in response.text
    assert "A valid CSRF token is required for this action." in response.text


def test_ui_generate_still_rejects_non_blank_invalid_idempotency_key(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotency_key": "short",
            "csrf_token": csrf_token,
            "saved_photo_id": "",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 422
    assert "error-panel" in response.text
    assert "Unprocessable Entity" in response.text


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


def test_ui_rejects_unsupported_photo_content_type_with_error_panel(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-photo-type",
            "csrf_token": csrf_token,
        },
        files={"photo": ("portrait.gif", b"GIF89a", "image/gif")},
    )

    assert response.status_code == 415
    assert "error-panel" in response.text
    assert "Unsupported Photo Type" in response.text
    assert "JPEG, PNG, or WebP" in response.text


def test_ui_rejects_oversized_photo_upload_with_error_panel(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-photo-size",
            "csrf_token": csrf_token,
        },
        files={"photo": ("portrait.png", b"x" * ((5 * 1024 * 1024) + 1), "image/png")},
    )

    assert response.status_code == 413
    assert "error-panel" in response.text
    assert "Photo Too Large" in response.text
    assert "5 MB or smaller" in response.text


def test_ui_rejects_photo_and_saved_photo_together_with_error_panel(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-photo-conflict",
            "csrf_token": csrf_token,
            "saved_photo_id": uuid4().hex,
        },
        files={"photo": ("portrait.png", b"\x89PNG\r\n\x1a\nmock", "image/png")},
    )

    assert response.status_code == 422
    assert "error-panel" in response.text
    assert "Conflicting Photo Inputs" in response.text
    assert "Provide either photo or saved_photo_id, but not both." in response.text


def test_ui_multipart_saved_photo_submission_uses_image_edit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.image_calls = 0
            self.image_edit_calls = 0
            self.reference_image: ReferenceImageUpload | None = None

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_calls += 1
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

        async def generate_image_edit(
            self,
            art_prompt: str,
            *,
            reference_image: ReferenceImageUpload,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_edit_calls += 1
            self.reference_image = reference_image
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

    services = build_services(monkeypatch)
    services.ai_client = TrackingAIClient(services.settings)
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    saved = client.post(
        "/my/photos",
        data={"label": "Reusable", "csrf_token": csrf_token},
        files={"photo": ("reusable.png", VALID_PNG_BYTES, "image/png")},
    )
    saved_photo_id = saved.json()["photoId"]

    response = client.post(
        "/ui/cards/generate",
        files=[
            ("prompt", (None, "create a safe fantasy knight with a moonlit shield")),
            ("idempotency_key", (None, "idem-ui-saved-photo-multipart")),
            ("csrf_token", (None, csrf_token)),
            ("saved_photo_id", (None, saved_photo_id)),
        ],
    )

    assert response.status_code == 200
    assert services.ai_client.image_calls == 0
    assert services.ai_client.image_edit_calls == 1
    assert services.ai_client.reference_image is not None
    assert services.ai_client.reference_image.content == VALID_PNG_BYTES
    assert services.ai_client.reference_image.content_type == "image/png"


def test_ui_requires_upload_when_save_photo_is_enabled(
    authenticated_client: TestClient,
) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-save-without-upload",
            "csrf_token": csrf_token,
            "save_photo": "true",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 422
    assert "error-panel" in response.text
    assert "Saved Photo Requires Upload" in response.text
    assert "save_photo=true requires a fresh photo upload." in response.text


def test_ui_surfaces_saved_photo_limit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch, SAVED_PHOTO_MAX_COUNT="1")
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")
    first_save = client.post(
        "/my/photos",
        data={"label": "Existing photo", "csrf_token": csrf_token},
        files={"photo": ("existing.png", VALID_PNG_BYTES, "image/png")},
    )
    assert first_save.status_code == 201

    response = client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-save-limit",
            "csrf_token": csrf_token,
            "save_photo": "true",
            "photo_label": "Moonlit ranger",
        },
        files={"photo": ("portrait.png", b"\x89PNG\r\n\x1a\nmock", "image/png")},
    )

    assert response.status_code == 409
    assert "error-panel" in response.text
    assert "Saved Photo Limit Reached" in response.text


def test_ui_surfaces_saved_photo_rejected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch)
    services.photo_moderation_service = RejectingPhotoModerationService()
    client = TestClient(create_app(services=services), base_url="https://testserver")
    from app import main as main_module
    from tests.conftest import FakeOAuthClient

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client.get("/auth/login", follow_redirects=False)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    csrf_token = extract_hidden_value(client.get("/app").text, "csrf_token")

    response = client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-save-rejected",
            "csrf_token": csrf_token,
            "save_photo": "true",
            "photo_label": "Unsafe portrait",
        },
        files={"photo": ("portrait.png", b"\x89PNG\r\n\x1a\nmock", "image/png")},
    )

    assert response.status_code == 422
    assert "error-panel" in response.text
    assert "Saved Photo Rejected" in response.text


def test_ui_surfaces_missing_saved_photo_errors(authenticated_client: TestClient) -> None:
    csrf_token = extract_hidden_value(authenticated_client.get("/app").text, "csrf_token")

    response = authenticated_client.post(
        "/ui/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-ui-missing-saved-photo",
            "csrf_token": csrf_token,
            "saved_photo_id": "missing-photo",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 404
    assert "error-panel" in response.text
    assert "No saved photo was found for this user." in response.text


def test_card_generate_body_normalizes_blank_optional_strings() -> None:
    body = CardGenerateBody.model_validate(
        {
            "prompt": "create a safe fantasy knight with a radiant crown",
            "idempotencyKey": "   ",
            "csrfToken": "   ",
            "savedPhotoId": "",
        }
    )

    assert body.idempotencyKey is None
    assert body.csrfToken is None
    assert body.savedPhotoId is None


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

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            self.image_calls += 1
            return await super().generate_image(
                art_prompt, request_id=request_id, image_quality=image_quality
            )

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
                    detail=(f"Rate limit exceeded for {error_suffix}. Retry after 60 seconds."),
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
                    detail=(f"Rate limit exceeded for {error_suffix}. Retry after 60 seconds."),
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

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ):
            if "[[mock:image-500]]" in art_prompt:
                self.initial_image_calls += 1
            else:
                self.retry_image_calls += 1
            await asyncio.sleep(0.45)
            return await super().generate_image(
                art_prompt, request_id=request_id, image_quality=image_quality
            )

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


# ---------------------------------------------------------------------------
# Image quality — settings contract
# ---------------------------------------------------------------------------


def test_image_quality_defaults_to_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_QUALITY", raising=False)

    settings = load_app_settings()

    assert settings.image_quality == "low"


@pytest.mark.parametrize("quality", ["low", "medium", "high"])
def test_image_quality_accepts_valid_values(monkeypatch: pytest.MonkeyPatch, quality: str) -> None:
    monkeypatch.setenv("IMAGE_QUALITY", quality)

    settings = load_app_settings()

    assert settings.image_quality == quality


@pytest.mark.parametrize("bad_value", ["hd", "ultra", "standard", "Low", "HIGH"])
def test_image_quality_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    monkeypatch.setenv("IMAGE_QUALITY", bad_value)

    with pytest.raises(SettingsError, match="IMAGE_QUALITY must be one of"):
        load_app_settings()


def test_debug_log_ai_payloads_defaults_to_enabled_when_app_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEBUG_LOG_AI_PAYLOADS", raising=False)

    settings = load_app_settings()

    assert settings.app_env == "development"
    assert settings.debug_log_ai_payloads is True


def test_debug_log_ai_payloads_defaults_to_disabled_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DEBUG_LOG_AI_PAYLOADS", raising=False)

    settings = load_app_settings()

    assert settings.debug_log_ai_payloads is False


@pytest.mark.parametrize(
    ("app_env", "override", "expected"),
    [
        ("development", "false", False),
        ("development", "true", True),
        ("production", "true", False),
    ],
)
def test_debug_log_ai_payloads_env_override(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    override: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("DEBUG_LOG_AI_PAYLOADS", override)

    settings = load_app_settings()

    assert settings.debug_log_ai_payloads is expected


# ---------------------------------------------------------------------------
# Image quality — generation payload contract
# ---------------------------------------------------------------------------


def test_foundry_image_request_payload_includes_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")
    monkeypatch.setenv("IMAGE_QUALITY", "medium")

    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": (
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
                        ),
                        "revised_prompt": "A radiant knight.",
                    }
                ]
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

    asyncio.run(
        client.generate_image(
            "A radiant knight raising a silver shield.",
            request_id="req-quality",
        )
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert (
        payload.get("quality") == "medium"
    ), "Image generation payload must include 'quality' matching IMAGE_QUALITY setting"


def test_foundry_image_request_payload_quality_matches_low_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")
    monkeypatch.delenv("IMAGE_QUALITY", raising=False)

    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": (
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
                        ),
                        "revised_prompt": "A radiant knight.",
                    }
                ]
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

    asyncio.run(
        client.generate_image(
            "A shining knight in silver armor.",
            request_id="req-quality-default",
        )
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert (
        payload.get("quality") == "low"
    ), "Image generation payload must default to quality='low' when IMAGE_QUALITY is unset"


def test_foundry_image_edit_uses_edits_endpoint_and_multipart(
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
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["content_type"] = request.headers["Content-Type"]
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": (
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
                        ),
                        "revised_prompt": "A radiant knight based on the uploaded portrait.",
                    }
                ]
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

    asyncio.run(
        client.generate_image_edit(
            "A radiant knight raising a silver shield.",
            reference_image=ReferenceImageUpload(
                content=b"mock-image-bytes",
                content_type="image/png",
                filename="portrait.png",
            ),
            request_id="req-edit",
        )
    )

    assert captured["url"] == (
        "https://foundry.example/openai/deployments/gpt-image-2/images/edits"
        "?api-version=2025-04-01-preview"
    )
    assert captured["authorization"] == "Bearer live-access-token"
    content_type = captured["content_type"]
    assert isinstance(content_type, str)
    assert content_type.startswith("multipart/form-data; boundary=")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="prompt"' in body
    assert b"A radiant knight raising a silver shield." in body
    assert b'name="image"; filename="portrait.png"' in body


def test_generate_card_emits_debug_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEBUG_LOG_AI_PAYLOADS", "true")
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")

    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
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

    with caplog.at_level(logging.DEBUG, logger=generation_module.AI_DEBUG_LOGGER_NAME):
        asyncio.run(
            client.generate_card(
                "create a safe fantasy knight with a moonlit shield",
                request_id="req-debug-enabled",
            )
        )

    debug_records = [
        record for record in caplog.records if record.name == generation_module.AI_DEBUG_LOGGER_NAME
    ]
    assert len(debug_records) == 2
    messages = [record.getMessage() for record in debug_records]
    assert any("generate_card.request" in message for message in messages)
    assert any("generate_card.response" in message for message in messages)
    assert any(
        "create a safe fantasy knight with a moonlit shield" in message for message in messages
    )
    assert any("Knight of Dawn" in message for message in messages)


def test_generate_card_does_not_emit_debug_logs_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG_LOG_AI_PAYLOADS", "true")
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_TEXT_DEPLOYMENT", "gpt-5-5")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")

    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
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

    with caplog.at_level(logging.DEBUG, logger=generation_module.AI_DEBUG_LOGGER_NAME):
        asyncio.run(
            client.generate_card(
                "create a safe fantasy knight with a moonlit shield",
                request_id="req-debug-disabled",
            )
        )

    assert not [
        record for record in caplog.records if record.name == generation_module.AI_DEBUG_LOGGER_NAME
    ]


# ---------------------------------------------------------------------------
# Image quality — retry contract
# ---------------------------------------------------------------------------


def test_partial_card_persists_selected_image_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When image generation fails the partial StoredCard must carry the original quality."""
    monkeypatch.setenv("IMAGE_QUALITY", "low")

    async def run() -> StoredCard | None:
        settings = load_app_settings()
        defaults = create_services(settings)
        repo = InMemoryCardRepository()
        services = AppServices(
            settings=settings,
            card_repository=repo,
            audit_repository=InMemoryAuditRepository(),
            asset_store=InMemoryAssetStore(),
            ai_client=MockAIClient(settings),
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
            display_name="Samwise",
            email="samwise@example.com",
        )
        response = await service.generate_card(
            owner=owner,
            prompt="create a safe fantasy knight [[mock:image-500]]",
            idempotency_key="idem-quality-persist",
            request_id="req-quality-persist",
            client_ip="127.0.0.1",
            image_quality="high",
        )
        return await repo.get(owner.owner_id, response.cardId)

    record = asyncio.run(run())
    assert record is not None
    assert record.status == "awaiting_artwork_retry"
    assert (
        record.image_quality == "high"
    ), "Partial card must persist the image_quality used during the initial generation"


def test_retry_artwork_uses_original_image_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artwork retry must forward the quality originally chosen, not the current app default."""
    monkeypatch.setenv("IMAGE_QUALITY", "low")  # app default is low; original was high

    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.retry_quality: list[str | None] = []

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ) -> object:
            if "[[mock:image-500]]" not in art_prompt:
                self.retry_quality.append(image_quality)
            return await super().generate_image(
                art_prompt, request_id=request_id, image_quality=image_quality
            )

    async def run() -> list[str | None]:
        settings = load_app_settings()
        defaults = create_services(settings)
        ai_client = TrackingAIClient(settings)
        repo = InMemoryCardRepository()
        services = AppServices(
            settings=settings,
            card_repository=repo,
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
            display_name="Samwise",
            email="samwise@example.com",
        )
        partial = await service.generate_card(
            owner=owner,
            prompt="create a safe fantasy knight [[mock:image-500]]",
            idempotency_key="idem-retry-quality",
            request_id="req-retry-quality",
            client_ip="127.0.0.1",
            image_quality="high",
        )
        record = await repo.get(owner.owner_id, partial.cardId)
        assert record is not None
        record.derived_art_prompt = (record.derived_art_prompt or "").replace(
            "[[mock:image-500]]", ""
        )
        await repo.save(record)

        await service.retry_artwork(
            owner=owner,
            card_id=partial.cardId,
            idempotency_key="idem-retry-quality-finish",
            request_id="req-retry-quality-finish",
            client_ip="127.0.0.1",
        )
        return ai_client.retry_quality

    retry_qualities = asyncio.run(run())
    assert retry_qualities == ["high"], (
        "Artwork retry must use the quality from the original generation ('high'), "
        "not the current app default ('low')"
    )


def test_retry_artwork_legacy_record_without_quality_falls_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy stored card without imageQuality must fall back to settings.image_quality."""
    monkeypatch.setenv("IMAGE_QUALITY", "medium")

    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.retry_quality: list[str | None] = []

        async def generate_image(
            self,
            art_prompt: str,
            *,
            request_id: str,
            image_quality: Literal["low", "medium", "high"] | None = None,
        ) -> object:
            self.retry_quality.append(image_quality)
            return await super().generate_image(
                art_prompt, request_id=request_id, image_quality=image_quality
            )

    async def run() -> tuple[list[str | None], str | None]:
        settings = load_app_settings()
        defaults = create_services(settings)
        ai_client = TrackingAIClient(settings)
        repo = InMemoryCardRepository()

        # Generate a normal partial card so we have a valid validatedPayload and derivedArtPrompt.
        seed_services = AppServices(
            settings=settings,
            card_repository=repo,
            audit_repository=InMemoryAuditRepository(),
            asset_store=InMemoryAssetStore(),
            ai_client=MockAIClient(settings),
            moderation_service=defaults.moderation_service,
            rate_limiter=defaults.rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        seed_service = CardGenerationService(seed_services)
        owner = AuthenticatedOwner(
            owner_id=TEST_OWNER_ID,
            tenant_id=None,
            object_id=None,
            subject="sub",
            display_name="Samwise",
            email="samwise@example.com",
        )
        partial = await seed_service.generate_card(
            owner=owner,
            prompt="create a safe fantasy knight [[mock:image-500]]",
            idempotency_key="idem-legacy-seed",
            request_id="req-legacy-seed",
            client_ip="127.0.0.1",
            image_quality="high",
        )

        # Simulate a legacy record by clearing image_quality from the stored document.
        record = await repo.get(owner.owner_id, partial.cardId)
        assert record is not None
        legacy_doc = record.to_document()
        legacy_doc.pop("imageQuality", None)
        legacy_record = StoredCard.from_document(legacy_doc)
        legacy_record.derived_art_prompt = (legacy_record.derived_art_prompt or "").replace(
            "[[mock:image-500]]", ""
        )
        await repo.save(legacy_record)

        # Now retry using the tracking client.
        retry_services = AppServices(
            settings=settings,
            card_repository=repo,
            audit_repository=InMemoryAuditRepository(),
            asset_store=InMemoryAssetStore(),
            ai_client=ai_client,
            moderation_service=defaults.moderation_service,
            rate_limiter=defaults.rate_limiter,
            csrf_protector=defaults.csrf_protector,
        )
        retry_service = CardGenerationService(retry_services)
        await retry_service.retry_artwork(
            owner=owner,
            card_id=partial.cardId,
            idempotency_key="idem-legacy-retry",
            request_id="req-legacy-retry",
            client_ip="127.0.0.1",
        )
        return ai_client.retry_quality, legacy_record.image_quality

    retry_qualities, legacy_quality = asyncio.run(run())
    assert legacy_quality is None, "Legacy record round-trip must have image_quality=None"
    assert retry_qualities == [
        "medium"
    ], "Legacy records without imageQuality must fall back to settings.image_quality ('medium')"
