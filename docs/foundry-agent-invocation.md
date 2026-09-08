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

No hosted `card-orchestrator` agent is deployed by this branch. Passing unit tests or mock transports is not evidence of live end-to-end success; a real smoke test requires deployment of the merged endpoint/RBAC wiring and an existing configured agent endpoint.

## Dev acceptance gates for #109

PR #117 supplies the endpoint, opt-in RBAC and operator client, not a hosted
runtime. Keep `/generate` on direct inference throughout these gates.

### 1. Read-only inventory (Gimli)

Run from the repository with an authenticated Azure CLI operator:

```bash
python scripts/foundry_dev_preflight.py \
  --subscription <dev-subscription-id> \
  --resource-group <dev-resource-group> \
  --account <existing-foundry-account> \
  --project <existing-project> \
  --container-app <existing-app>
```

This uses five bounded read-only Azure queries (30 seconds each, no retries).
It neither reads `.env`/azd values nor installs CLI extensions, acquires printable
tokens, invokes an agent, provisions resources, or changes credentials. The
resource group must have `azd-env-name=dev` and the single application container
must declare `APP_ENV=dev` or `development`. Only allowlisted metadata is emitted.
CLI stderr is replaced with a diagnostic code; authentication, authorization,
missing resources and known network restrictions are distinct failures.

Exit 2 means a blocked/inconclusive preflight; exit 0 means only the enumerated
prerequisites passed. `liveAcceptance` is **always false**. Exact role/principal/
scope matches are required: inherited broader roles do not satisfy the intended
least-privilege wiring. A paginated inventory is inconclusive rather than proof
that an agent is absent. Agent name presence does not prove hosted type, readiness,
version, protocol, schema, or target-identity access.

The report records configured image, latest ready revision and traffic for a
subsequent rollout review. These are not a complete rollback artifact: resolve
every traffic-bearing revision and its immutable image digest before deployment;
the template image need not equal the image serving a split traffic allocation.

### 2. Supply the real hosted runtime (Aragorn + Gandalf)

Before provisioning, identify the reviewed `card-orchestrator` source/package,
immutable image, deployed hosted version, Responses endpoint, request/response
contract above, and synthetic input. Confirm dev regional capacity, network path,
and project-identity image-pull/connection requirements. Runtime/platform creation
is a separate scoped decision, not permission to create an echo agent, deploy a
new platform, increase capacity, or relax networking to make this test pass.
Coordinate monitoring ownership and the separate agent rollout/rollback through
existing issue #99 rather than creating a duplicate operational workstream.

### 3. Review the dev-only change set (Gimli)

- Preserve the currently serving ACA image and traffic. `containerImage` must be
  explicit: its empty default selects the bootstrap image.
- Review `azure.yaml` and both provision hooks before any azd command that can run
  hooks. `postprovision` calls `az ad app credential reset --append`, so a blanket
  `azd up`/`azd provision` is **not** an RBAC-only operation. Do not run that path
  without an approved hook-safe rollout plan; never rotate credentials for this
  acceptance test.
- Use Bicep and azd orchestration with a reviewed preview before applying dev
  changes. Require a diff limited to the intended endpoint/role wiring and
  explicitly approved runtime prerequisites; reject unexpected app image,
  traffic, network, model-capacity, production or credential changes.
- Enable `ENABLE_FOUNDRY_AGENT_ACCESS` only for the approved dev rollout. Re-run
  the inventory after RBAC propagation; do not broaden roles to resolve a 403.
- Record newly created assignment IDs separately from pre-existing grants.
  Incremental Bicep deployment with the flag false **does not revoke** assignments.
  Rollback must explicitly remove only grants created by this rollout under the
  approved change process, and restore the recorded image/config/traffic if changed.

### 4. Prove the target identity, not the operator (Gimli + Aragorn)

The existing smoke CLI uses `DefaultAzureCredential`; a local successful command
can therefore use the operator account and **does not** validate ACA RBAC.
Run the bounded test inside the existing dev ACA container, at a reviewed revision
containing the merged client, with an explicitly constructed
`azure.identity.aio.ManagedIdentityCredential()` passed to
`FoundryAgentClient(settings, credential=credential)`. Use an async context manager
to close that supplied credential. Do not fall back to CLI, developer, or
environment credentials. Confirm the app's system-assigned principal matches the
inventory, the project endpoint is the reviewed dev endpoint, and the real hosted
agent/version is active before sending one synthetic request.

Use the existing timeout (5 seconds by default), no automatic retries and no
production input, image, user identity or persistence. Output only the smoke
summary fields documented above; never print the token, card or raw response.
Record UTC time, source commit, ACA revision and principal, hosted version,
request/response IDs, schema validity and result on #109. A refusal, timeout,
permission error, unknown version, or malformed schema is not successful
acceptance. Keep #109 open until a schema-valid completed response from the real
agent is observed using the target managed identity.
