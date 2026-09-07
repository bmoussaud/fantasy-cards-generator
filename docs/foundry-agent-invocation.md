# Foundry hosted-agent invocation smoke test

This project has a minimal invoke-only client for a future Foundry hosted agent. It is not wired into card generation startup or `/generate`; the existing direct Azure OpenAI flow remains unchanged.

## Configuration

Set these only when an operator wants to smoke-test an already deployed agent:

- `FOUNDRY_PROJECT_ENDPOINT`: canonical project endpoint, for example `https://<account>.services.ai.azure.com/api/projects/<project>`
- `FOUNDRY_AGENT_NAME`: hosted agent name, for example `card-orchestrator`
- `FOUNDRY_AGENT_API_VERSION`: defaults to `v1`
- `FOUNDRY_AGENT_EXPECTED_VERSION`: optional application metadata check against the agent response `metadata.agentVersion` or `metadata.version`
- `FOUNDRY_AGENT_TIMEOUT_SECONDS`: defaults to `5.0`

`FOUNDRY_PROJECT_ENDPOINT` is intentionally separate from `FOUNDRY_ENDPOINT`; there is no fallback to the account/model endpoint.

## Manual smoke command

```bash
python -m app.foundry_agent_client \
  --allow-nonprod-live \
  "Create a safe original fire drake trading card"
```

The command refuses production and requires `--allow-nonprod-live` because it can incur model usage. It prints only a sanitized summary: status, schema validity, request IDs, retryability, and reported agent version. It does not print tokens, prompts, raw response bodies, generated card text, or art prompts.

## Wire contract

The client uses Microsoft Entra ID with the `https://ai.azure.com/.default` token scope and posts to the documented hosted-agent Responses protocol endpoint:

```text
POST {projectEndpoint}/agents/{agentName}/endpoint/protocols/openai/responses?api-version=v1
```

Request body:

```json
{
  "store": false,
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "{\"schemaVersion\":1,\"query\":\"...\"}"
        }
      ]
    }
  ]
}
```

No user ID, photo bytes, tool definitions, or tool executions are sent.

The response parser reads the raw Responses wire envelope `output[]/content[]/output_text`, detects refusals before schema validation, treats incomplete or malformed output as non-success, and validates the proposed agent payload:

- `schemaVersion`
- `status`
- `card` using the existing `GeneratedCardModel`
- `artPrompt`
- `metadata`
- `safetyHints`

## Current live gap

No hosted `card-orchestrator` agent is deployed by this branch. Passing unit tests or mock transports is not evidence of live end-to-end success; a real smoke test requires Gimli's infra/RBAC work and an existing configured agent endpoint.
