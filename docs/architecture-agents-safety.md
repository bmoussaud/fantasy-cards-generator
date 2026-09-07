# Moderation and safety layer contract

This document describes every active moderation and safety layer in the
`fantasy-cards-generator` backend, what each layer checks, what it returns,
how failures are classified, and the explicit precedence rules that govern
conflicts between layers.

It is scoped to the **current implementation** and the **agreed future
contract** for when the proposed Foundry agent layer is added (issue #109).
Sections that describe future behavior are clearly marked **[FUTURE]**.

> Cross-reference: see `docs/architecture-agents-foundry.md §Moderation and
> safety contract (issue #101)` for the summary and integration context.

---

## 1. Layers at a glance

| # | Layer | Scope | Status | Authority |
|---|-------|-------|--------|-----------|
| 1 | Heuristic moderation | Card text + generated image | **Active** | Authoritative |
| 2 | Azure AI Content Safety | Saved/inline reference photos | **Active** | Authoritative |
| 3 | Foundry hosted-agent guardrails | Agent-generated card text | **[FUTURE]** | Authoritative |
| 4 | Advisory safety skill | Card concept (pre-image budget) | **[FUTURE — optional]** | Advisory only |

"Authoritative" means a BLOCK from that layer **cannot** be overridden by
any agent, fallback, or retry path. "Advisory only" means the layer may
flag risk but the final enforcement decision remains with an authoritative
layer.

An inactive or not-yet-deployed layer is **not** an implicit pass. Missing
required moderation evidence is classified as INDETERMINATE (see §6).

---

## 2. Layer 1 — Heuristic moderation (card text pipeline)

### What it is

`HeuristicModerationService` (`app/generation.py:459–528`). A pure-Python,
in-process, deterministic service. No external API calls. Cannot throw an
exception that bypasses its decision; it always returns a `ModerationDecision`.

### Configuration

| Environment variable | Default | Purpose |
|----------------------|---------|---------|
| `MODERATION_SERVICE` | `heuristic` | Must be `heuristic`; the only accepted value (`app/settings.py:231`) |
| `MODERATION_POLICY_NAME` | `conservative-v1` | Human label attached to telemetry and decision details |

### Stages and triggering

The service is called four times per card-generation request in this order
(`app/generation.py:1947–2134`):

| Stage constant | `progress.stage` label | Input text | When called |
|----------------|------------------------|------------|-------------|
| `"pre_prompt"` | `"pre-moderation"` | Normalised user prompt | Before any model call |
| `"post_text"` | `"post-text-moderation"` | `name + rulesText + flavorText + artBrief` | After text model returns |
| `"post_art_prompt"` | `"art-prompt-moderation"` | Derived art prompt (`derive_art_prompt()`) | After art prompt is built |
| `"post_image"` | `"post-image-moderation"` | `ImageResult` (content-type + bytes + labels) | After image model returns |

The first three are text stages; the fourth is an image-payload stage.

### Text moderation — blocked patterns

`moderate_text()` lowercases the input and checks each pattern with Python
`in`. First match wins; remaining patterns are not evaluated.

| Pattern (lowercase substring) | `reasonCode` |
|-------------------------------|-------------|
| `"in the style of"` | `"living-artist-imitation"` |
| `"living artist"` | `"living-artist-imitation"` |
| `"copyrighted logo"` | `"copyrighted-logo"` |
| `"trademark"` | `"trademark-request"` |
| `"disney"` | `"copyrighted-character"` |
| `"pokemon"` | `"copyrighted-character"` |
| `"graphic gore"` | `"graphic-violence"` |
| `"sexual minor"` | `"sexual-content-minor"` |
| `"self-harm"` | `"self-harm"` |
| *(no match)* | `"allowed"` |

### Image moderation — checks

`moderate_image()` is called with an `ImageResult` and performs two checks
(`app/generation.py:495–515`):

| Check | `reasonCode` |
|-------|-------------|
| `"post-image-block"` present in `image.labels` | `"unsafe-generated-image"` |
| `image.content_type != "image/png"` or bytes do not start with `b"\x89PNG"` | `"invalid-image-payload"` |
| Both checks pass | `"allowed"` |

### Output model

```python
class ModerationDecision(BaseModel):          # app/generation.py:201
    stage: Literal["pre_prompt", "post_text", "post_art_prompt", "post_image"]
    allowed: bool
    reasonCode: str
    details: str
```

Every call returns exactly one `ModerationDecision`. The service never
raises an exception; if a pattern matches or a check fails it returns
`allowed=False`, otherwise `allowed=True`.

### Effect of a BLOCK per stage

| Stage | `allowed=False` effect | HTTP status |
|-------|------------------------|-------------|
| `pre_prompt` | Card document deleted; audit failure saved; `ProblemDetails` raised | `422` |
| `post_text` | Card document deleted; audit failure saved; `ProblemDetails` raised | `422` |
| `post_art_prompt` | Card document deleted; audit failure saved; `ProblemDetails` raised | `422` |
| `post_image` | Card persisted as `awaiting_artwork_retry`; audit failure saved; **200 returned** (no exception) | `200` |

The post-image stage intentionally does **not** fail the request. Validated
card text and the derived art prompt already exist; they are persisted so
the user can retry artwork generation later. This asymmetry is by design and
must be preserved for any future layers that run post-image.

### Moderation decisions stored in Cosmos

Every `ModerationDecision` is appended to `StoredCard.moderation`
(`app/generation.py:256, 1951, 2026, 2053, 2134`) and persisted to Cosmos
as part of the card document. All four per-request decisions are stored.

### Idempotency replay for content blocks

When a new request reuses an idempotency key that previously hit an audit
failure, `_problem_from_audit()` (`app/generation.py:2596–2634`) replays
the original refusal. If the stored `error_code` is one of:

- `"prompt_rejected"`
- `"living-artist-imitation"`
- `"copyrighted-character"`

the replay returns `422 Prompt Rejected`, not `503`. Other audit failures
replay as `503 Service Unavailable`. Content blocks are therefore **not
silently retried** through an idempotency replay.

---

## 3. Layer 2 — Azure AI Content Safety (reference photo uploads)

### What it is

`ContentSafetyPhotoModerationService` (`app/photos.py:355–468`). An async
HTTP client that calls the Azure AI Content Safety image-analysis endpoint.
Applies **only to reference photos** (saved and inline). It does **not**
run on card text or generated images.

### Configuration

| Environment variable | Default | Notes |
|----------------------|---------|-------|
| `CONTENT_SAFETY_ENDPOINT` | `FOUNDRY_ENDPOINT` fallback | If absent, upload is **blocked** (503) |
| `CONTENT_SAFETY_API_VERSION` | `2024-09-01` | Azure Content Safety API version |
| `CONTENT_SAFETY_MAX_HATE_SEVERITY` | `2` | Must be 0, 2, 4, or 6 |
| `CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY` | `2` | Must be 0, 2, 4, or 6 |
| `CONTENT_SAFETY_MAX_SEXUAL_SEVERITY` | `2` | Must be 0, 2, 4, or 6 |
| `CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY` | `2` | Must be 0, 2, 4, or 6 |

Authentication: managed identity bearer token from `DefaultAzureCredential`,
scope `https://cognitiveservices.azure.com/.default`.

HTTP timeout: 30 seconds (`app/photos.py:443`).

### When triggered

`ContentSafetyPhotoModerationService.assert_allowed()` is called from
`SavedPhotoService.save_photo()` (`app/photos.py:514`) before the photo is
written to Blob Storage. It is called:

1. When a user uploads a photo to the library (`POST /my/photos`,
   `app/main.py:408–424`).
2. When a user submits a card-generation request with `save_photo=true` and
   an inline photo upload (`app/main.py:732–735`, `app/main.py:863–866`).

Photos loaded from the library for use in card generation (via
`photo_service.load_reference_image()`) were already safety-checked at
upload time; they are **not** re-checked at generation time.

### Pre-safety size gate

Before calling Content Safety, `save_photo()` checks that the photo does
not exceed `SAVED_PHOTO_MAX_BYTES` (default 4 MB, `app/photos.py:500–510`).
Oversized photos return `413 saved_photo_too_large` before any Content
Safety call is made.

### API call

```
POST {CONTENT_SAFETY_ENDPOINT}/contentsafety/image:analyze
     ?api-version={CONTENT_SAFETY_API_VERSION}
Content-Type: application/json
Authorization: Bearer <managed-identity-token>

{
  "image": {"content": "<base64-encoded bytes>"},
  "categories": ["Hate", "SelfHarm", "Sexual", "Violence"],
  "outputType": "FourSeverityLevels"
}
```

Categories constant: `CONTENT_SAFETY_CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")`
(`app/photos.py:28`).

`FourSeverityLevels` produces integer severity scores in `{0, 2, 4, 6}`.

### Decision logic

```python
# app/photos.py:394–415
rejected = [r for r in results if r.severity > threshold_for_category(r.category)]
```

Severity **strictly greater than** the configured threshold triggers a
rejection. At the default threshold of `2`, severities `4` and `6` are
blocked; severities `0` and `2` are allowed.

### Output

| Condition | `error_code` | HTTP status |
|-----------|-------------|------------|
| Endpoint not configured (`CONTENT_SAFETY_ENDPOINT` absent) | `photo_moderation_unconfigured` | `503` |
| Azure API returned HTTP `4xx`/`5xx` | From `error.code` or `"content_safety_failed"` | `503` |
| Network/timeout error (`UpstreamServiceError`) | `photo_moderation_unavailable` | `503` |
| Any category severity > threshold | `saved_photo_rejected` | `422` |
| All categories within threshold | *(list of `ContentSafetyCategoryResult`)* | — (no exception) |

Retryable upstream errors from `_post()` use the status set
`{408, 429, 500, 502, 503, 504}` (`app/photos.py:452–456`). The
`assert_allowed()` wrapper does **not** retry; it converts any
`UpstreamServiceError` into a single `503 photo_moderation_unavailable`
response.

### Critical: unconfigured endpoint is a BLOCK, not a pass

If `CONTENT_SAFETY_ENDPOINT` is absent (and `FOUNDRY_ENDPOINT` is also
absent), `assert_allowed()` raises `503 photo_moderation_unconfigured` and
the photo is rejected. The system **never** silently skips Content Safety
to allow an upload through. This is intentional and must be preserved.

---

## 4. Pre-pipeline deterministic input gates

These constraints run before either moderation layer and are not safety
layers in themselves, but they eliminate obviously invalid inputs early.

| Gate | Location | Condition | HTTP status |
|------|----------|-----------|------------|
| Prompt minimum length | `normalize_prompt()`, `app/generation.py` | `< 12 chars` after trim | `422 invalid_prompt` |
| Reference photo content-type | `ALLOWED_PHOTO_CONTENT_TYPES`, `app/main.py:69` | Not jpeg/png/webp | `422` |
| Reference photo size (inline) | `MAX_REFERENCE_PHOTO_BYTES`, `app/main.py:70` | `> 5 MB` | `413` |
| Saved photo size | `app/photos.py:500` | `> SAVED_PHOTO_MAX_BYTES` (4 MB) | `413 saved_photo_too_large` |
| Saved photo count | `app/photos.py:506` | `>= SAVED_PHOTO_MAX_COUNT` (10) | `409 saved_photo_limit_reached` |

These gates do not produce `ModerationDecision` objects and are not stored
in the moderation audit trail.

---

## 5. Conflict precedence — current implementation

Because heuristic moderation (Layer 1) and Azure Content Safety (Layer 2)
operate on **different inputs at different stages**, there are no conflicts
between them today. The rules below express the implemented behavior in
contract form so they can be preserved when new layers are added.

### 5.1 Text pipeline — heuristic only

No second layer exists for card text. The rules are:

1. Heuristic decision at `pre_prompt` runs first. BLOCK → abort immediately;
   no further stages run.
2. Heuristic decision at `post_text`, then `post_art_prompt` run in
   sequence. Each BLOCK aborts the pipeline.
3. Heuristic decision at `post_image` runs last. BLOCK → partial persist
   (no abort, no HTTP error).

### 5.2 Reference photo pipeline — Content Safety only

No heuristic layer applies to photos. Content Safety is the sole
authoritative gate.

### 5.3 General precedence rules (current + future contract)

These rules apply when multiple layers evaluate the same input.

| Rule | Statement |
|------|-----------|
| **Authoritative BLOCK wins** | If any authoritative layer returns a BLOCK, the request is rejected. No subsequent allow from any other layer or agent can reverse it. |
| **Required layer unavailable → INDETERMINATE, not pass** | If a required active moderation layer cannot be reached (network failure, unconfigured endpoint), the request is blocked or held, not silently passed. |
| **Allow requires explicit allow from all required layers** | A request is only considered allowed when every required active layer has returned an explicit allow. Absence of a decision from a layer that should have run is not an allow. |
| **Inactive/not-yet-deployed layer is not an outage** | A layer that is intentionally inactive (e.g., Foundry guardrails before agent integration) is treated as "not applicable" for this configuration, not as a transient outage. It does not cause an INDETERMINATE result. |
| **Agent refusal ≠ authoritative BLOCK** | A refusal from the Foundry hosted agent (proposed) is a creative or policy guidance signal, not an authoritative safety decision. The backend's deterministic moderation layers remain the authority for enforcement. |
| **Transient errors ≠ content denial** | A 503 from a required moderation service (timeout, API error) must not be treated as a pass. It must surface as a distinct technical failure. Content denials (422) and technical failures (503) must not be conflated in client responses or audit records. |
| **Rejection does not fall back to a less-moderated path** | If the moderation-enabled path is unavailable, the request fails with an appropriate error. The system never degrades to a path with fewer safety gates to serve the request. |

---

## 6. Decision classification

This table consolidates the possible outcomes across both active layers plus
the proposed future layer, distinguishing content decisions from technical
errors.

| Outcome | Classification | Current producer | HTTP |
|---------|---------------|-----------------|------|
| Heuristic BLOCK (text stage) | Content denial | `HeuristicModerationService` | `422` |
| Heuristic BLOCK (image stage) | Content denial (partial) | `HeuristicModerationService` | `200` (awaiting retry) |
| Content Safety BLOCK (severity > threshold) | Content denial | `ContentSafetyPhotoModerationService` | `422` |
| Content Safety endpoint unconfigured | Configuration error (BLOCK) | `ContentSafetyPhotoModerationService` | `503` |
| Content Safety API failure | Transient technical failure (BLOCK) | `ContentSafetyPhotoModerationService` | `503` |
| Text model timeout/5xx (retryable, retries exhausted) | Transient technical failure | `_retry_upstream` | `504` |
| Text model non-retryable error | Upstream failure | `_retry_upstream` | `502`/`503` |
| Image model timeout/failure (no reference image) | Transient technical failure (partial) | `_retry_upstream` | `200` (awaiting retry) |
| Image model timeout/failure (reference image) | Transient technical failure | `_retry_upstream` | `504`/`422` |
| Overall request timeout at image stage | Transient technical failure (partial) | `asyncio.wait_for` | `200` (awaiting retry) |
| Overall request timeout before text stage | Transient technical failure | `asyncio.wait_for` | `504` |
| **[FUTURE]** Foundry guardrail BLOCK | Content denial (authoritative) | Foundry agent layer | `422` |
| **[FUTURE]** Foundry guardrail unavailable | Configuration/transient failure (BLOCK) | Foundry agent layer | `503` |
| **[FUTURE]** Advisory safety skill flag | Advisory signal only | Safety Review Specialist | *(agent-internal)* |

---

## 7. Retry and fallback rules by layer

### 7.1 Heuristic moderation retries

Not applicable. The service is in-process and deterministic. Identical input
always produces identical output. The idempotency-replay behavior described
in §2 ensures past blocks are replayed, not silently retried.

### 7.2 Content Safety retries

`assert_allowed()` does not retry internally. A single attempt is made; any
upstream error raises `503 photo_moderation_unavailable` immediately. The
caller (the photo save endpoint) does not retry.

Retryable status codes for telemetry purposes: `{408, 429, 500, 502, 503, 504}`
(`app/photos.py:452`). These affect how the error is classified in telemetry
but do not change the HTTP response.

### 7.3 Image generation and `awaiting_artwork_retry`

Image generation does not retry automatically (`IMAGE_MAX_RETRIES=0` by
default, `app/settings.py:197`). When image generation fails or times out
after valid card text exists **and** no reference image was used, the backend
persists a partial record and returns `status="awaiting_artwork_retry"`. A
later explicit retry call (`POST /api/v1/cards/{card_id}/artwork/retry`) can
attempt image generation again for that persisted card.

Post-image heuristic moderation also runs on the retried image. A second
BLOCK appends another `ModerationDecision` to the stored list and leaves the
record as `awaiting_artwork_retry` again.

For the **reference-image path**, `awaiting_artwork_retry` is **not** used.
Image-edit failure returns a hard error (`504` or `422`).

### 7.4 [FUTURE] Agent hop retries

Per the latency budget decision (`docs/architecture-agents-foundry.md §Latency
budget findings`): the agent hop gets one 5 s attempt and one 3 s retry
(8.15 s total). If the agent fails, the backend **degrades in-process to the
current heuristic-only text path** (30.15 s emergency budget). This fallback
runs **with** all existing moderation checkpoints intact; it is not a less-
moderated path.

---

## 8. Photo moderation and card generation — interaction

A reference photo may enter a card-generation request in two ways:

1. **Inline upload at generation time.** The photo is parsed by
   `_parse_reference_image()` (`app/main.py:1047`) and passed directly to
   `generate_card()`. If `save_photo=true`, `save_photo()` is called first
   (Content Safety runs). If `save_photo=false`, Content Safety does **not**
   run on the inline photo before card generation — the photo goes straight
   to the image-edit endpoint.

2. **Pre-saved library photo.** The photo was already checked by Content
   Safety when it was uploaded to the library. `load_reference_image()`
   retrieves the saved bytes from Blob and constructs a `ReferenceImageUpload`
   without a second Content Safety call.

**Gap (current implementation):** An inline photo used with `save_photo=false`
bypasses Content Safety. Only the post-image heuristic moderation (which
checks PNG magic bytes and labels, not content safety categories) runs on the
resulting generated image. This is an open implementation gap to be addressed
in a future issue; it is not addressed in this issue.

---

## 9. [FUTURE] Foundry guardrails integration contract

This section describes the agreed contract for when Foundry guardrails are
integrated (issue #109). **Nothing in this section is currently
implemented.**

### Guardrail scope

Foundry guardrails would apply at the hosted `card-orchestrator` agent
boundary, covering agent-generated card text and art-prompt output.

### Precedence

Foundry guardrails are authoritative. A BLOCK from Foundry guardrails has
the same force as a heuristic BLOCK: the generation pipeline must not
continue and the request must not fall back to a less-moderated path.

### Outage vs inactive

| State | Classification | Behavior |
|-------|----------------|---------|
| Guardrails configured and reachable | Active | Normal enforcement |
| Guardrails configured but endpoint unreachable | Transient outage (BLOCK) | Return 503; do not continue |
| Guardrails not yet deployed / intentionally inactive | Inactive (not applicable) | Not an outage; existing heuristic layers remain the sole authority |

An intentionally inactive Foundry guardrail layer must not cause an
INDETERMINATE result or surface as a 503. The backend should have an explicit
configuration flag that distinguishes "guardrails not configured yet" from
"guardrails expected but unreachable".

### Layer ordering post-integration

When guardrails are active, the intended order is:

1. Heuristic `pre_prompt` (before agent call)
2. Foundry guardrails (inside or immediately after agent call)
3. Heuristic `post_text` + `post_art_prompt` (after agent output is received)
4. Image generation
5. Heuristic `post_image`

All layers run. A BLOCK at any authoritative stage terminates the pipeline
with an appropriate error. No layer's allow can override a prior authoritative
BLOCK.

### Advisory safety skill

If a MAF safety-review skill is added (see
`docs/architecture-agents-foundry.md §Safety Review Specialist`), its output
is advisory. It may flag content for increased scrutiny or generate guidance
for the orchestrator, but it does **not** replace any authoritative layer.
A flag from the advisory skill without a corresponding authoritative BLOCK
does not block the request.

---

## 10. Open implementation gaps

| Gap | Detail | Tracking |
|-----|--------|---------|
| Inline reference photo Content Safety bypass | Photos submitted inline with `save_photo=false` are not checked by Content Safety before the image-edit call. | Open — future issue |
| Foundry guardrails not integrated | No guardrail layer exists for agent-generated text. The heuristic service is the only text-moderation authority. | Open — issue #109 |
| `MODERATION_SERVICE` single-value lock | `app/settings.py:231` enforces `MODERATION_SERVICE == "heuristic"`. Adding Azure Content Safety to card text or image paths requires removing this validation guard first. | Open — scope of future runtime integration issue |
| Advisory safety skill not implemented | The optional MAF safety-review skill described in the architecture is not built. | Open — proposed phase 2+ |
