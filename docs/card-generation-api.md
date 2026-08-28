# Card generation API

## Stable API boundary

Programmatic card-generation endpoints live under `/api/v1`:

- `POST /api/v1/cards/generate`
- `POST /api/v1/cards/{cardId}/artwork/retry`

HTMX/UI routes live under `/ui/...` and are intentionally UI-coupled.

## Authentication, ownership, and CSRF

- Authentication is required for all generation and asset-delivery operations.
- Ownership is derived from `tid + oid` when present; the fallback is
  `tid + sub`, then `sub` alone if the identity provider omits tenant/object
  identifiers.
- Browser and HTMX mutations must include the session-bound CSRF token.

## Request contract

### `POST /api/v1/cards/generate`

```json
{
  "prompt": "Create a safe fantasy guardian with a moonlit shield",
  "idempotencyKey": "optional-client-supplied-key",
  "csrfToken": "required-for-session-authenticated-browser-clients"
}
```

### `POST /api/v1/cards/{cardId}/artwork/retry`

```json
{
  "idempotencyKey": "optional-client-supplied-key",
  "csrfToken": "required-for-session-authenticated-browser-clients"
}
```

## Response contract

Successful responses return `200 OK` with schema version `1`.

```json
{
  "schemaVersion": 1,
  "cardId": "32-char-deterministic-id",
  "status": "completed",
  "requestId": "request-correlation-id",
  "idempotencyKey": "client-or-server-key",
  "ownerId": "tenant-id:object-id",
  "name": "Moonlit Guardian",
  "cardType": "hero",
  "rarity": "rare",
  "manaCost": 6,
  "attack": 8,
  "health": 9,
  "rulesText": "When played, ...",
  "flavorText": "Forged from a single idea: ...",
  "imageUrl": "/cards/{cardId}/image",
  "actions": []
}
```

If text generation succeeds but artwork does not, the response still returns
`200 OK` with `status: "awaiting_artwork_retry"` and a `retry_artwork` action.

## Error contract

Errors return `application/problem+json`, for example:

```json
{
  "type": "/problems/prompt-rejected",
  "title": "Prompt Rejected",
  "status": 422,
  "detail": "The prompt was rejected by the moderation policy.",
  "instance": "/api/v1/cards/generate",
  "errorCode": "prompt_rejected",
  "requestId": "request-correlation-id"
}
```

## Moderation policy

The MVP uses `MODERATION_POLICY_NAME=conservative-v1` with a conservative
heuristic service that blocks:

- living-artist imitation requests (`"in the style of"`, `"living artist"`)
- obvious copyrighted-logo / trademark requests
- explicit self-harm, sexual-minor, or graphic-gore prompts

The service applies moderation at:

1. the user prompt before any model call
2. the validated structured card text
3. the derived artwork prompt
4. the generated image payload before publication

Unsafe generated image bytes are discarded. Only a minimal sanitized forensic
audit record is retained for 30 days; prompts, unsafe outputs, secrets, tokens,
and SAS URLs are never logged.

## Runtime configuration

### Mock mode

Use deterministic local mode for tests and local UI work:

```dotenv
AI_MODE=mock
PERSISTENCE_MODE=memory
```

### Live mode

Use Azure-backed mode in deployed environments:

```dotenv
AI_MODE=live
PERSISTENCE_MODE=azure
FOUNDRY_ENDPOINT=<https endpoint>
FOUNDRY_TEXT_DEPLOYMENT=gpt-5-5
FOUNDRY_IMAGE_DEPLOYMENT=gpt-image-2
COSMOS_ENDPOINT=<https endpoint>
COSMOS_DATABASE_NAME=appdb
COSMOS_CONTAINER_NAME=cards
BLOB_ENDPOINT=<https endpoint>
BLOB_CONTAINER_NAME=card-assets
```

Additional operational settings:

- `RATE_LIMIT_USER_REQUESTS`, `RATE_LIMIT_USER_WINDOW_SECONDS`
- `RATE_LIMIT_IP_REQUESTS`, `RATE_LIMIT_IP_WINDOW_SECONDS`
- `UPSTREAM_MAX_RETRIES`, `UPSTREAM_BASE_BACKOFF_SECONDS`
- `UPSTREAM_TIMEOUT_SECONDS`, `OVERALL_TIMEOUT_SECONDS`
- `AUDIT_RETENTION_DAYS`
- `IMAGE_SIZE`
