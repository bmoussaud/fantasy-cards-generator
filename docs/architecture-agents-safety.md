# Moderation and safety layer contract

This document describes every active moderation and safety layer in the
`fantasy-cards-generator` backend, what each layer checks, what it returns,
how failures are classified, and the explicit precedence rules that govern
conflicts between layers.

It is scoped to the **current implementation** and the **agreed future
contract** for any future Foundry agent/guardrail integration work.
Sections that describe future behavior are clearly marked **[FUTURE]**.

> Cross-reference: see [the Foundry architecture summary](architecture-agents-foundry.md#moderation-and-safety-contract-issue-101)
> for the integration-facing view of this contract.

---

## 1. Layers at a glance

| # | Layer | Scope | Status | Authority |
|---|-------|-------|--------|-----------|
| 1 | Heuristic moderation | Card text + generated image | **Active** | Authoritative |
| 2 | Azure AI Content Safety | Saved-photo uploads only (library uploads and `save_photo=true` inline uploads) | **Active** | Authoritative |
| 3 | Foundry hosted-agent guardrails | Agent-generated card text | **[FUTURE]** | Authoritative |
| 4 | Advisory safety skill | Card concept (pre-image budget) | **[FUTURE — optional]** | Advisory only |

"Authoritative" means a BLOCK from that layer **cannot** be overridden by
any agent, fallback, or retry path. "Advisory only" means the layer may
flag risk but the final enforcement decision remains with an authoritative
layer.

An inactive or not-yet-deployed layer is **not** an implicit pass. Under
the future contract, missing required moderation evidence is
INDETERMINATE rather than allow (see §6).

---

## 2. Layer 1 — Heuristic moderation (card text pipeline)

### What it is

`HeuristicModerationService` (`app/generation.py:459–528`). A pure-Python,
in-process, deterministic service. No external API calls. For the current
call sites and validated inputs, it returns a `ModerationDecision` rather
than using an external failure path.

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

Every current call returns exactly one `ModerationDecision`. If a pattern
matches or a check fails it returns `allowed=False`; otherwise it returns
`allowed=True`.

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

Every moderation stage that is actually reached appends its
`ModerationDecision` to the in-memory `moderation` list
(`app/generation.py:1947–2135`). Completed cards and partial
`awaiting_artwork_retry` cards persist only the decisions reached before
persistence (`_persist_completed()` / `_persist_partial()`).

Early text-stage denials do **not** leave behind a completed/partial card with
"all four" decisions. Those paths delete the card document, save an
`audit_failed` record via `_save_audit_failure()`, and stop before later
stages run.

### Idempotency replay for content blocks

When an idempotency replay finds an `audit_failed` record,
`_problem_from_audit()` first rehydrates the stored structured failure fields
(`failure_status_code`, `failure_title`, `failure_detail`, `failure_type`,
optional headers) when they are present (`app/generation.py:2595–2610`).
That is the normal path for current audit records written by
`_save_audit_failure()`.

Only when those structured fields are absent does `_problem_from_audit()` fall
back to a narrow legacy/incomplete-record heuristic: three older moderation
reason codes replay as `422 Prompt Rejected`, and other legacy failures replay
as `503 Service Unavailable`. Content denials are therefore not silently
retried, but the fallback set is **not** the primary replay mechanism.

---

## 3. Layer 2 — Azure AI Content Safety (reference photo uploads)

### What it is

`ContentSafetyPhotoModerationService` (`app/photos.py:355–468`). An async
HTTP client that calls the Azure AI Content Safety image-analysis endpoint.
Today it runs **only on the saved-photo write path**: direct library uploads
and generation requests that use `save_photo=true` (because those requests
call `SavedPhotoService.save_photo()` first). It does **not** run on card
text, generated images, or unsaved inline reference-image uploads.

### Configuration

| Environment variable | Default | Notes |
|----------------------|---------|-------|
| `CONTENT_SAFETY_ENDPOINT` | `FOUNDRY_ENDPOINT` fallback | If absent, saved-photo upload is **blocked** (503) |
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
`SavedPhotoService.save_photo()` (`app/photos.py:483–524`) before the photo is
written to Blob Storage. It is called:

1. When a user uploads a photo to the library (`POST /my/photos`,
   `app/main.py:419`).
2. When a user submits a card-generation request with `save_photo=true` and
   an inline photo upload (`app/main.py:732–735`, `app/main.py:864–866`).

Photos loaded from the library for use in card generation (via
`photo_service.load_reference_image()`) were already safety-checked at upload
time; they are **not** re-checked at generation time.

**Current gap:** inline uploads used directly for reference-image generation
with `save_photo=false` bypass this layer entirely.

### Pre-safety size gate

Before calling Content Safety, `save_photo()` checks that the photo does
not exceed `SAVED_PHOTO_MAX_BYTES` (default 4 MB, `app/photos.py:491–500`).
Oversized photos return `413 saved_photo_too_large` before any Content
Safety call is made.

### API call

```
POST {CONTENT_SAFETY_ENDPOINT}/contentsafety/image:analyze
     ?api-version={CONTENT_SAFETY_API_VERSION}
Content-Type: application/json
Authorization: ******

{
  "image": {"content": "<base64-encoded bytes>"},
  "categories": ["Hate", "SelfHarm", "Sexual", "Violence"],
  "outputType": "FourSeverityLevels"
}
```

Categories constant: `CONTENT_SAFETY_CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")`
(`app/photos.py:28`).

`FourSeverityLevels` is requested so the response is expected to carry integer
severity scores in `{0, 2, 4, 6}`.

### Decision logic

```python
# app/photos.py:386–398
analysis = response.get("categoriesAnalysis") or []
rejected = [r for r in results if r.severity > threshold_for_category(r.category)]
```

Severity **strictly greater than** the configured threshold triggers a
rejection. At the default threshold of `2`, severities `4` and `6` are
blocked; severities `0` and `2` are allowed.

**Current gap:** the implementation does **not** require explicit evidence for
all requested categories. `categoriesAnalysis` missing or empty becomes `[]`;
missing `severity` becomes `0`; missing/unknown `category` becomes `""` and
uses threshold `0`. If no parsed result exceeds a threshold, the photo is
allowed. The current service therefore does **not** enforce “all categories
explicitly allowed” yet. The future contract should treat missing required
category evidence as INDETERMINATE / block, not as allow.

### Output

| Condition | External result | HTTP status |
|-----------|-----------------|------------|
| Endpoint not configured (`CONTENT_SAFETY_ENDPOINT`/fallback absent) | `photo_moderation_unconfigured` | `503` |
| Azure API returned HTTP `4xx`/`5xx` | `photo_moderation_unavailable` | `503` |
| Any parsed category severity > threshold | `saved_photo_rejected` | `422` |
| No parsed result exceeds threshold | Returns `list[ContentSafetyCategoryResult]` | — (no exception) |
| `httpx.RequestError`, credential acquisition failure, malformed success JSON, or malformed success response shape | **Not normalized by this service** | Service-level behavior undefined here; outer exception handling decides |

`_post()` wraps only HTTP error responses in `UpstreamServiceError`
(`app/photos.py:445–456`). `assert_allowed()` then converts that wrapped
error into a single `503 photo_moderation_unavailable` response. Upstream
status codes and Azure `error.code` values are therefore kept for internal
classification/logging only; they are not exposed as distinct public problem
codes by this service.

The service does **not** promise that every failure becomes a named `503`.
Transport errors, token failures, `response.json()` parsing failures, and
success-payload shape errors currently escape the `UpstreamServiceError` catch
and are not normalized here.

### Critical: unconfigured endpoint is a BLOCK, not a pass

If `CONTENT_SAFETY_ENDPOINT` is absent (and `FOUNDRY_ENDPOINT` is also
absent), `assert_allowed()` raises `503 photo_moderation_unconfigured` and
the saved-photo write is rejected. The system does not silently skip Content
Safety on that saved-photo path. This must remain true for any future agent
or fallback path as well.

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

Today's shipped layers are mostly disjoint rather than comprehensive:
heuristic moderation handles card text and generated-image payload checks,
while Content Safety handles only the saved-photo write path. There is
therefore little true multi-layer arbitration on the same payload today — and
there are also known bypass/missing-evidence gaps that must not be described
as stronger than they are.

### 5.1 Text pipeline — heuristic only

No second layer exists for card text today. The rules are:

1. Heuristic decision at `pre_prompt` runs first. BLOCK → abort immediately;
   no further stages run.
2. Heuristic decisions at `post_text`, then `post_art_prompt`, run in
   sequence. Each BLOCK aborts the pipeline.
3. Heuristic decision at `post_image` runs only after image generation/edit
   succeeds. BLOCK → partial persist (no abort, no HTTP error).

### 5.2 Reference photo pipeline — saved-photo gate only

No heuristic layer applies to uploaded reference photos. Content Safety is
currently the sole authoritative gate **only when the request actually goes
through `save_photo()`**. Unsaved inline reference-image uploads do not hit
Content Safety today.

### 5.3 General precedence rules (current behavior vs future policy)

| Rule | Current implementation | Required future contract |
|------|------------------------|--------------------------|
| **Authoritative deny wins** | Heuristic text denials abort; saved-photo Content Safety denials reject the save; post-image heuristic denial preserves safe text and marks artwork retryable. | Any authoritative deny — including a managed guardrail denial — must stop the protected path. No later allow may reverse it. |
| **Optional advisory signals are not authoritative** | No advisory agent layer exists today. | An optional safety-review skill may advise only. That does **not** apply to the orchestrator's own structured refusal/failure payload or to managed guardrail denials. |
| **Structured refusal/failure is not permission** | N/A today. | If the hosted orchestrator cannot safely continue and returns a structured refusal/failure payload, the backend must map that outcome to an error response. Ambiguous or unclassified refusal is not an allow and must not trigger a fail-open retry/direct path. |
| **Required layer unavailable or indeterminate → not pass** | Saved-photo Content Safety unconfigured/HTTP-error cases block. But missing category evidence is a current gap that can still allow. | Required active layers must either return sufficient evidence or block/hold as INDETERMINATE. Missing evidence must not be treated as allow. |
| **Inactive is not outage** | Foundry guardrails are not integrated, so they are simply not applicable today. | A layer intentionally not adopted/configured as required is inactive; a layer configured as required but unreachable is an outage. |
| **Fallback cannot weaken safety** | Current code does not fall back from saved-photo moderation to a lighter path, but unsaved inline reference images already bypass Content Safety as a known gap. | Any future direct fallback may run only after an eligible technical failure and must rerun every required active safety layer on the replacement output, or fail/hold if that cannot be done. |

---

## 6. Decision classification

This table distinguishes current enforced outcomes, current gaps, and future
agent-layer outcomes without inventing an agent status schema that does not
yet exist.

| Outcome | Classification | Current producer | HTTP / external effect |
|---------|---------------|-----------------|------------------------|
| Heuristic BLOCK (`pre_prompt`, `post_text`, `post_art_prompt`) | Content denial | `HeuristicModerationService` | `422` |
| Heuristic BLOCK (`post_image`) | Content denial on artwork; safe text preserved | `HeuristicModerationService` | `200` + `awaiting_artwork_retry` |
| Content Safety threshold exceedance | Content denial | `ContentSafetyPhotoModerationService` | `422 saved_photo_rejected` |
| Content Safety endpoint unconfigured on saved-photo path | Configuration failure (BLOCK) | `ContentSafetyPhotoModerationService` | `503 photo_moderation_unconfigured` |
| Content Safety HTTP `4xx`/`5xx` from Azure | Technical failure (BLOCK) | `_post()` → `assert_allowed()` | `503 photo_moderation_unavailable` |
| Content Safety transport/credential/malformed-success-response exception | Unhandled service exception | `_post()` / `assert_allowed()` | Not normalized here; outer exception handling decides (typically generic `500` if uncaught) |
| Content Safety missing/empty category evidence | **Current gap** | `assert_allowed()` parsing defaults | May incorrectly allow |
| Text model timeout/5xx (retries exhausted) | Transient technical failure | `_retry_upstream` | `504` |
| Text model non-retryable error or invalid structured output | Upstream / technical failure | `_retry_upstream` / model validation | `502` |
| Image model timeout/failure (no reference image) | Transient technical failure (partial) | `_retry_upstream` | `200` + `awaiting_artwork_retry` |
| Image edit timeout/failure (reference image) before any image exists | Upstream / technical failure | `_retry_upstream` / `_reference_image_problem()` | `502` or `504` |
| Successful image/edit followed by post-image heuristic BLOCK | Content denial on artwork only | `HeuristicModerationService` | `200` + `awaiting_artwork_retry` on both reference and non-reference paths |
| **[FUTURE]** Managed guardrail denial | Authoritative denial | Foundry runtime + backend mapping | Backend-mapped `ProblemDetails`; no fallback around the deny |
| **[FUTURE]** Hosted orchestrator structured refusal/failure payload | Authoritative refusal/failure input for backend | Hosted orchestrator + backend mapping | Backend-mapped `ProblemDetails`; no fail-open continuation |
| **[FUTURE]** Optional advisory safety-skill flag | Advisory signal only | Safety Review Specialist | Agent-internal/advisory only |

---

## 7. Retry and fallback rules by layer

### 7.1 Heuristic moderation retries

Not applicable. The service is in-process and deterministic. Identical input
always produces identical output. The idempotency-replay behavior described
in §2 ensures prior denials replay as denials rather than being silently
retried.

### 7.2 Content Safety retries

`assert_allowed()` does not retry internally. A single attempt is made; any
wrapped upstream HTTP error becomes `503 photo_moderation_unavailable`. The
caller (the photo save endpoint) does not retry.

Retryable status codes for telemetry/classification purposes remain
`{408, 429, 500, 502, 503, 504}` (`app/photos.py:451–456`). Those codes do
not change the external problem shape.

### 7.3 Image generation and `awaiting_artwork_retry`

Image generation does not retry automatically (`IMAGE_MAX_RETRIES=0` by
default, `app/settings.py:197`). When image generation fails or times out
after valid card text exists **and** no reference image was used, the backend
persists a partial record and returns `status="awaiting_artwork_retry"`. A
later explicit retry call (`POST /api/v1/cards/{card_id}/artwork/retry`) can
attempt image generation again for that persisted card.

For the reference-image edit path, **upstream edit failure** remains a hard
error (`502`/`504`) and does not create an `awaiting_artwork_retry` record.

However, once either image path has successfully produced image bytes, the
post-image heuristic moderation stage behaves the same on both paths: a BLOCK
quarantines/rejects the artwork, preserves the already-validated text/art
prompt, persists `awaiting_artwork_retry`, and returns `200`. `BLOCK wins` at
that stage applies to the generated image output; it does not require deleting
otherwise safe validated text.

### 7.4 [FUTURE] Agent hop retries

Per the latency budget decision in
[the Foundry architecture doc](architecture-agents-foundry.md#latency-budget-findings-issue-97),
the agent hop keeps its agreed budgets unchanged: one 5 s attempt, one 3 s
retry, and 0.15 s backoff (**8.15 s total**). The degraded legacy direct-text
path likewise keeps its emergency budget unchanged (**30.15 s total**).

That future direct fallback is eligible **only for technical failure modes**
(such as timeout, retryable overload, transport failure, or invalid technical
output) where no authoritative deny has already occurred. It must **not** run
after a managed guardrail denial, after an orchestrator structured
refusal/failure that the backend interprets as a safety/policy stop, or when a
required active safety layer is unavailable/indeterminate for the replacement
path.

If fallback is used, the replacement path must still run every required active
safety layer that applies to its own output. If a required active layer cannot
run or does not return sufficient evidence, the result is fail/hold — not a
heuristic-only escape hatch.

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
   Safety when it was uploaded through `save_photo()`. `load_reference_image()`
   retrieves the saved bytes from Blob and constructs a `ReferenceImageUpload`
   without a second Content Safety call.

3. **Post-image moderation after generation/edit.** If the image generation or
   image-edit call succeeds, the backend still runs heuristic `post_image`
   checks on the generated output. A BLOCK there preserves valid text and
   leaves the card in `awaiting_artwork_retry`; it is not a full-pipeline hard
   failure.

**Gap (current implementation):** An inline photo used with `save_photo=false`
bypasses Content Safety. Only the post-image heuristic moderation (which
checks PNG magic bytes and labels, not content safety categories) runs on the
resulting generated image. This is an open implementation gap to be addressed
in a future issue; it is not addressed in this issue.

---

## 9. [FUTURE] Foundry guardrails integration contract

This section describes the policy/runtime contract for any future Foundry
guardrail adoption. It is **not** current behavior, and issue #109 by itself
is only the endpoint/config/RBAC invocation integration follow-up — not proof
that a full hosted runtime plus guardrail implementation is already enforced.

### Guardrail scope

If adopted and configured as required, Foundry guardrails would apply at the
hosted `card-orchestrator` boundary, covering agent-generated card text and
art-prompt output.

### Precedence

Managed guardrail denials are authoritative. A denial from an adopted required
guardrail must stop the protected path, and the request must not continue via
a weaker retry/direct fallback. The backend still owns interpretation and
client error mapping; this document does not define a concrete shipped agent
status schema.

### Outage vs inactive

| State | Classification | Behavior |
|-------|----------------|---------|
| Guardrails adopted/configured as required and reachable | Active | Normal enforcement |
| Guardrails adopted/configured as required but unreachable | Required-layer outage | Return failure/hold; do not continue |
| Guardrails not yet adopted or intentionally optional in this deployment | Inactive (not applicable) | Not an outage; existing authoritative layers remain in force |

An intentionally inactive Foundry guardrail layer must not surface as a false
`503`. Conversely, a required guardrail outage must not be downgraded into a
heuristic-only pass.

### Layer ordering post-integration

When guardrails are active, the intended ordering remains:

1. Heuristic `pre_prompt` (before agent call)
2. Foundry guardrails at/around the hosted orchestrator boundary
3. Heuristic `post_text` + `post_art_prompt` on the received text output
4. Image generation/edit
5. Heuristic `post_image`

This does **not** mean every later stage runs after an earlier authoritative
block. Early denies still short-circuit later work. It does mean that every
required active layer applicable to the chosen successful path must run on that
path's output before the result is treated as allowed.

### Advisory safety skill

If a MAF safety-review skill is added (see
[the Foundry architecture doc](architecture-agents-foundry.md#5-safety-review-specialist--optional-advisory-only)),
its output remains advisory. It may flag risk or suggest extra scrutiny, but
it does **not** replace deterministic/backend-managed enforcement and does not
turn an orchestrator refusal or managed guardrail denial into something
optional.

---

## 10. Open implementation gaps

| Gap | Detail | Tracking |
|-----|--------|---------|
| Inline reference photo Content Safety bypass | Photos submitted inline with `save_photo=false` are not checked by Content Safety before the image-edit call. | Open — future issue |
| Content Safety missing-evidence gap | `categoriesAnalysis` may be absent/empty, categories may be missing, and severities may default to `0`; the current parser can still allow without complete required evidence. | Open — policy/runtime follow-up |
| Content Safety exception-normalization gap | `assert_allowed()` normalizes only `UpstreamServiceError` (HTTP error responses). Transport errors, credential failures, malformed JSON, and malformed success payload shapes are not normalized here. | Open — backend hardening follow-up |
| Foundry guardrails not integrated | No guardrail layer exists for agent-generated text. If guardrails are later adopted as required, their runtime/policy enforcement is a follow-up beyond #109's endpoint/config/RBAC integration scope. | Open — future runtime/policy follow-up |
| `MODERATION_SERVICE` single-value lock | `app/settings.py:231` enforces `MODERATION_SERVICE == "heuristic"`. Extending additional moderation engines into text/image paths requires revisiting that validation guard. | Open — scope of future runtime integration issue |
| Advisory safety skill not implemented | The optional MAF safety-review skill described in the architecture is not built. | Open — proposed phase 2+ |
