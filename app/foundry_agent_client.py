from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.generation import GeneratedCardModel
from app.settings import AppSettings, SettingsError, load_app_settings

FOUNDRY_AGENT_TOKEN_SCOPE = "https://ai.azure.com/.default"


class TokenCredential(Protocol):
    def get_token(self, *scopes: str) -> Any: ...


class GenerateCardAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schemaVersion: Literal[1] = 1
    query: str = Field(min_length=1, max_length=400)


class GenerateCardAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schemaVersion: Literal[1]
    status: Literal["completed", "refused", "routing_defer", "held"]
    card: GeneratedCardModel | None = None
    artPrompt: str | None = Field(default=None, min_length=1, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    safetyHints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _completed_requires_card_and_art_prompt(self) -> GenerateCardAgentResponse:
        if self.status == "completed" and (self.card is None or self.artPrompt is None):
            raise ValueError("completed agent responses require card and artPrompt")
        return self


@dataclass(frozen=True, slots=True)
class FoundryAgentInvocationResult:
    status: str
    success: bool = False
    retryable: bool = False
    schema_valid: bool = False
    response_id: str | None = None
    request_id: str | None = None
    agent_version: str | None = None
    card: GeneratedCardModel | None = None
    art_prompt: str | None = None
    error_code: str | None = None
    message: str = ""


class FoundryAgentConfigurationError(SettingsError):
    pass


class FoundryAgentClient:
    def __init__(
        self,
        settings: AppSettings,
        *,
        credential: TokenCredential | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential if credential is not None else _default_azure_credential()
        self._owns_credential = credential is None
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._transport = transport

    async def __aenter__(self) -> FoundryAgentClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        if self._owns_credential:
            close = getattr(self._credential, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def invoke(self, query: str) -> FoundryAgentInvocationResult:
        try:
            return await asyncio.wait_for(
                self._invoke_without_outer_timeout(query),
                timeout=self._settings.foundry_agent_timeout_seconds,
            )
        except TimeoutError:
            return FoundryAgentInvocationResult(
                status="transient_error",
                retryable=True,
                error_code="timeout",
                message="Foundry agent invocation timed out.",
            )

    async def _invoke_without_outer_timeout(self, query: str) -> FoundryAgentInvocationResult:
        try:
            url = _build_responses_url(
                self._settings.foundry_project_endpoint,
                self._settings.foundry_agent_name,
                self._settings.foundry_agent_api_version,
            )
            request = GenerateCardAgentRequest(query=query)
        except (SettingsError, ValidationError, ValueError):
            return FoundryAgentInvocationResult(
                status="configuration_error",
                error_code="invalid_configuration",
                message="Foundry agent configuration or query is invalid.",
            )

        try:
            token = await self._get_token()
        except (ClientAuthenticationError, ServiceRequestError, ServiceResponseError):
            return FoundryAgentInvocationResult(
                status="auth_error",
                error_code="credential_unavailable",
                message="Unable to acquire a Foundry access token.",
            )

        client = self._client()
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "store": False,
                    "stream": False,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": request.model_dump_json(by_alias=False),
                                }
                            ],
                        }
                    ],
                },
            )
        except httpx.TimeoutException:
            return FoundryAgentInvocationResult(
                status="transient_error",
                retryable=True,
                error_code="timeout",
                message="Foundry agent request timed out.",
            )
        except httpx.TransportError:
            return FoundryAgentInvocationResult(
                status="transient_error",
                retryable=True,
                error_code="transport_error",
                message="Foundry agent transport failed.",
            )

        request_id = _safe_identifier_or_none(
            response.headers.get("apim-request-id") or response.headers.get("x-ms-request-id")
        )
        if response.status_code >= 400:
            return _http_error_result(response, request_id=request_id)

        try:
            body = response.json()
        except ValueError:
            return FoundryAgentInvocationResult(
                status="invalid_response",
                request_id=request_id,
                error_code="non_json_response",
                message="Foundry agent returned non-JSON response.",
            )

        if not isinstance(body, dict):
            return FoundryAgentInvocationResult(
                status="invalid_response",
                request_id=request_id,
                error_code="malformed_response",
                message="Foundry agent response envelope was malformed.",
            )
        return _parse_success_envelope(
            body,
            request_id=request_id,
            expected_version=self._settings.foundry_agent_expected_version,
        )

    async def _get_token(self) -> str:
        get_token = self._credential.get_token
        if inspect.iscoroutinefunction(get_token):
            token_result = await get_token(FOUNDRY_AGENT_TOKEN_SCOPE)
        else:
            token_result = await asyncio.to_thread(get_token, FOUNDRY_AGENT_TOKEN_SCOPE)
        if inspect.isawaitable(token_result):
            token_result = await token_result
        token = getattr(token_result, "token", token_result)
        if not isinstance(token, str) or not token:
            raise ClientAuthenticationError("Credential returned an empty token.")
        return token

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            timeout = httpx.Timeout(self._settings.foundry_agent_timeout_seconds)
            self._http_client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._http_client


def _default_azure_credential() -> TokenCredential:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


def _build_responses_url(
    project_endpoint: str | None,
    agent_name: str | None,
    api_version: str,
) -> str:
    endpoint = _normalize_project_endpoint(project_endpoint)
    encoded_agent = _encode_agent_name(agent_name)
    if api_version not in {"v1", "preview"}:
        raise FoundryAgentConfigurationError("FOUNDRY_AGENT_API_VERSION must be 'v1' or 'preview'.")
    return (
        f"{endpoint}agents/{encoded_agent}/endpoint/protocols/openai/responses"
        f"?api-version={quote(api_version, safe='')}"
    )


def _normalize_project_endpoint(project_endpoint: str | None) -> str:
    if not project_endpoint:
        raise FoundryAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT must be set for invocation.")
    parsed = urlsplit(project_endpoint)
    if parsed.scheme != "https":
        raise FoundryAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT must use https.")
    if parsed.username or parsed.password:
        raise FoundryAgentConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT must not include credentials."
        )
    if parsed.query or parsed.fragment:
        raise FoundryAgentConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT must not include query or fragment."
        )
    host = (parsed.hostname or "").lower()
    if not host.endswith(".services.ai.azure.com"):
        raise FoundryAgentConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT must be a public Azure AI services project endpoint."
        )
    if parsed.port is not None:
        raise FoundryAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT must not include a port.")
    if "%2f" in parsed.path.lower() or "%5c" in parsed.path.lower():
        raise FoundryAgentConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT path must not encode slashes."
        )
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/api/projects/[^/]+", path):
        raise FoundryAgentConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT path must be /api/projects/<project>."
        )
    normalized = urlunsplit(("https", host, f"{path}/", "", ""))
    return normalized


def _encode_agent_name(agent_name: str | None) -> str:
    if not agent_name:
        raise FoundryAgentConfigurationError("FOUNDRY_AGENT_NAME must be set for invocation.")
    stripped = agent_name.strip()
    if not stripped or any(character in stripped for character in ("/", "\\", "?", "#")):
        raise FoundryAgentConfigurationError("FOUNDRY_AGENT_NAME must be a single path segment.")
    return quote(stripped, safe="")


def _http_error_result(
    response: httpx.Response,
    *,
    request_id: str | None,
) -> FoundryAgentInvocationResult:
    code = _error_code(response)
    if response.status_code in {401, 403}:
        return FoundryAgentInvocationResult(
            status="auth_error",
            request_id=request_id,
            error_code=code or f"http_{response.status_code}",
            message="Foundry agent authentication or authorization failed.",
        )
    if response.status_code == 400 and code == "content_filter":
        return FoundryAgentInvocationResult(
            status="policy_refusal",
            request_id=request_id,
            error_code=code,
            message="Foundry policy refused the request.",
        )
    if response.status_code == 429 or response.status_code >= 500:
        return FoundryAgentInvocationResult(
            status="transient_error",
            retryable=True,
            request_id=request_id,
            error_code=code or f"http_{response.status_code}",
            message="Foundry agent service returned a transient error.",
        )
    return FoundryAgentInvocationResult(
        status="http_error",
        request_id=request_id,
        error_code=code or f"http_{response.status_code}",
        message="Foundry agent request failed.",
    )


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    return _safe_identifier_or_none(error.get("code"))


def _parse_success_envelope(
    body: Mapping[str, Any],
    *,
    request_id: str | None,
    expected_version: str | None,
) -> FoundryAgentInvocationResult:
    response_id = _safe_identifier_or_none(body.get("id"))
    envelope_error = body.get("error")
    if isinstance(envelope_error, dict):
        code = _string_or_none(envelope_error.get("code"))
        if code == "content_filter":
            return FoundryAgentInvocationResult(
                status="policy_refusal",
                response_id=response_id,
                request_id=request_id,
                error_code=code,
                message="Foundry policy refused the request.",
            )
        return FoundryAgentInvocationResult(
            status="failed",
            response_id=response_id,
            request_id=request_id,
            error_code=code or "response_error",
            message="Foundry agent response reported failure.",
        )

    response_status = body.get("status")
    if response_status == "incomplete":
        return FoundryAgentInvocationResult(
            status="incomplete",
            response_id=response_id,
            request_id=request_id,
            error_code=_incomplete_reason(body),
            message="Foundry agent response was incomplete.",
        )
    if response_status not in (None, "completed"):
        return FoundryAgentInvocationResult(
            status="failed",
            response_id=response_id,
            request_id=request_id,
            error_code=_safe_identifier_or_none(f"response_{response_status}") or "response_failed",
            message="Foundry agent response was not completed.",
        )

    if _contains_refusal(body):
        return FoundryAgentInvocationResult(
            status="policy_refusal",
            response_id=response_id,
            request_id=request_id,
            error_code="refusal",
            message="Foundry agent refused the request.",
        )

    output_text = _extract_output_text(body)
    if not output_text:
        return FoundryAgentInvocationResult(
            status="invalid_response",
            response_id=response_id,
            request_id=request_id,
            error_code="empty_output",
            message="Foundry agent returned no output text.",
        )

    try:
        payload = json.loads(output_text)
        agent_response = GenerateCardAgentResponse.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return FoundryAgentInvocationResult(
            status="invalid_response",
            response_id=response_id,
            request_id=request_id,
            error_code="schema_validation_failed",
            message="Foundry agent output did not match the card schema.",
        )

    if agent_response.status != "completed":
        return FoundryAgentInvocationResult(
            status=agent_response.status,
            response_id=response_id,
            request_id=request_id,
            schema_valid=True,
            agent_version=_metadata_version(agent_response.metadata),
            error_code=_safe_identifier_or_none(f"agent_{agent_response.status}"),
            message="Foundry agent returned a non-success status.",
        )

    agent_version = _metadata_version(agent_response.metadata)
    if expected_version and agent_version != expected_version:
        return FoundryAgentInvocationResult(
            status="version_mismatch",
            response_id=response_id,
            request_id=request_id,
            schema_valid=True,
            agent_version=agent_version,
            error_code="agent_version_mismatch",
            message="Foundry agent reported an unexpected application version.",
        )

    return FoundryAgentInvocationResult(
        status="completed",
        success=True,
        schema_valid=True,
        response_id=response_id,
        request_id=request_id,
        agent_version=agent_version,
        card=agent_response.card,
        art_prompt=agent_response.artPrompt,
        message="Foundry agent invocation completed.",
    )


def _contains_refusal(body: Mapping[str, Any]) -> bool:
    if _string_or_none(body.get("refusal")):
        return True
    output = body.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "refusal":
            return True
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and (
                    part.get("type") == "refusal" or part.get("refusal")
                ):
                    return True
    return False


def _extract_output_text(body: Mapping[str, Any]) -> str | None:
    output = body.get("output")
    if not isinstance(output, list):
        return None
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            return None
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            return None
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                return None
            text = part.get("text")
            if not isinstance(text, str):
                return None
            chunks.append(text)
    return "".join(chunks) or None


def _metadata_version(metadata: Mapping[str, Any]) -> str | None:
    for key in ("agentVersion", "version"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return _safe_identifier_or_none(value)
    return None


def _incomplete_reason(body: Mapping[str, Any]) -> str | None:
    details = body.get("incomplete_details")
    if isinstance(details, dict):
        reason = details.get("reason")
        safe_reason = _safe_identifier_or_none(reason)
        if safe_reason:
            return safe_reason
    return "incomplete"


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_identifier_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", stripped):
        return stripped
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-invoke a configured Foundry hosted agent.")
    parser.add_argument("query", nargs="?", help="Non-production smoke-test query to send.")
    parser.add_argument("--project-endpoint", help="Override FOUNDRY_PROJECT_ENDPOINT.")
    parser.add_argument("--agent-name", help="Override FOUNDRY_AGENT_NAME.")
    parser.add_argument("--api-version", help="Override FOUNDRY_AGENT_API_VERSION.")
    parser.add_argument("--expected-version", help="Override FOUNDRY_AGENT_EXPECTED_VERSION.")
    parser.add_argument("--timeout", type=float, help="Override FOUNDRY_AGENT_TIMEOUT_SECONDS.")
    parser.add_argument(
        "--allow-nonprod-live",
        action="store_true",
        help="Confirm this bounded smoke invocation may call the non-production live agent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.query:
        raise SystemExit("query is required for invocation; use --help for usage.")
    settings = load_app_settings()
    settings = _settings_with_overrides(settings, args)
    if settings.app_env == "production" or not args.allow_nonprod_live:
        raise SystemExit(
            "Refusing live agent call without explicit non-production smoke confirmation."
        )

    result = asyncio.run(_invoke_for_cli(settings, args.query))
    summary = {
        "status": result.status,
        "success": result.success,
        "retryable": result.retryable,
        "schemaValid": result.schema_valid,
        "responseId": result.response_id,
        "requestId": result.request_id,
        "agentVersion": result.agent_version,
        "errorCode": result.error_code,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result.success else 2


async def _invoke_for_cli(settings: AppSettings, query: str) -> FoundryAgentInvocationResult:
    async with FoundryAgentClient(settings) as client:
        return await client.invoke(query)


def _settings_with_overrides(settings: AppSettings, args: argparse.Namespace) -> AppSettings:
    from dataclasses import replace

    timeout = args.timeout if args.timeout is not None else settings.foundry_agent_timeout_seconds
    if timeout <= 0 or not isfinite(timeout):
        raise SystemExit("FOUNDRY_AGENT_TIMEOUT_SECONDS must be positive and finite.")
    return replace(
        settings,
        foundry_project_endpoint=args.project_endpoint or settings.foundry_project_endpoint,
        foundry_agent_name=args.agent_name or settings.foundry_agent_name,
        foundry_agent_api_version=args.api_version or settings.foundry_agent_api_version,
        foundry_agent_expected_version=args.expected_version
        or settings.foundry_agent_expected_version,
        foundry_agent_timeout_seconds=timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
