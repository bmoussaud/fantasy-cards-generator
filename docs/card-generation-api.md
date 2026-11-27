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

The separate Sweden Central DEV for #194 selects `MAI-Image-2.7` on its own
Foundry account and configures `IMAGE_SIZE=1024x1536`. Its image endpoint is
derived from that account when the operator leaves the override empty.
`gpt-image-2` remains deployed there as an explicitly selected operator
alternative, never an automatic error, timeout, or moderation fallback.
This changes deployment configuration, not the API or UI request contract.
See the [execution record](dev-sweden-central-implementation-plan.md) for
live acceptance evidence and outstanding checks.

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

Prompts must contain 12 to 2,500 characters after trimming, and at least 12
characters after collapsing repeated whitespace. The generator form enforces
these bounds before submission; server-side validation remains authoritative.
Blank optional string fields are treated as absent.

### Saved browser profile contract

The dedicated `/my/profile` page contains only profile management: identity
fields and expertise/post-excerpt fieldsets use two columns on wider screens
and stack on mobile. It has no generator, reference-photo picker or card preview.
The single **Save Profile and Generate** button saves the profile and opens
`/app?saved=true#generate-card`, without starting AI generation automatically.
Validation, moderation or revision-conflict errors keep the editor and submitted
values visible instead of redirecting.
The `/app` Generator shows a compact, read-only summary of the saved profile:
name, title, role and expertise, with optional recent-post excerpts collapsed
under a native disclosure. Its Edit profile link opens `/my/profile`; there
are no editable profile fields or profile-save button in Generator.
Without a saved profile, it links to profile creation and offers no generation
form. Creative direction and the reference-photo picker remain generation-only.
The shared navigation emphasizes the current page and announces it with
`aria-current="page"`. Signed-in identity appears once in the header, with the
display name and available email address.
`POST /my/profile` saves the authenticated owner's
profile using the session CSRF token and the hidden `profile_revision`. An empty
revision creates the profile; subsequent saves require the current revision.
Concurrent creation or a stale revision returns `409` with the submitted values
preserved and a reload link rather than overwriting another edit.

Exactly one `professional-profile` document (`profile:current`, schema version 1)
is stored per authenticated owner in the existing Cosmos `/userId` partition.
Creation is conditional and replacement uses the Cosmos ETag. The document has
no expiry (`ttl: -1`), needs no container migration, and cannot collide with card
or audit IDs. Azure mode persists across instances and sign-ins; the existing
memory mode remains ephemeral for development/tests. Account deletion removes
the profile as well as cards and photos. Profile data is not kept in session
cookies, and authenticated responses use `Cache-Control: no-store`.

The saved profile contains:

- editable display name or pseudonym: 1–80 characters
- professional title: 2–120 characters, distinct from the current role
- current role: 2–120 characters
- one to five explicit expertise statements: 2–80 characters each
- up to three existing recent-post excerpts: 20–500 characters each. My Profile
  no longer edits them; saving from that editor preserves existing excerpts.

My Profile follows the card's reading order: display name, an illustration
note, title and role, then the existing expertise fields presented as card
abilities. Public-search suggestions are separate from manual editing and do
not save the profile. The illustration note is not an upload field.

After saving, the UI offers generation from that saved profile, with a server-owned
art-direction menu and the existing photo picker. The initial catalog contains
`paper_arcana` (default), `engraved_legends`, and `paper_cut_myths`; only these
short identifiers are submitted by the browser. Their MAI Image 2.7 prompts remain
server-side. An optional additional direction (0–200 characters) lets the user add
scene, mood, palette, or character details without replacing the selected style. Generator
preselects the first three saved expertise entries (or all if fewer). Only
Up to three checked entries become named abilities, in saved profile order, with an
agent-written explanation for each. The same abilities appear in the generation
result and My Cards. Clearing all checkboxes leaves the printed ability panel empty;
`rulesText` remains available in the API/semantic HTML but is never drawn on the card.
Cards generated from the saved-profile UI print the profile's display name in the
top plaque and its current role in the central type plaque. These exact values are
stored in additive `profileInscription` metadata so artwork retries and My Cards
keep the original inscription even if the profile changes later. Historical cards
and public API generations without this metadata continue to use their generated
card name and type line.
This choice never edits the saved
profile. If the profile changed since the page loaded, reload Generator before
submitting the selection again. Generating named abilities requires a Hosted
agent that accepts `selectedExpertise` and returns the matching `abilities`;
incomplete responses fail before artwork generation. The public JSON API
and previously saved cards retain their existing contract. Direction is
specific to each card, not stored in the profile; absent, empty and whitespace-only
values are accepted. The initial Generator form suggests a storybook anime portrait
of a smiling wizard in a gold-trimmed blue cloak. Users can edit or clear this
suggestion; it is not an API default. HTMX result/error updates preserve the
form and its current value, including an explicit empty value.

For all new cards, the backend requests **only an illustration**
from the image provider: no typography, card frame, UI, stats, rules or named
powers. The effective prompt is built on the web from the validated visual
`artBrief`, not the Hosted agent's precomposed-card `artPrompt`; its bounded input
keeps it below 4,000 characters. The same prompt is moderated, used for ordinary
generation or reference-photo editing, and stored for artwork retry. The subject's
appearance can come from the reference; its text/layout must not be copied.
The provider paints a complete, expressive fantasy scene for a **1.19:1 window**,
with luminous focal lighting even at night. A reference photo is used for likeness
only; the scene is generated or photo-edited once, never edited again to extract
a subject. The WEB moderates the actual prompt and image before composition.

The default [`cadre-classique@1` recipe](../app/card_recipes/cadre_classique_v1.yaml)
produces a **1024×1536** portrait. The illustration stays within a rectangular
window covering half the height. Pillow supplies the bold serif title, two original
cost marks, type plaque, tinted rules panel with large serif text and italic flavor,
rarity badge, lower-right attack/health plate and **Illus. Azure AI Foundry** credit.
Ivory, blue, rose, green, violet and gold are color identities selected by `accentColor`
(per-type fallback); rare and legendary finishes add gold independently of identity.

The text specialist writes the effects in English. The card image, generation result
and My Cards detail display each selected expertise name (`abilities[].name`)
exactly as saved, in selection order, even when the name is not in English.
The WEB still validates and moderates `inscription.abilityNames` for wire compatibility
but does not use these labels for display. For profile-backed generation,
`profileInscription.current_role` replaces the generated subtype on the printed card.
Otherwise, the WEB uses `inscription.subtype` to print the type line, e.g.
`Creature — Human Knight`. The hosted agent carries
inscription and accent in optional metadata, not
new keys in its strict wire card. Older agents still work: their existing labels
are preserved without attempting an unverified translation. The current recipe
accepts up to three selected abilities, 20–120 characters per effect and up to 90
for flavor; older agents returning larger payloads retain the historical layout.

Text is wrapped without truncation. The renderer first shrinks ability typography
to recipe minima, then omits flavor if necessary. It reserves the stats plate
and fails explicitly if the fixed panel still cannot hold the abilities.
Inputs are limited to PNG/JPEG/WebP, 32 MiB and 4,096 pixels per source edge.
Bundled fonts fail explicitly on unsupported characters rather than replacement boxes.
The recipe uses unmodified [EB Garamond from Google Fonts](https://github.com/google/fonts/tree/main/ofl/ebgaramond),
distributed with its [SIL Open Font License](../app/assets/fonts/eb-garamond/OFL.txt).
The historical renderer retains Noto Sans and its [SIL Open Font License](../app/assets/fonts/OFL.txt);
the unmodified font is from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/notosans),
content blob `75575046c015ff623a848096a15779867ba71453`.

Generation and successful artwork retries share finalization. New cards, including
pending cards, persist `artworkLayout: "cadre_classique_v1"` and
`card_style: {recipe: "cadre-classique", version: 1, accent: "#RRGGBB"}`.
The private `artworkAssets` map stores the scene snapshot, not a second subject;
the main blob contains the final PNG. No source-asset public
URLs are introduced. `recompose_stored_card` reuses these assets without a provider
call after the caller authorizes the owner. Compensation and card/account deletion
cover every asset. Retries retain the stored style/layout and do not regenerate text.
Previously stored [`portrait-debordant@1`](../app/card_recipes/portrait_debordant_v1.yaml)
cards retain their 750×1050 composition, original labels and optional subject snapshot.
Only retries on that older recipe still perform the moderated cutout edit.
Historical `illustration_only_v1` cards keep their growing Noto renderer; cards
without a layout marker keep their original prompt/footer behavior. Historical
payloads still accept five abilities, 180-character descriptions and 280-character flavor.
The additive `blob.abilitiesEmbedded` marker identifies completed inscribed files;
missing markers mean historical artwork and are never inferred from card text.
The JSON response contract is unchanged. With an inscribed image available,
Generator/My Cards keep a semantic, screen-reader-accessible HTML copy without a
visible duplicate. For the new complete-card layout, exterior HTML title/stats/flavor
are also visually hidden while the image works, leaving a single visible card.
These copies become visible if artwork fails to load. Pending and
historical cards keep the visible ability text. Library thumbnails show the full
composed file without cropping. Existing image files are byte-for-byte unchanged;
new zero-ability cards also use the recipe. Only the card's validated,
moderated abilities are rendered, never private My Profile explanations.
Composition failures use the existing explicit `persistence_failure` response;
no image is uploaded or marked inscribed, and an existing retryable card survives.

### Recipe evidence

The historical portrait-debordant [four-rarity sheet](assets/card-recipes/portrait-debordant-v1.png) and
[six-thumbnail mobile grid](assets/card-recipes/portrait-debordant-mobile.png)
use one original live-generated scene and extracted subject with four accents.
They isolate layout/finish differences, not four separate generation attempts.
Recognizing the visual family in three seconds remains a human acceptance check.

One dev `gpt-image-2` probe (API `2025-04-01-preview`, low quality, 1536×1024)
returned HTTP 200 for both calls: scene 15.245 s (88 input / 158 output tokens),
extraction 28.523 s (1,592 input / 158 output tokens). The extracted PNG is RGBA
with 39.15% fully transparent pixels. This establishes support on that deployment,
not guaranteed success or a latency percentile. Monetary cost is not inferred:
contracted token prices and Azure billing reconciliation remain unverified.

The loading status stays in the result column's persistent empty-state shell.
Only `#generation-content` is swapped for cards, marked HTML errors or artwork
retries, so `#generation-pending` remains available for subsequent generations
and is hidden while idle.
HTMX disables the Generate card submit button for the duration of its request
to prevent repeated clicks, then re-enables it after success or HTTP/transport
failure, unless an image-service cooldown is still active. Other generation
options remain editable.
Reference-photo failures preserve actionable public error codes in both JSON
and the HTMX error panel: `reference_image_rejected` (422) for an upstream
photo/input rejection, `image_rate_limited` (429) for image-service throttling,
and `image_service_unauthorized` or `image_service_misconfigured` (503) for
application authorization or endpoint/deployment failures.
An upstream numeric `Retry-After` delay is retained when positive, capped at
3,600 seconds. On marked image429 HTML failures, the error panel displays this
delay and JavaScript temporarily disables Generate card and any displayed
artwork-retry submit buttons. The countdown preserves the form, including its
photo selection, and enables manual retry at expiry, never an automatic request.
Missing or invalid delays retain the generic error without inventing a cooldown.
This is page-local guidance, not shared quota enforcement; successful partial-card
responses retain their existing artwork-retry contract. Image-service request
limits are shared by all clients of the deployment, so disabling double-clicks
does not eliminate quota errors. `reference_image_unsupported` (502) requires
an explicit unsupported-operation code, not just HTTP400 or an invalid image.
Other reference-image failures remain `reference_image_generation_failed`
(502). A rejected photo is never silently discarded in favor of photo-free
generation. Moderation refusals remain `generated_art_rejected` and take
precedence over technical error classification.
Content-free dependency diagnostics retain only bounded HTTP statuses and
allowlisted error codes, never the upstream response body or uploaded photo.
On an image-edit `invalid_image_file` response, `image.reference_rejected`
adds finite format/mode categories, Pillow verification and MIME-agreement
booleans, and bucketed byte size/longest edge. These are diagnostic hints, not
proof that the provider accepts the input. Raw dimensions, filenames, MIME
strings, image bytes, prompts and provider messages are not retained.
Before an edit request, the backend decodes the reference locally. Already
canonical RGB/RGBA PNG, JPEG and WebP files with matching content types are
preserved. Other decodable encodings, mismatched MIME types and non-RGB(A) modes
are converted to metadata-free RGB(A) PNG without resizing or dropping the
reference. Unreadable/unsafe inputs or converted files at least50MB fail
explicitly before any provider request; there is no photo-free fallback.
Unsaved edits are not used. Direct JSON, multipart or form
calls to `/ui/cards/generate` also require a saved profile; submitted prompts or
profile fields cannot replace the server-loaded profile. The public programmatic
`/api/v1/cards/generate` prompt contract remains unchanged.

After canonical whitespace normalization, the composed context must contain
12–2,500 Unicode characters. Sections are composed in deterministic order:
identity, professional title, current role, explicit expertise, optional recent-post excerpts, then
optional additional creative direction. The complete canonical context is moderated and
used for request hashing and idempotency. The selected art-style identifier is also
part of the request hash, and both the selected style prompt and the moderated
additional direction are explicitly included in the derived image prompt. Profile
validation reserves enough room
for the full 200-character direction, including its label and separators, so any
valid direction fits. The profile is also moderated before it is saved; refusal
preserves the prior saved version.

Detailed content captures are suppressed for the deterministic professional
context in both web and hosted runtimes, including artwork retry in the web
runtime. Content-free operational telemetry remains available. Generated cards
still retain their generation context as before: editing the profile does not
rewrite or erase historical cards. Delete those cards or the account to remove
those copies.

A one-time LinkedIn OIDC import may prefill only the editable display name and,
when separately consented and safely copied, an owner-scoped saved-photo
reference. LinkedIn does not supply the role, expertise, post excerpts, powers,
or creative direction. The UI accepts no LinkedIn profile URL. Imported values
remain editable, and manual completion works without LinkedIn. A successful
profile save consumes the bound import draft. Imports never silently overwrite
an existing saved profile.

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

If text generation succeeds but artwork encounters a technical failure, the
response still returns `200 OK` with `status: "awaiting_artwork_retry"` and a
`retry_artwork` action. A policy refusal is not a technical partial success.

An artwork retry does not regenerate validated text. On upload or completed-card
save failure it returns `503 persistence_failure`, preserving the pre-existing
safe partial card and its metadata. After recovery, clients can retry with a new
idempotency key. Compensation targets only that attempt's newly uploaded asset,
not the pre-existing card or another attempt's asset; cleanup errors do not mask
the original persistence failure. Failure audits use the retry request identity
in the `audit:` namespace, separate from `card:` documents and initial-generation
audits. Blob and Cosmos operations are not an inter-service transaction: an
ambiguous write or failed cleanup can require reconciliation, and no blind card
restoration is performed over a concurrent save.

### `POST /my/photos`

Returns `201 Created`:

```json
{
  "schemaVersion": 1,
  "photoId": "32-char-or-uuid-style-id",
  "label": "Optional label",
  "source": null,
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
      "source": null,
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

In the browser, uploads are available only on **My Photos**
(`/my/photos/library`). Choose an image, optionally add a label, then select
**Save photo to my library**. The page posts to `/my/photos`, displays moderation,
capacity, and storage errors, and refreshes the library after a successful save.
The upload limit shown in the form follows `SAVED_PHOTO_MAX_BYTES` (4 MB by default).
Every upload from this page is explicitly saved; there is no separate save checkbox.

The generator (`/app`) only selects an existing saved photo, or generates without
one. Its **Add or manage photos** link opens My Photos. The underlying generation
API continues accepting fresh uploads and `save_photo` for existing clients;
these controls are no longer exposed in the generator UI.

Before Microsoft sign-in, users may select the optional photo-import checkbox on
`/auth/login`. The single sign-in callback uses its transient Graph token to copy
the photo into this library, without a second authorization flow or stored token.
The import is a one-time snapshot, labeled
`Microsoft profile photo`, and does not synchronize later profile-photo changes.
Deleting that imported item permanently suppresses automatic re-import for the
account. Import state is durable and uses these lifecycle values:
`not_offered`, `offered`, `declined`, `accepted`, `imported`, `no_photo`,
`failed_retryable`, and `deleted_suppressed`. `accepted` is the short-lived
claim held while an import runs; failures transition to `failed_retryable` so
users can opt in again on a later sign-in. New accounts are claimed atomically;
legacy `not_offered`, `offered`, and `declined` states, as well as `no_photo`,
allow a fresh explicitly requested import. `imported` and `deleted_suppressed`
never trigger another copy. Graph, moderation, validation, capacity, and storage
failures never undo successful authentication. Denied consent before token
validation returns to the sign-in screen instead of establishing a session.

## Error contract

API errors return `application/problem+json`, for example:

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

UI errors return an HTML error panel with the original HTTP status and an
`X-Generation-Error` header containing an allowlisted error code (unknown codes
become `internal_error`). HTMX displays these marked HTML responses only in the
generation result region; it does not convert failures to successful responses.

For a generation `422`, distinguish `validation_error` (request/schema validation)
from `invalid_prompt` (normalized prompt length), photo-input errors, and
`prompt_rejected` / `generated_text_rejected` / `generated_art_rejected` (policy).
An upstream agent HTTP 422 is a non-retryable dependency failure surfaced as
HTTP 502, not a request-validation 422. Agent policy refusals can return 422.
`generated_art_rejected` covers a rejected derived art prompt, a generated image
rejected before publication, and a structured image-provider policy refusal
(`content_filter`, `content_policy_violation`, or `ResponsibleAIPolicyViolation`,
case-insensitive). Supported provider envelopes include `error.code` and nested
`innererror` / `inner_error` causes. HTTP 400 alone, free-text messages, and
`content_filter_error` (filter failure rather than a policy decision) are not
classified as refusals. Public refusal messages are static and never include the
provider response, prompt, credentials, or reference photo.
For diagnostics, share only the HTTP status and the error panel's static
title/detail/code, not form contents, cookies, tokens, or a HAR export.

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

Every policy refusal is deny-wins. The service returns only the bounded response
status and public error code, stops before later stages, and persists no card,
artwork blob, durable reservation, or generation-audit record. This includes
hosted-agent refusal, image-provider refusal for normal generation and
reference-photo editing, and all four moderation stages. A provider policy
refusal is never automatically retried, even when marked retryable upstream.
Operational failures that are not content refusals
may still retain the existing TTL-limited, content-free generation audit.

Refusal-capable processing runs before any durable card or retry-audit
reservation. Same-instance duplicate requests are coalesced in memory; the
durable idempotency reservation is acquired only when a safe completed or
technical-failure partial card is ready to persist. A refused artwork retry
leaves the existing safe partial card unchanged, returns the same explicit
`422 generated_art_rejected` refusal, and creates no new reservation, blob, or
retry audit.

Saved-photo persistence uses Azure AI Content Safety image analysis before
Blob/Cosmos persistence. The backend rejects saves when any of
`Hate/Sexual/Violence/SelfHarm` exceeds the configured threshold (defaults: `2`
for all categories, meaning medium/high severity are rejected).

## Runtime configuration

Application construction always uses Azure-backed clients. Automated tests use
explicit dependency injection for deterministic agent and image clients; no
environment variable can select a mock implementation.

```dotenv
PERSISTENCE_MODE=azure
FOUNDRY_ENDPOINT=<https endpoint>
FOUNDRY_IMAGE_DEPLOYMENT=gpt-image-2
FOUNDRY_PROJECT_ENDPOINT=<https://account.services.ai.azure.com/api/projects/project>
FOUNDRY_AGENT_NAME=card-orchestrator
FOUNDRY_AGENT_VERSION=<exact active hosted version>
FOUNDRY_AGENT_API_VERSION=v1
FOUNDRY_AGENT_TIMEOUT_SECONDS=70
TELEMETRY_ENABLED=true
APPLICATIONINSIGHTS_CONNECTION_STRING=<connection string>
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
- `FOUNDRY_IMAGE_MODEL`, `FOUNDRY_IMAGE_ENDPOINT`
- `SAVED_PHOTO_MAX_COUNT`
- `SAVED_PHOTO_MAX_BYTES`
- `SAVED_PHOTO_THUMBNAIL_SIZE`

### Image model selection

`FOUNDRY_IMAGE_MODEL` selects the image client: `gpt-image-2` (default) or
`MAI-Image-2.7`. No other value is accepted, and there is no fallback between
models: a failing MAI call surfaces as the usual image error or refusal.

| Setting | `gpt-image-2` | `MAI-Image-2.7` |
| --- | --- | --- |
| Endpoint | `FOUNDRY_ENDPOINT` | `FOUNDRY_IMAGE_ENDPOINT` (`https://<account>.services.ai.azure.com`; defaults to the managed account when `AI_FOUNDRY_MAI_IMAGE_DEPLOYMENT_ENABLED=true`); text and moderation endpoints are unchanged |
| Generation | `POST /openai/deployments/{deployment}/images/generations?api-version=2025-04-01-preview`, JSON `prompt`/`size`/`quality` | `POST /mai/v1/images/generations`, JSON `model`/`prompt`/`width`/`height`, no `api-version` |
| Reference edit | multipart `images/edits` with `size`, `quality` and optional transparency | multipart `/mai/v1/images/edits` with `model`/`prompt`/`width`/`height`/`image`; WEBP references are converted to PNG |
| Default `IMAGE_SIZE` | `1024x1536` | `1104x928`; each side ≥ 768 px and at most 1,572,864 pixels |
| `IMAGE_QUALITY` | Sent | Not sent; telemetry records `not_applicable`, the card keeps the requested value |
| Subject extraction (`portrait-debordant@1` retries) | Transparent-background edit | Skipped: this adapter does not request transparent-background edits; the scene is kept alone |

Authentication is the app managed identity's Entra token for
`https://cognitiveservices.azure.com/.default`; no key is used. With
`AI_FOUNDRY_MAI_IMAGE_DEPLOYMENT_ENABLED=true`, Bicep deploys MAI on the managed
Foundry account, where the app identity already holds `Cognitive Services User`.
Otherwise the external MAI account and its inference role are managed outside
this template. MAI responses
carry no token usage, so image usage records zero tokens and only latency.
`FOUNDRY_IMAGE_MODEL`, `FOUNDRY_IMAGE_ENDPOINT` and `FOUNDRY_MAI_IMAGE_DEPLOYMENT`
(default `MAI-Image-2.7`) feed the matching Bicep parameters. The MAI edits
reference documents no `width`/`height`; MAI-Image-2.7 accepted them in a DEV
probe that returned a 1024×1536 PNG. Learn publishes no pixel ceiling for 2.7;
the 1,572,864-pixel limit is the largest size verified on DEV.

Startup validates local settings and mandatory telemetry. Dependency readiness
then validates the exact agent name/version, active hosted version status,
the endpoint's single 100% selector for that same version, and managed-identity
access with bounded content-free `GET /agents/{name}/versions/{version}` and
`GET /agents/{name}` requests after the server listens. The
70-second client cap covers
the hosted runtime's 65-second overall budget while remaining inside the
application's 225-second request budget. Missing configuration, identity/RBAC
failure, inactive or absent versions, and telemetry initialization failure
prevent readiness. Timeout, transient availability/rate limiting,
authentication/authorization/configuration, invalid output, and policy refusal
retain distinct structured public errors. Prompts, outputs, response bodies,
tokens, URLs, and raw exceptions are never logged.
