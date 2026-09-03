# Card generation API

## Stable API boundary

Programmatic card-generation endpoints live under `/api/v1`:

- `POST /api/v1/cards/generate`
- `POST /api/v1/cards/{cardId}/artwork/retry`

Authenticated photo-library endpoints live under `/my/...`:

- `POST /my/photos`
- `GET /my/photos`
- `GET /my/photos/{photoId}/image`
- `GET /my/photos/{photoId}/thumbnail`
- `DELETE /my/photos/{photoId}`

HTMX/UI routes live under `/ui/...` and are intentionally UI-coupled.

## Correlation

Clients may send `X-Request-ID` using 1–64 ASCII letters, digits, dots,
underscores, colons, or hyphens, beginning with a letter or digit. Invalid
values are replaced, and the accepted/generated value is returned in the
response header and body.

W3C `traceparent` remains the distributed trace identity. `X-Request-ID` is a
diagnostic attribute only: it is not used as a trace ID, metric dimension, or
outbound third-party header. Telemetry uses normalized routes and never records
prompts, generated content, identities, card/blob IDs, request bodies, query
values, credentials, cookies, authorization headers, emails, or client IPs.

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
  "csrfToken": "required-for-session-authenticated-browser-clients",
  "savedPhotoId": "optional-saved-photo-id"
}
```

Multipart `POST /api/v1/cards/generate` and `POST /ui/cards/generate` also accept:

- `photo` — optional fresh JPEG/PNG/WebP upload (max 5 MB)
- `saved_photo_id` — optional saved photo ID
- `save_photo` — optional boolean; when `true`, the fresh upload is also persisted
- `photo_label` — optional 1–80 char label used only when `save_photo=true`

`photo` and `saved_photo_id`/`savedPhotoId` are mutually exclusive. Sending both returns
`422 photo_reference_conflict`.

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

### `POST /my/photos`

Returns `201 Created`:

```json
{
  "schemaVersion": 1,
  "photoId": "32-char-or-uuid-style-id",
  "label": "Optional label",
  "createdAt": "2026-09-03T10:00:00Z",
  "updatedAt": "2026-09-03T10:00:00Z",
  "image": {
    "contentType": "image/png",
    "sizeBytes": 12345,
    "url": "/my/photos/{photoId}/image"
  },
  "thumbnail": {
    "contentType": "image/png",
    "url": "/my/photos/{photoId}/thumbnail"
  }
}
```

### `GET /my/photos`

Returns `200 OK`:

```json
{
  "schemaVersion": 1,
  "photos": [
    {
      "schemaVersion": 1,
      "photoId": "photo-id",
      "label": "Optional label",
      "createdAt": "2026-09-03T10:00:00Z",
      "updatedAt": "2026-09-03T10:00:00Z",
      "image": {
        "contentType": "image/png",
        "sizeBytes": 12345,
        "url": "/my/photos/{photoId}/image"
      },
      "thumbnail": {
        "contentType": "image/png",
        "url": "/my/photos/{photoId}/thumbnail"
      }
    }
  ]
}
```

`DELETE /my/photos/{photoId}` returns `204 No Content`. The `image` and `thumbnail`
routes stream owner-scoped bytes and return `404` for non-owned or missing photos.

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

Saved-photo persistence uses Azure AI Content Safety image analysis before
Blob/Cosmos persistence. The backend rejects saves when any of
`Hate/Sexual/Violence/SelfHarm` exceeds the configured threshold (defaults: `2`
for all categories, meaning medium/high severity are rejected).

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
PROFILE_PHOTOS_CONTAINER_NAME=profile-photos
CONTENT_SAFETY_ENDPOINT=<https endpoint>
CONTENT_SAFETY_API_VERSION=2024-09-01
```

Additional operational settings:

- `RATE_LIMIT_USER_REQUESTS`, `RATE_LIMIT_USER_WINDOW_SECONDS`
- `RATE_LIMIT_IP_REQUESTS`, `RATE_LIMIT_IP_WINDOW_SECONDS`
- `TRUSTED_PROXY_HOPS` (`0` by default; set to `1` behind Azure Container Apps ingress so the app trusts only ACA's rightmost appended `X-Forwarded-For` hop)
- `UPSTREAM_MAX_RETRIES`, `IMAGE_MAX_RETRIES`, `UPSTREAM_BASE_BACKOFF_SECONDS`
- `TEXT_TIMEOUT_SECONDS`, `IMAGE_TIMEOUT_SECONDS`, `OVERALL_TIMEOUT_SECONDS`
- `AUDIT_RETENTION_DAYS`
- `IMAGE_SIZE`
- `IMAGE_QUALITY`
- `SAVED_PHOTO_MAX_COUNT`
- `SAVED_PHOTO_MAX_BYTES`
- `SAVED_PHOTO_THUMBNAIL_SIZE`
