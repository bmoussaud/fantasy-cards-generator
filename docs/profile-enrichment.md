# Public profile suggestions

**Update my profile** offers an optional **Find fantasy role and skills** action
in My Profile. It uses only the existing Hosted `card-orchestrator` and Foundry
managed Web Search. Normal application login is required; there is no second
sign-in, Microsoft 365 data retrieval, new app registration, secret or OAuth scope.
Work IQ is deferred in [#183](https://github.com/bmoussaud/fantasy-cards-generator/issues/183).

## Consent and data boundaries

The search button sits beside **Display name or pseudonym**, stacking below it
on narrow screens. There is no separate public-name or public-context input.
The adjacent consent checkbox is **unchecked by default**; explicitly check it
and activate **Find fantasy role and skills** to send the current display name
to public Web Search. **About public search** opens the contextual help.
There is no additional confirmation. Do not use an internal
or sensitive name for this public action.

The server uses only the submitted `display_name`, normalized for whitespace,
never a saved name, legacy `public_name`/`public_context`, or account identity as
a search fallback. Missing/blank names and email addresses are rejected before
dispatch. The visible display name can still have its normal saved/imported/account
default on page load; this does not trigger a search or grant consent.
Typing, page load, **Save Profile** and implicit Enter never initiate lookup,
even with consent checked. A native default Save submitter preserves Enter
behavior after placing the lookup button earlier in the form. No JavaScript is
required: the lookup submitter uses its explicit `formaction`, and the backend
enforces consent and input validation. JavaScript adds immediate validation/focus,
pending feedback and duplicate-submit protection.
Name edits, including an explicitly blank name, survive errors and Cancel.
No other profile field, private explanation, excerpt or photo is a search input.

The authenticated, CSRF-protected `POST /my/profile/enrich` sends a strict
`enrich_profile` envelope containing only `schemaVersion`, `operation`,
`transactionId`, `publicName` and `publicContext`. `publicName` is the current
display name and `publicContext` is always empty in this editor flow; the existing
Hosted contract is unchanged. The transaction identifier is
control metadata, not model input. The backend authenticates to Hosted using its
workload identity, not a user bearer or forwarded owner header.

The first isolated model request uses the existing managed `web_search` factory
and query `<display name> Explicit expertise`, with no additional public context.
It must abstain for namesakes, missing evidence or ambiguous identity. A matched
finding requires nonempty public evidence and provider URL citations with titles.
Only that evidence reaches the second, tool-free request. This request invents a
tailored original humorous fantasy trading-card role and magical skills grounded
in demonstrated professional capabilities. It must not treat interests, mentions,
aspirations, source instructions or unrelated text as professional qualifications.
It must not invent certifications, copy named cards or branded rules, or claim
affiliation. Source text remains untrusted data in both stages.

The accepted proposal has a nonblank role of 2–120 characters and 1–5 distinct,
nonblank skills of 2–80 characters each. These are freeform original fiction, not
a closed taxonomy or verified qualifications. Deterministic validation enforces
shape and bounds; grounding, identity matching, originality and prompt-injection
resistance still require live evaluation and human review.

Each ability also has an editable private explanation: 2–3 concrete fictional
sentences describing its effect and use, 40–1,000 characters, plain text without
markup or embedded control characters. The existing tool-free synthesis generates
the explanations in the same model call, in ability order. No extra model call
or Web Search input is added. Successful private response schema version **2**
requires one valid explanation per ability; an older agent response without them
fails closed rather than pretending to supply detailed explanations.

The web backend and Hosted runtime moderate the complete proposal, including
explanations. Save Profile revalidates each name/explanation pair and moderates
the edited explanations before writing the owner-scoped profile. Explanations
are shown as escaped editable text only in My Profile. They are never appended
to the Generator prompt, card data, My Cards, public API or telemetry.

## Review, cancellation and persistence

Proposals and public references render only in the editor response. References
come from the public search response, never the synthesis. They are restricted to
public HTTPS URLs, escaped in HTML and opened with `noopener noreferrer`.
A successful response fills the visible **Display name**, **Title**, **Current
role** and **Explicit expertise** fields immediately, with no intermediate Apply
click. The name is the normalized public name explicitly submitted by the user,
never a model-generated identity. The exact deployed Hosted v17 contract returns
role and skills but no title; the web application therefore uses the explicitly
fictional local category **Fantasy trading-card persona** (or **Fictional card
adventurer** if the role itself equals that category). This is not a researched
professional title or a duplicate of the returned role. No new Hosted contract,
version or deployment is needed for the title compatibility behavior alone.
Explanation support requires the compatible schema-v2 Hosted release and matching
web source; a local web restart cannot update the remote Hosted agent.

All filled fields remain editable. **Save Profile** is still the explicit
persistence boundary. Other draft fields, including excerpts, remain unchanged.
Abstentions, refusals, cancellation, stale revisions and errors leave the original
draft intact. No citations or source evidence are saved with the profile or card.
The old Apply endpoint remains compatible with already-open older editor pages,
but new pages do not render that action or duplicate proposed fields.

Existing saved profiles without explanations remain valid and display empty
explanation fields. Description/name ranks are checked before empty ability slots
are removed, preventing an explanation from moving to another ability. A partial
set of explanations or an orphaned explanation is rejected without saving, with
the submitted editor text preserved. All explanations can be cleared deliberately
to retain the legacy names-only profile; otherwise every populated ability needs
its explanation. Cancellation and unsuccessful lookup preserve the current draft.

The GET editor establishes the session key before the long POST. While the lookup
is pending, **Cancel search** replaces **Find fantasy role and skills** in the
same position and receives keyboard focus. Cancel is a normal form submission
that carries the current draft; with JavaScript off it remains available as a
separate control. A spinner and live status show progress without disabling form
data, and duplicate lookup or cancel submissions are blocked. The status can announce
pending feedback. The existing reduced-motion preference also limits the spinner.
This means the request is being submitted/waited on, not that the server has
accepted it or found evidence. Actual outcomes appear separately in a bordered
server-result notice, including errors.

Missing name or consent blocks submission with a visible status message and focus
on the specific control that needs attention; no pending state or lookup starts.
Cancel or another submitted action restores the idle button. Normal HTML
navigation remains in use, including without JavaScript: server responses replace
the page; transport failures are reported by the browser rather than converted
to success. Returning to the editor restores the idle controls through page
show/hide events, including back/forward cache, without changing draft inputs or
automatically retrying. A reload or navigation may
discard unsaved edits as with any ordinary HTML form; use Cancel to preserve them.

Transactions use the existing owner-partitioned repository, short TTL and CAS
admission. At most one active request per owner/session is accepted. Revision
checks run before dispatch and after results. Cancellation, logout, expiry or a
changed saved revision prevents dispatch after an awaited credential lookup or
discards late results. Late responses never restore the session cookie. Cancellation
does not undo a provider call already dispatched or its cost. No background queue
or automatic provider retry is introduced. Paid calls share generation's existing
per-user and trusted-client-IP rate limiter.

Public model responses and transport bodies are capped at 65,536 bytes; public
search and synthesis each have a 30-second budget within the bounded request.
The backend has a 120-second request budget. Refusals win, input/output moderation
remains active, and diagnostics never include source text or provider exceptions.
No evidence, timeout, malformed response or refusal yields a fallback success.

## Default-off activation

Set `PROFILE_ENRICHMENT_ENABLED=true` explicitly on **both** the web container and
the exact deployed Hosted agent version to enable the operation. The root
`azure.yaml` passes `${PROFILE_ENRICHMENT_ENABLED=false}` to Hosted.
`infra/main.parameters.json` maps that same default-off value to
`profileEnrichmentEnabled`; the root Bicep module passes it to the web container
environment. Missing/false configuration leaves manual editing available.

No provisioning is needed merely to release this code to an existing configured
environment. Follow the repository's targeted release and immutable Hosted-version
procedures; keep exact agent routing and web readiness aligned. Do not provision
new resources, permissions or secrets for this feature.

## Validation scope

The enrichment backend and Hosted tests use fixtures and real pinned SDK
serialization with mock HTTP transports. They cover payload/header boundaries,
admission/cancellation/logout, revision checks, response bounds, provenance,
abstentions and error handling. Scripted model abstentions are not proof of live
semantic quality, source relevance, originality or injection resistance.
An enabled exact-runtime managed Web Search smoke and human review remain release
verification work; offline success is not a deployment claim.
