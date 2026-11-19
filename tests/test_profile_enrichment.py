from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from itsdangerous import TimestampSigner

from app.foundry_agent_client import FoundryAgentClient
from app.profile_enrichment import (
    EnrichmentDraft,
    EnrichmentFailure,
    EnrichmentRepository,
    digest,
    new_transaction,
)
from app.profile_enrichment_contract import ProfileEnrichmentRequest, ProfileEnrichmentResponse
from app.settings import load_app_settings
from tests.conftest import (
    TEST_OWNER_ID,
    extract_hidden_value,
    make_authenticated_client,
    save_test_profile,
)

SAFE_RESULT = {
    "schemaVersion": 2,
    "status": "completed",
    "current_role": "Archmage of clockwork clouds",
    "expertise": ["Summon a helpful debugging sprite"],
    "expertise_descriptions": [
        "A bright sprite reveals hidden faults in enchanted mechanisms. "
        "Send it ahead to inspect a ward before your party enters."
    ],
    "publicReferences": [{"url": "https://example.org/expertise", "title": "Public & biography"}],
}


@pytest.fixture
def enrichment_client(monkeypatch, base_environment):
    monkeypatch.setenv("PROFILE_ENRICHMENT_ENABLED", "true")
    client = make_authenticated_client(monkeypatch)
    calls = []

    async def enrich(request, *, before_dispatch):
        await before_dispatch()
        calls.append(request)
        return ProfileEnrichmentResponse.model_validate(SAFE_RESULT)

    client.app.state.services.agent_client.enrich_profile = enrich
    return client, calls


def draft_form(client, **overrides):
    page = client.get("/my/profile")
    return {
        "display_name": "A pseudonym",
        "title": "Unpublished editor title",
        "current_role": "",
        "expertise": ["", "", "", "", ""],
        "expertise_description": ["", "", "", "", ""],
        "post_excerpt": ["Private manual draft excerpt, never a public query."],
        "csrf_token": extract_hidden_value(page.text, "csrf_token"),
        "profile_revision": extract_hidden_value(page.text, "profile_revision"),
        "public_lookup_consent": "true",
        **overrides,
    }


@pytest.mark.parametrize(
    "name",
    [
        "Synthetic Given Family",
        None,
        "synthetic@example.org",
    ],
)
def test_display_name_is_the_only_identity_input_and_page_load_never_searches(
    enrichment_client, name
):
    client, calls = enrichment_client
    user = {
        "sub": "synthetic-user",
        "owner_id": TEST_OWNER_ID,
        "name": name,
        "email": "synthetic@example.org",
    }
    cookie = (
        TimestampSigner("test-session-secret")
        .sign(base64.b64encode(json.dumps({"user": user}).encode()))
        .decode()
    )
    client.cookies.set("fantasy_cards_session", cookie, domain="testserver.local", path="/")
    save_test_profile(client, display_name="A saved pseudonym")
    page = client.get("/my/profile")
    value = re.search(r'id="display-name"[^>]*value="([^"]*)"', page.text)
    assert value is not None
    assert unescape(value.group(1)) == "A saved pseudonym"
    assert 'name="public_name"' not in page.text
    assert 'name="public_context"' not in page.text
    consent = re.search(r'<input\b[^>]*id="public-lookup-consent"[^>]*>', page.text).group()
    assert "checked" not in consent
    assert "data-profile-default-save" in page.text
    assert not calls


@pytest.mark.parametrize("display_name", ["", 'Edited public name & "alias"'])
@pytest.mark.parametrize("action", ["", "/enrich", "/enrich/cancel", "/enrich/apply"])
def test_rendered_profile_actions_preserve_current_display_name(
    enrichment_client, display_name, action
):
    client, calls = enrichment_client
    form = draft_form(
        client,
        display_name=display_name,
        public_lookup_consent="false",
    )
    page = client.post("/my/profile" + action, data=form)
    assert page.status_code in {200, 422}
    value = re.search(r'id="display-name"[^>]*value="([^"]*)"', page.text)
    assert value is not None
    assert unescape(value.group(1)) == display_name
    assert "Unpublished editor title" in page.text
    assert not calls
    assert asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID)) is None


def test_search_uses_current_display_name_and_ignores_legacy_identity_fields(enrichment_client):
    client, calls = enrichment_client
    save_test_profile(client, display_name="Saved pseudonym")
    form = draft_form(
        client,
        display_name="  Current   Synthetic Name  ",
        public_name="Ignored legacy name",
        public_context="Ignored legacy context",
    )
    response = client.post("/my/profile/enrich", data=form)
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0].publicName == "Current Synthetic Name"
    assert calls[0].publicContext == ""
    saved = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert saved.profile.display_name == "Saved pseudonym"
    assert "Ignored legacy" not in response.text


@pytest.mark.parametrize("name", [None, "", "synthetic@example.org"])
def test_lookup_rejects_missing_or_email_display_name_without_identity_fallback(
    enrichment_client, name
):
    client, calls = enrichment_client
    form = draft_form(client, public_name="Legacy fallback must not be used")
    if name is None:
        form.pop("display_name")
    else:
        form["display_name"] = name
    response = client.post("/my/profile/enrich", data=form)
    assert response.status_code == 422 and not calls
    assert "not an email address" in response.text
    field = re.search(r'<input\b[^>]*id="display-name"[^>]*>', response.text).group()
    assert 'aria-invalid="true"' in field and "autofocus" in field
    assert "Unpublished editor title" in response.text
    assert not client.app.state.services.profile_enrichment_repository._records


def test_save_with_search_consent_still_never_starts_a_lookup(enrichment_client):
    client, calls = enrichment_client
    form = draft_form(
        client,
        current_role="Fictional guardian",
        expertise=["A fictional ward"] + [""] * 4,
    )
    response = client.post("/my/profile", data=form, follow_redirects=False)
    assert response.status_code == 303 and not calls
    assert asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID)) is not None


def test_enrichment_submit_identifies_missing_consent_and_busy_state(enrichment_client):
    client, calls = enrichment_client
    script = client.get("/static/js/app.js").text
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync(0, "utf8");
let focused = null;
class Element {
  constructor() { this.handlers = {}; this.attributes = {}; this.hidden = true; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] || null; }
  removeAttribute(name) { delete this.attributes[name]; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  focus() { focused = this; }
}
const form = new Element(), name = new Element(), consent = new Element();
const progress = new Element(), start = new Element(), cancel = new Element();
const outcome = new Element();
const save = new Element(), windowHandlers = {};
const announcements = [];
Object.defineProperty(progress, "textContent", {
  get() { return announcements.at(-1) || ""; },
  set(value) { announcements.push(value); }
});
start.hidden = false;
outcome.textContent = "Server outcome must remain visible.";
outcome.hidden = false;
start.setAttribute("data-enrichment-start", "");
start.setAttribute("formaction", "/my/profile/enrich");
cancel.setAttribute("data-enrichment-cancel", "");
cancel.setAttribute("formaction", "/my/profile/enrich/cancel");
form.querySelector = selector =>
  selector === "[data-enrichment-start]" ? start :
  selector === "[data-enrichment-cancel]" ? cancel : consent;
vm.runInNewContext(source, {
  document: {
    getElementById(id) {
      return {"profile-editor": form, "display-name": name,
              "enrichment-progress": progress, "enrichment-result": outcome}[id] || null;
    },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, body: new Element()
  },
  window: {
    addEventListener(name, handler) { windowHandlers[name] = handler; },
  }
});
function submit(button) {
  const event = {submitter: button, prevented: false,
                 preventDefault() { this.prevented = true; }};
  form.handlers.submit(event);
  return event;
}
name.value = ""; consent.checked = false;
assert.equal(submit(null).prevented, false);
assert.equal(submit(start).prevented, true);
assert.equal(focused, name);
assert.equal(progress.hidden, false);
assert.match(progress.textContent, /name/i);
assert.equal(name.getAttribute("aria-invalid"), "true");
assert.equal(start.hidden, false);
assert.equal(cancel.hidden, true);
name.value = "synthetic@example.org"; consent.checked = true;
assert.equal(submit(start).prevented, true);
assert.equal(focused, name);
assert.match(progress.textContent, /not an email/);
assert.equal(cancel.hidden, true);
consent.checked = false;
name.value = "Synthetic Given Family";
assert.equal(submit(start).prevented, true);
assert.equal(focused, consent);
assert.match(progress.textContent, /consent/i);
assert.equal(consent.getAttribute("aria-invalid"), "true");
assert.equal(name.getAttribute("aria-invalid"), null);
assert.equal(cancel.hidden, true);
consent.checked = true;
assert.equal(submit(start).prevented, false);
assert.equal(start.hidden, true);
assert.equal(cancel.hidden, false);
assert.equal(focused, cancel);
assert.notEqual(start.disabled, true);
assert.equal(start.getAttribute("formaction"), "/my/profile/enrich");
assert.equal(cancel.getAttribute("formaction"), "/my/profile/enrich/cancel");
assert.equal(consent.getAttribute("aria-invalid"), null);
assert.match(progress.textContent, /Sending.*request/);
assert.equal(submit(start).prevented, true);
const announcementCount = announcements.length;
assert.equal(submit(start).prevented, true);
assert.equal(announcements.length, announcementCount);
assert.equal(submit(cancel).prevented, false);
assert.equal(cancel.hidden, false);
assert.equal(cancel.getAttribute("aria-disabled"), "true");
assert.match(progress.textContent, /Cancelling/);
assert.equal(submit(cancel).prevented, true);
assert.equal(submit(start).prevented, true);
windowHandlers.pageshow();
assert.equal(start.hidden, false);
assert.equal(cancel.hidden, true);
submit(start);
assert.equal(submit(save).prevented, false);
assert.equal(start.hidden, false);
submit(start);
windowHandlers.pagehide();
assert.equal(start.hidden, false);
assert.equal(cancel.hidden, true);
submit(start);
// Returning from a native navigation/transport error, including bfcache.
windowHandlers.pageshow({persisted: true});
assert.equal(start.hidden, false);
assert.equal(cancel.getAttribute("aria-disabled"), null);
assert.equal(progress.hidden, true);
assert.equal(cancel.hidden, true);
assert.equal(outcome.hidden, false);
assert.equal(outcome.textContent, "Server outcome must remain visible.");
assert.equal(name.value, "Synthetic Given Family");
assert.equal(consent.checked, true);
"""
    result = subprocess.run(
        ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert not calls


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("skill_count", [1, 5])
def test_suggestions_fill_editor_then_explicit_save_preserves_saved_excerpts(
    enrichment_client, existing, skill_count
):
    client, calls = enrichment_client
    result = {
        **SAFE_RESULT,
        "expertise": [f"Summon debugging sprite {index}" for index in range(skill_count)],
        "expertise_descriptions": SAFE_RESULT["expertise_descriptions"] * skill_count,
    }

    async def enrich(request, *, before_dispatch):
        await before_dispatch()
        calls.append(request)
        return ProfileEnrichmentResponse.model_validate(result)

    client.app.state.services.agent_client.enrich_profile = enrich
    excerpts = ["A saved excerpt about maintaining reliable platforms."]
    if existing:
        save_test_profile(client, post_excerpt=excerpts)
    form = draft_form(client, display_name="  Synthetic   Example  ")
    page = client.post("/my/profile/enrich", data=form)
    assert page.status_code == 200
    assert "filled into the editor" in page.text
    assert "/my/profile/enrich/apply" not in page.text
    assert 'name="proposed_role"' not in page.text
    assert re.search(r'<input\b[^>]*\bid="display-name"[^>]*\bvalue="Synthetic Example"', page.text)
    assert 'value="Fantasy trading-card persona"' in page.text
    assert 'value="Archmage of clockwork clouds"' in page.text
    expertise_fields = re.findall(r'name="expertise"\s+value="([^"]*)"', page.text)
    assert expertise_fields == result["expertise"] + [""] * (5 - skill_count)
    descriptions = re.findall(
        r'name="expertise_description"[^>]*>(.*?)</textarea>', page.text, re.S
    )
    assert [unescape(value) for value in descriptions] == result["expertise_descriptions"] + [
        ""
    ] * (5 - skill_count)
    assert 'id="enrichment-result"' in page.text
    assert re.search(r"data-enrichment-start[^>]*>\s*Find fantasy role and skills", page.text)
    assert re.search(
        r'data-enrichment-cancel[^>]*>\s*<span class="spinner" aria-hidden="true"></span>',
        page.text,
    )
    assert 'name="post_excerpt"' not in page.text
    assert "Private manual draft excerpt" not in page.text
    assert "Unpublished editor title" not in page.text
    assert "Archmage of clockwork clouds" in page.text
    assert "Public &amp; biography" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in page.headers
    assert len(calls) == 1
    assert calls[0].model_dump() == {
        "schemaVersion": 1,
        "operation": "enrich_profile",
        "publicName": "Synthetic Example",
        "publicContext": "",
        "transactionId": calls[0].transactionId,
    }
    repository = client.app.state.services.profile_repository
    before = asyncio.run(repository.get(TEST_OWNER_ID))
    assert (before is not None) == existing
    response = client.post(
        "/my/profile",
        data={
            **{key: value for key, value in form.items() if key != "post_excerpt"},
            "display_name": "Synthetic Example",
            "title": "Fantasy trading-card persona",
            "current_role": "Edited fictional role",
            "expertise": ["Edited fictional skill"],
            "expertise_description": [
                "The familiar draws a glowing shield around nearby allies. "
                "Call it when the group crosses an unstable magical bridge."
            ],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = asyncio.run(repository.get(TEST_OWNER_ID))
    assert saved.profile.current_role == "Edited fictional role"
    assert saved.profile.display_name == "Synthetic Example"
    assert saved.profile.title == "Fantasy trading-card persona"
    assert saved.profile.post_excerpts == (excerpts if existing else [])
    assert saved.profile.expertise_descriptions == [
        "The familiar draws a glowing shield around nearby allies. "
        "Call it when the group crosses an unstable magical bridge."
    ]
    assert saved.profile.expertise_descriptions[0] in client.get("/my/profile").text
    assert saved.profile.expertise_descriptions[0] not in saved.profile.context()
    assert saved.profile.expertise_descriptions[0] not in client.get("/app").text
    assert "example.org" not in saved.profile.model_dump_json()


@pytest.mark.parametrize("outcome", ["no_evidence", "ambiguous_identity", "refused", "unavailable"])
def test_unsuccessful_enrichment_does_not_replace_editor(enrichment_client, outcome):
    client, calls = enrichment_client

    async def enrich(request, *, before_dispatch):
        await before_dispatch()
        return ProfileEnrichmentResponse(status=outcome)

    client.app.state.services.agent_client.enrich_profile = enrich
    form = draft_form(client, current_role="Original role", expertise=["Original skill"])
    page = client.post("/my/profile/enrich", data=form)
    assert 'value="A pseudonym"' in page.text
    assert 'value="Unpublished editor title"' in page.text
    assert 'value="Original role"' in page.text
    assert 'value="Original skill"' in page.text
    assert "Private manual draft excerpt" not in page.text
    assert "filled into the editor" not in page.text
    assert asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID)) is None


@pytest.mark.parametrize("stage", ["proposal", "save"])
def test_explanation_moderation_refusal_preserves_private_draft(enrichment_client, stage):
    client, calls = enrichment_client
    services = client.app.state.services
    original = services.moderation_service.moderate_text
    seen = []
    description = SAFE_RESULT["expertise_descriptions"][0]

    async def moderate(text, *, stage):
        seen.append(text)
        if description in text:
            return await original("Pokemon wizard", stage=stage)
        return await original(text, stage=stage)

    services.moderation_service.moderate_text = moderate
    form = draft_form(
        client,
        current_role="Original role",
        expertise=["Original skill"],
        expertise_description=[description],
    )
    response = client.post(
        "/my/profile/enrich" if stage == "proposal" else "/my/profile", data=form
    )
    assert response.status_code in {200, 422}
    assert any(description in text for text in seen)
    assert 'value="Original role"' in response.text
    assert description in response.text
    assert "filled into the editor" not in response.text
    assert asyncio.run(services.profile_repository.get(TEST_OWNER_ID)) is None
    assert len(calls) == (1 if stage == "proposal" else 0)


def test_invalid_explanation_save_preserves_paired_editor_inputs(enrichment_client):
    client, calls = enrichment_client
    form = draft_form(
        client,
        current_role="Original role",
        expertise=["Original skill"],
        expertise_description=["One sentence is not a detailed explanation."],
    )
    response = client.post("/my/profile", data=form)
    assert response.status_code == 422
    assert "One sentence is not a detailed explanation." in response.text
    assert 'value="Original skill"' in response.text
    assert "each explanation needs" in response.text
    assert not calls
    assert asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID)) is None


@pytest.mark.parametrize(
    "failure", ["csrf", "consent", "config", "name", "oversize", "anonymous", "stale", "rate"]
)
def test_initiation_rejects_before_sources_and_preserves_valid_draft(
    enrichment_client, monkeypatch, failure
):
    client, calls = enrichment_client
    form = draft_form(client)
    if failure == "csrf":
        form["csrf_token"] = "wrong"
    elif failure == "consent":
        form.pop("public_lookup_consent")
    elif failure == "name":
        form["display_name"] = ""
    elif failure == "config":
        monkeypatch.setenv("PROFILE_ENRICHMENT_ENABLED", "false")
        client = make_authenticated_client(monkeypatch)
        form = draft_form(client)
    elif failure == "oversize":
        form["public_context"] = "x" * 70_000
    elif failure == "anonymous":
        client.cookies.clear()
    elif failure == "stale":
        save_test_profile(client)
    elif failure == "rate":
        limiter = client.app.state.services.rate_limiter
        limiter._buckets[f"user:{TEST_OWNER_ID}"].extend([time.monotonic()] * 6)
    response = client.post("/my/profile/enrich", data=form)
    assert response.status_code in {401, 403, 413, 422, 429, 503}
    assert not calls
    if response.status_code in {422, 429, 503}:
        assert "Unpublished editor title" in response.text
        assert 'id="enrichment-result"' in response.text
        assert re.search(
            r"data-enrichment-start[^>]*>\s*Find fantasy role and skills", response.text
        )
    if failure == "rate":
        assert response.headers["retry-after"] == "60"
    assert not client.app.state.services.profile_enrichment_repository._records


@pytest.mark.parametrize(
    "action", ["cancel", "logout", "stale", "expired", "duplicate", "account_switch"]
)
@pytest.mark.parametrize("stage", ["credential", "provider", "post_moderation"])
def test_first_use_concurrent_actions_suppress_dispatch_or_late_results(
    enrichment_client, action, stage
):
    client, calls = enrichment_client
    services = client.app.state.services
    form = draft_form(client)
    entered, release = threading.Event(), threading.Event()
    original = services.agent_client.enrich_profile
    original_moderation = services.moderation_service.moderate_text

    async def slow_moderation(text, *, stage):
        if stage == "post_text":
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        return await original_moderation(text, stage=stage)

    if stage == "post_moderation":
        services.moderation_service.moderate_text = slow_moderation

    async def slow(query, *, before_dispatch):
        if stage in {"provider", "post_moderation"}:
            await before_dispatch()
            calls.append(query)
        if stage == "post_moderation":
            return ProfileEnrichmentResponse.model_validate(SAFE_RESULT)
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        if stage == "credential":
            return await original(query, before_dispatch=before_dispatch)
        return ProfileEnrichmentResponse.model_validate(SAFE_RESULT)

    services.agent_client.enrich_profile = slow
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, "/my/profile/enrich", data=form)
        try:
            assert entered.wait(10)
            if action == "cancel":
                cancelled = client.post("/my/profile/enrich/cancel", data=form)
                assert cancelled.status_code == 200
                assert "Unpublished editor title" in cancelled.text
            elif action in {"logout", "account_switch"}:
                client.get("/auth/logout", follow_redirects=False)
                assert client.get("/my/profile", follow_redirects=False).status_code == 307
                if action == "account_switch":
                    # A second login replaces the cookie while the old request is still running.
                    user = {
                        "sub": "different-subject",
                        "name": "Other account",
                        "tenant_id": str(uuid4()),
                        "object_id": str(uuid4()),
                    }
                    user["owner_id"] = f"{user['tenant_id']}:{user['object_id']}"
                    cookie = (
                        TimestampSigner("test-session-secret")
                        .sign(base64.b64encode(json.dumps({"user": user}).encode()))
                        .decode()
                    )
                    client.cookies.set(
                        "fantasy_cards_session", cookie, domain="testserver.local", path="/"
                    )
            elif action == "stale":
                save_test_profile(client)
            elif action == "expired":
                for record in services.profile_enrichment_repository._records.values():
                    record.expires_at = time.time() - 1
            else:
                duplicate = client.post("/my/profile/enrich", data=form)
                assert duplicate.status_code == 422
                assert "already running" in duplicate.text
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert "set-cookie" not in response.headers
    if action == "logout":
        assert client.get("/my/profile", follow_redirects=False).status_code == 307
    elif action == "account_switch":
        switched = client.get("/my/profile")
        assert "Other account" in switched.text
    if action == "duplicate":
        assert "filled into the editor" in response.text
        assert 'value="Fantasy trading-card persona"' in response.text
        assert len(calls) == 1
    else:
        assert "filled into the editor" not in response.text
        assert "Unpublished editor title" in response.text
        assert 'value="A pseudonym"' in response.text
        assert len(calls) == (0 if stage == "credential" else 1)
    if action != "stale":
        assert asyncio.run(services.profile_repository.get(TEST_OWNER_ID)) is None


@pytest.mark.parametrize("cosmos", [False, True])
def test_transactions_are_single_use_owner_session_bound_and_cancelable(cosmos):
    from azure.core import MatchConditions
    from azure.cosmos.exceptions import (
        CosmosAccessConditionFailedError,
        CosmosResourceExistsError,
        CosmosResourceNotFoundError,
    )

    class Container:
        def __init__(self):
            self.document = None
            self.conflict = False

        async def create_item(self, document):
            if self.document is not None:
                raise CosmosResourceExistsError(status_code=409)
            self.document = {**document, "_etag": uuid4().hex}

        async def read_item(self, item, *, partition_key):
            if (
                self.document is None
                or self.document["id"] != item
                or self.document["userId"] != partition_key
            ):
                raise CosmosResourceNotFoundError(status_code=404)
            return dict(self.document)

        async def replace_item(self, item, document, *, etag, match_condition):
            assert match_condition == MatchConditions.IfNotModified
            if self.conflict or self.document["_etag"] != etag:
                raise CosmosAccessConditionFailedError(status_code=412)
            self.document = {**document, "_etag": uuid4().hex}

        async def delete_item(self, item, *, partition_key, etag=None, match_condition=None):
            await self.read_item(item, partition_key=partition_key)
            if etag is not None:
                assert match_condition == MatchConditions.IfNotModified
                assert etag == self.document["_etag"]
            self.document = None

        async def query_items(self, **kwargs):
            if self.document is not None:
                yield {
                    "id": self.document["id"],
                    "session_hash": self.document["transaction"]["session_hash"],
                }

    async def exercise():
        container = Container() if cosmos else None
        repository = EnrichmentRepository(container)
        state, record = new_transaction(
            owner_id=TEST_OWNER_ID,
            session_hash=digest("browser"),
            public_name="Synthetic Example",
            draft=EnrichmentDraft(display_name="Draft name"),
        )
        await repository.create(record)
        with pytest.raises(EnrichmentFailure, match="could not complete"):
            await repository.create(record)
        assert not await repository.admit(record)
        assert await repository.consume("other-owner", state, record.session_hash) is None
        assert await repository.consume(TEST_OWNER_ID, state, digest("other-session")) is None
        consumed = await asyncio.gather(
            repository.consume(TEST_OWNER_ID, state, record.session_hash),
            repository.consume(TEST_OWNER_ID, state, record.session_hash),
        )
        assert sum(item is not None for item in consumed) == 1
        if cosmos:
            container.conflict = True
            assert not await repository.admit(record)
            container.conflict = False
        assert await repository.admit(record)
        assert not await repository.admit(record)
        await repository.delete_owner(TEST_OWNER_ID, digest("other-session"))
        assert await repository.finish(record)
        assert not await repository.finish(record)
        await repository.create(record)
        await repository.delete_owner(TEST_OWNER_ID, record.session_hash)
        newer = record.model_copy(update={"transaction_id": uuid4().hex})
        await repository.create(newer)
        await repository.consume(TEST_OWNER_ID, state, record.session_hash)
        assert not await repository.admit(record)
        assert not await repository.finish(record)
        assert await repository.admit(newer)
        assert await repository.finish(newer)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "size",
        "schema",
        "redirect",
        "forbidden",
        "timeout",
        "cancel",
        "cancel_during_credential",
    ],
)
def test_foundry_transport_separates_credentials_and_bounds_response(failure, caplog):
    calls = []
    credential_acquired, admitted = False, False
    cancelled = failure == "cancel"
    request = ProfileEnrichmentRequest(
        operation="enrich_profile", publicName="Synthetic Example", transactionId=uuid4().hex
    )

    async def handler(http_request):
        assert admitted
        calls.append(http_request)
        assert http_request.headers["authorization"] == "Bearer workload-token"
        assert not any(key.startswith("x-client-") for key in http_request.headers)
        assert "x-ms-user-identity" not in http_request.headers
        assert json.loads(http_request.content)["input"][0]["content"][0]["text"] == (
            request.model_dump_json()
        )
        if failure == "timeout":
            raise httpx.ReadTimeout("private payload must not escape", request=http_request)
        status = 302 if failure == "redirect" else 403 if failure == "forbidden" else 200
        text = (
            json.dumps({**SAFE_RESULT, "private": "unexpected"})
            if failure == "schema"
            else ("x" * 65_537 if failure == "size" else json.dumps(SAFE_RESULT))
        )
        return httpx.Response(
            status,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            },
            headers={"Location": "https://not-called.example"},
        )

    def get_token(*scopes):
        nonlocal credential_acquired, cancelled
        credential_acquired = True
        if failure == "cancel_during_credential":
            cancelled = True
        return SimpleNamespace(token="workload-token")

    async def before_dispatch():
        nonlocal admitted
        assert credential_acquired
        if cancelled:
            raise EnrichmentFailure("cancelled")
        admitted = True

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = FoundryAgentClient(
                load_app_settings(),
                credential=SimpleNamespace(get_token=get_token),
                http_client=http,
            )
            if failure:
                with pytest.raises(EnrichmentFailure) as caught:
                    await client.enrich_profile(request, before_dispatch=before_dispatch)
                assert "private" not in str(caught.value)
            else:
                result = await client.enrich_profile(request, before_dispatch=before_dispatch)
                assert result.status == "completed"
        assert len(calls) == (0 if failure in {"cancel", "cancel_during_credential"} else 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["no_evidence", "ambiguous_identity", "refused", "timeout"])
def test_non_success_outcomes_preserve_editor_without_success_fallback(enrichment_client, outcome):
    client, calls = enrichment_client

    async def failure(*args, before_dispatch):
        await before_dispatch()
        calls.append("called")
        return ProfileEnrichmentResponse(status=outcome)

    client.app.state.services.agent_client.enrich_profile = failure
    response = client.post("/my/profile/enrich", data=draft_form(client))
    assert "filled into the editor" not in response.text
    assert "Unpublished editor title" in response.text
    assert "Private manual draft excerpt" not in response.text
    assert asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID)) is None
