from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError

from app.foundry_agent_client import (
    FOUNDRY_AGENT_TOKEN_SCOPE,
    FoundryAgentClient,
    _build_responses_url,
)
from app.settings import SettingsError, load_app_settings

PROJECT_ENDPOINT = "https://cards.services.ai.azure.com/api/projects/fellowship"
AGENT_NAME = "card-orchestrator"


class FakeAccessToken:
    def __init__(self, token: str) -> None:
        self.token = token


class FakeCredential:
    def __init__(self, token: str = "fake-token") -> None:
        self.token = token
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False

    def get_token(self, *scopes: str) -> FakeAccessToken:
        self.scopes.append(scopes)
        return FakeAccessToken(self.token)

    def close(self) -> None:
        self.closed = True


def configured_settings(**overrides: Any):
    settings = load_app_settings()
    values = {
        "foundry_project_endpoint": PROJECT_ENDPOINT,
        "foundry_agent_name": AGENT_NAME,
        "foundry_agent_timeout_seconds": 1.0,
    } | overrides
    return replace(settings, **values)


def valid_card() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "name": "Ember Drake",
        "cardType": "creature",
        "rarity": "uncommon",
        "manaCost": 4,
        "attack": 5,
        "health": 3,
        "rulesText": "When Ember Drake enters play, deal 2 damage to any target.",
        "flavorText": "Born from the dying breath of a volcano.",
        "artBrief": "Orange-scaled ember drake soaring over a volcanic crater at dusk.",
    }


def responses_envelope(agent_payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    envelope = {
        "id": "resp_123",
        "object": "response",
        "created_at": 1788780000,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "status": "completed",
        "parallel_tool_calls": True,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(agent_payload),
                    }
                ],
            }
        ],
    }
    envelope.update(overrides)
    return envelope


def completed_agent_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "schemaVersion": 1,
        "status": "completed",
        "card": valid_card(),
        "artPrompt": "Orange-scaled ember drake over a volcanic crater at dusk.",
        "metadata": {"agentVersion": "2026.09.07"},
        "safetyHints": [],
    }
    payload.update(overrides)
    return payload


def invoke_with_transport(
    settings,
    handler,
    *,
    credential: FakeCredential | None = None,
) -> tuple[Any, FakeCredential]:
    fake_credential = credential or FakeCredential()
    client = FoundryAgentClient(
        settings,
        credential=fake_credential,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.invoke("Create a safe original fire drake trading card."))
    asyncio.run(client.aclose())
    return result, fake_credential


def test_agent_settings_are_optional_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_AGENT_NAME", raising=False)

    settings = load_app_settings()

    assert settings.foundry_project_endpoint is None
    assert settings.foundry_agent_name is None
    assert settings.foundry_agent_api_version == "v1"
    assert settings.foundry_agent_timeout_seconds == 5.0
    assert settings.foundry_endpoint is None


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_agent_timeout_must_be_positive_and_finite(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENT_TIMEOUT_SECONDS", timeout)

    with pytest.raises(SettingsError, match="FOUNDRY_AGENT_TIMEOUT_SECONDS"):
        load_app_settings()


def test_url_builder_normalizes_trailing_slash_and_encodes_agent_name() -> None:
    url = _build_responses_url(
        PROJECT_ENDPOINT,
        "card orchestrator",
        "v1",
    )

    assert (
        url == "https://cards.services.ai.azure.com/api/projects/fellowship/"
        "agents/card%20orchestrator/endpoint/protocols/openai/responses?api-version=v1"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://cards.services.ai.azure.com/api/projects/fellowship",
        "https://user:pass@cards.services.ai.azure.com/api/projects/fellowship",
        "https://cards.services.ai.azure.com/api/projects/fellowship?x=1",
        "https://cards.services.ai.azure.com/api/projects/fellowship#fragment",
        "https://cards.openai.azure.com/api/projects/fellowship",
        "https://cards.services.ai.azure.com/openai/deployments/model",
        "https://cards.services.ai.azure.com/api/projects/fellowship/extra",
    ],
)
def test_invalid_project_endpoint_is_rejected_before_token(endpoint: str) -> None:
    credential = FakeCredential()
    result, credential = invoke_with_transport(
        configured_settings(foundry_project_endpoint=endpoint),
        lambda request: httpx.Response(200, json={}),
        credential=credential,
    )

    assert result.status == "configuration_error"
    assert credential.scopes == []


@pytest.mark.parametrize("agent_name", ["bad/name", "bad\\name", "bad?name", "bad#name", " "])
def test_agent_name_must_be_single_path_segment(agent_name: str) -> None:
    credential = FakeCredential()
    result, credential = invoke_with_transport(
        configured_settings(foundry_agent_name=agent_name),
        lambda request: httpx.Response(200, json={}),
        credential=credential,
    )

    assert result.status == "configuration_error"
    assert credential.scopes == []


def test_successful_invocation_uses_documented_wire_contract() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"apim-request-id": "req-123"},
            json=responses_envelope(completed_agent_payload()),
        )

    result, credential = invoke_with_transport(handler=handler, settings=configured_settings())

    assert result.success is True
    assert result.schema_valid is True
    assert result.agent_version == "2026.09.07"
    assert result.card is not None
    assert credential.scopes == [(FOUNDRY_AGENT_TOKEN_SCOPE,)]
    assert captured["url"].endswith(
        "/api/projects/fellowship/agents/card-orchestrator/"
        "endpoint/protocols/openai/responses?api-version=v1"
    )
    assert captured["authorization"] == "Bearer fake-token"
    assert captured["body"]["store"] is False
    assert "tools" not in captured["body"]
    assert "user" not in captured["body"]
    assert "safety_identifier" not in captured["body"]
    input_message = captured["body"]["input"][0]
    assert input_message["role"] == "user"
    content = input_message["content"][0]
    assert content["type"] == "input_text"
    agent_request = json.loads(content["text"])
    assert agent_request == {
        "schemaVersion": 1,
        "query": "Create a safe original fire drake trading card.",
    }
    assert "owner" not in content["text"].lower()
    assert "photo" not in content["text"].lower()


def test_top_level_output_text_is_not_required_when_raw_output_array_is_present() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(
            200,
            json=responses_envelope(completed_agent_payload(), output_text=None),
        ),
    )

    assert result.success is True
    assert result.schema_valid is True


@pytest.mark.parametrize(
    "output",
    [
        None,
        "malformed",
        [],
        [None],
        [{"type": "message", "content": "malformed"}],
        [{"type": "message", "content": [None]}],
        [{"type": "message", "content": [{"type": "output_text", "text": 123}]}],
    ],
)
def test_top_level_output_text_cannot_rescue_malformed_raw_output(output: Any) -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(
            200,
            json=responses_envelope(
                completed_agent_payload(),
                output=output,
                output_text=json.dumps(completed_agent_payload()),
            ),
        ),
    )

    assert result.status == "invalid_response"
    assert result.success is False
    assert result.schema_valid is False


def test_malformed_message_cannot_be_hidden_by_valid_output_text() -> None:
    envelope = responses_envelope(completed_agent_payload())
    envelope["output"].append({"type": "message", "content": "malformed"})
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(200, json=envelope),
    )

    assert result.status == "invalid_response"
    assert result.success is False


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transient_http_statuses_are_retryable(status_code: int) -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(status_code, json={"error": {"code": "busy"}}),
    )

    assert result.status == "transient_error"
    assert result.retryable is True
    assert result.error_code == "busy"


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_http_statuses_are_not_retryable(status_code: int) -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(status_code, json={"error": {"code": "Unauthorized"}}),
    )

    assert result.status == "auth_error"
    assert result.retryable is False


def test_http_content_filter_is_policy_refusal() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(400, json={"error": {"code": "content_filter"}}),
    )

    assert result.status == "policy_refusal"
    assert result.retryable is False


def test_wire_refusal_is_detected_before_card_validation() -> None:
    envelope = responses_envelope({"not": "a card"})
    envelope["output"] = [
        {
            "type": "message",
            "content": [
                {
                    "type": "refusal",
                    "refusal": "blocked by policy",
                }
            ],
        }
    ]
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(200, json=envelope),
    )

    assert result.status == "policy_refusal"
    assert result.error_code == "refusal"


def test_incomplete_response_is_non_success() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(
            200,
            json=responses_envelope(
                completed_agent_payload(),
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        ),
    )

    assert result.status == "incomplete"
    assert result.success is False
    assert result.error_code == "max_output_tokens"


@pytest.mark.parametrize(
    ("envelope", "error_code"),
    [
        ({"output": []}, "empty_output"),
        (responses_envelope(completed_agent_payload(), output=[]), "empty_output"),
        (responses_envelope(completed_agent_payload(), output="bad"), "empty_output"),
    ],
)
def test_empty_or_malformed_output_is_non_success(
    envelope: dict[str, Any],
    error_code: str,
) -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(200, json=envelope),
    )

    assert result.status == "invalid_response"
    assert result.error_code == error_code


def test_non_json_agent_text_is_non_success() -> None:
    envelope = responses_envelope(completed_agent_payload())
    envelope["output"][0]["content"][0]["text"] = "not json"
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(200, json=envelope),
    )

    assert result.status == "invalid_response"
    assert result.error_code == "schema_validation_failed"


def test_invalid_card_schema_is_non_success() -> None:
    payload = completed_agent_payload(card=valid_card() | {"cardType": "villain"})
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(200, json=responses_envelope(payload)),
    )

    assert result.status == "invalid_response"
    assert result.schema_valid is False


def test_agent_version_mismatch_is_non_success() -> None:
    result, _ = invoke_with_transport(
        configured_settings(foundry_agent_expected_version="expected"),
        lambda request: httpx.Response(
            200,
            json=responses_envelope(completed_agent_payload(metadata={"agentVersion": "actual"})),
        ),
    )

    assert result.status == "version_mismatch"
    assert result.schema_valid is True
    assert result.agent_version == "actual"


def test_non_completed_agent_status_is_valid_but_non_success() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(
            200,
            json=responses_envelope(
                {
                    "schemaVersion": 1,
                    "status": "routing_defer",
                    "card": None,
                    "artPrompt": None,
                    "metadata": {"version": "v-next"},
                    "safetyHints": ["Needs lore specialist."],
                }
            ),
        ),
    )

    assert result.status == "routing_defer"
    assert result.schema_valid is True
    assert result.success is False
    assert result.agent_version == "v-next"


def test_timeout_is_transient() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=responses_envelope(completed_agent_payload()))

    result, _ = invoke_with_transport(
        configured_settings(foundry_agent_timeout_seconds=0.01),
        handler,
    )

    assert result.status == "transient_error"
    assert result.retryable is True
    assert result.error_code == "timeout"


def test_errors_do_not_leak_prompt_token_or_response_body() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: httpx.Response(
            401,
            json={
                "error": {
                    "code": "Unauthorized",
                    "message": "fake-token Create a safe original fire drake trading card.",
                }
            },
        ),
    )

    assert "fake-token" not in result.message
    assert "fire drake" not in result.message
    assert result.error_code == "Unauthorized"


def test_invalid_query_diagnostics_do_not_include_input() -> None:
    credential = FakeCredential()
    query = "private-query-marker-" * 21

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid query must not trigger an HTTP request")

    async def invoke_invalid_query():
        async with FoundryAgentClient(
            configured_settings(),
            credential=credential,
            transport=httpx.MockTransport(unexpected_request),
        ) as client:
            return await client.invoke(query)

    result = asyncio.run(invoke_invalid_query())

    assert result.status == "configuration_error"
    assert result.success is False
    assert credential.scopes == []
    assert result.message == "Foundry agent configuration or query is invalid."
    assert "private-query-marker" not in repr(result)


@pytest.mark.parametrize("failure", [ClientAuthenticationError, ServiceRequestError])
def test_expected_credential_failures_are_sanitized(failure) -> None:
    class FailingCredential(FakeCredential):
        def get_token(self, *scopes: str) -> FakeAccessToken:
            raise failure("private-credential-diagnostic")

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Failed authentication must not trigger an HTTP request")

    result, _ = invoke_with_transport(
        configured_settings(), unexpected_request, credential=FailingCredential()
    )

    assert result.status == "auth_error"
    assert result.retryable is False
    assert "private-credential-diagnostic" not in repr(result)


def test_empty_credential_token_is_non_success() -> None:
    result, _ = invoke_with_transport(
        configured_settings(),
        lambda request: pytest.fail("Empty token must not trigger an HTTP request"),
        credential=FakeCredential(token=""),
    )

    assert result.status == "auth_error"
    assert result.success is False


def test_unexpected_credential_programming_errors_are_not_swallowed() -> None:
    class BrokenCredential(FakeCredential):
        def get_token(self, *scopes: str) -> FakeAccessToken:
            raise RuntimeError("unexpected implementation error")

    with pytest.raises(RuntimeError, match="unexpected implementation error"):
        invoke_with_transport(
            configured_settings(),
            lambda request: pytest.fail("Broken credential must not trigger an HTTP request"),
            credential=BrokenCredential(),
        )


def test_owned_resources_are_closed_but_injected_client_is_not_closed() -> None:
    owned_credential = FakeCredential()
    owned_client = FoundryAgentClient(configured_settings(), credential=owned_credential)
    asyncio.run(owned_client.aclose())
    assert owned_credential.closed is False

    external_credential = FakeCredential()
    external_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    client = FoundryAgentClient(
        configured_settings(),
        credential=external_credential,
        http_client=external_http_client,
    )
    asyncio.run(client.aclose())

    assert external_credential.closed is False
    assert external_http_client.is_closed is False
    asyncio.run(external_http_client.aclose())
