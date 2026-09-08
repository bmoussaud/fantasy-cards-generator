from __future__ import annotations

import asyncio
import json

import httpx
import pytest

pytest.importorskip("agent_framework.foundry")
pytest.importorskip("azure.ai.agentserver.responses")

from app.foundry_agent_client import GenerateCardAgentRequest  # noqa: E402
from hosted_agents.card_orchestrator import specialists  # noqa: E402
from hosted_agents.card_orchestrator.orchestrator import (  # noqa: E402
    CardOrchestrator,
    RuntimeFailure,
)
from tests.test_card_orchestrator import ART, CARD, LORE, settings  # noqa: E402


def model_response(output, *, refusal=False, incomplete=False):
    content = [{"type": "output_text", "text": json.dumps(output), "annotations": []}]
    if refusal:
        content.append({"type": "refusal", "refusal": "PRIVATE_REFUSAL"})
    return {
        "id": "resp_offline",
        "object": "response",
        "created_at": 1788858000,
        "model": "test",
        "status": "incomplete" if incomplete else "completed",
        "error": None,
        "incomplete_details": {"reason": "content_filter"} if incomplete else None,
        "output": [
            {
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": content,
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


@pytest.fixture
def model_transport(monkeypatch):
    calls, clients, credentials, projects = [], [], [], []
    responses = [model_response(CARD), model_response(LORE), model_response(ART)]
    original_openai = specialists.AIProjectClient.get_openai_client
    original_exit = specialists.AIProjectClient.__aexit__

    class Credential:
        def __init__(self, **kwargs):
            self.closed = False
            credentials.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def get_token(self, *args, **kwargs):
            raise AssertionError("Offline test must not acquire tokens")

    async def handle(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == (
            "https://example.services.ai.azure.com/api/projects/test/openai/v1/responses"
        )
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, httpx.Response):
            return response
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    def get_openai(project, **kwargs):
        client = original_openai(
            project,
            api_key="offline",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            **kwargs,
        )
        clients.append(client)
        return client

    async def close(project, *args):
        projects.append(project)
        await original_exit(project, *args)

    monkeypatch.setattr(specialists, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(specialists.AIProjectClient, "get_openai_client", get_openai)
    monkeypatch.setattr(specialists.AIProjectClient, "__aexit__", close)
    return calls, responses, clients, credentials, projects


def test_real_maf_three_model_calls_are_project_scoped_and_nonpersistent(model_transport):
    calls, _, clients, credentials, projects = model_transport
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
    )
    assert result.status == "completed", result
    assert result.card.model_dump() == CARD | LORE | ART
    assert len(calls) == 3
    for call in calls:
        assert call["model"] == "test"
        assert call["store"] is False
        assert call.get("stream", False) is False
        assert "conversation" not in call
        assert "previous_response_id" not in call
        assert not call.get("tools")
        assert call["text"]["format"]["strict"] is True
        assert call["max_output_tokens"] == 1800
        assert len([m for m in call["input"] if m["role"] == "user"]) == 1
    assert clients[0].max_retries == 0
    assert all(client.is_closed() for client in clients)
    assert all(credential.closed for credential in credentials)
    assert len(projects) == 1


@pytest.mark.parametrize(
    "kind", ["refusal", "filter", "filter_http", "filter_failed", "invalid_with_refusal"]
)
def test_authoritative_refusal_wins_over_generated_card(model_transport, kind):
    calls, responses, clients, credentials, _ = model_transport
    if kind == "filter_http":
        responses[0] = httpx.Response(
            400, json={"error": {"code": "content_filter", "message": "PRIVATE_FILTER"}}
        )
    else:
        responses[0] = model_response(
            {"invalid": "PRIVATE_MODEL"} if kind == "invalid_with_refusal" else CARD,
            refusal=kind in {"refusal", "invalid_with_refusal"},
            incomplete=kind == "filter",
        )
        if kind == "filter_failed":
            responses[0]["status"] = "failed"
            responses[0]["error"] = {"code": "content_filter", "message": "PRIVATE_FILTER"}
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
    )
    assert result.status == "refused"
    assert result.card is result.artPrompt is None
    assert "PRIVATE" not in result.model_dump_json()
    assert len(calls) == 1
    assert all(client.is_closed() for client in clients)
    assert all(credential.closed for credential in credentials)


def test_real_model_schema_failure_is_held(model_transport):
    calls, responses, *_ = model_transport
    responses[0] = model_response(CARD | {"manaCost": 100})
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
    )
    assert result.status == "held"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_real_model_http_errors_do_not_retry_or_leak(model_transport, caplog, status):
    calls, responses, clients, credentials, _ = model_transport
    responses[0] = httpx.Response(status, json={"error": {"message": "PRIVATE_DEPENDENCY"}})
    with pytest.raises(RuntimeFailure) as exc:
        asyncio.run(
            CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
        )
    assert "PRIVATE" not in str(exc.value) + caplog.text
    assert len(calls) == 1
    assert all(client.is_closed() for client in clients)
    assert all(credential.closed for credential in credentials)


def test_model_cannot_trigger_tool_loop(model_transport):
    calls, responses, *_ = model_transport
    responses[0]["output"].append(
        {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test",
            "name": "unavailable",
            "arguments": "{}",
            "status": "completed",
        }
    )
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
    )
    assert result.status == "held"
    assert len(calls) == 1


def test_real_maf_error_logging_is_suppressed_at_host_boundary(model_transport, caplog):
    from hosted_agents.card_orchestrator.server import create_host
    from tests.test_card_orchestrator import post, wire

    calls, responses, *_ = model_transport
    responses[0] = httpx.Response(500, json={"error": {"message": "PRIVATE_MODEL_OR_CREDENTIAL"}})
    caplog.set_level("DEBUG")
    response = asyncio.run(post(create_host(settings()), wire("PRIVATE_USER_TEXT")))
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert len(calls) == 1
    assert "PRIVATE" not in caplog.text + response.text
