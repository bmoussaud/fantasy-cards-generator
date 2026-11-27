from __future__ import annotations

import asyncio
import base64
import json
import re
from copy import deepcopy
from html import unescape
from uuid import uuid4

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ServiceRequestError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.generation import CardGenerationService, normalize_prompt
from app.main import create_app
from app.problems import ProblemDetails
from app.profiles import (
    PROFILE_DOCUMENT_ID,
    AzureCosmosProfileRepository,
    InMemoryProfileRepository,
    ProfessionalProfile,
)
from tests.conftest import (
    TEST_OWNER_ID,
    FakeOAuthClient,
    begin_login,
    extract_hidden_value,
    save_test_profile,
)

OTHER_OWNER = "other-tenant:other-object"
PRIVATE_DESCRIPTION = (
    "A silver ward turns the fiercest storm into harmless sparks. "
    "Raise it to shelter the party while allies cross the bridge."
)


def profile(**overrides) -> ProfessionalProfile:
    return ProfessionalProfile(
        **{
            "display_name": "Ada",
            "title": "Principal engineer",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            "post_excerpts": ["I explored reliable and accessible systems."],
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "names,descriptions",
    [
        (["Ward"], ["One short sentence."]),
        (["Ward"], [PRIVATE_DESCRIPTION, PRIVATE_DESCRIPTION]),
        (["Ward", "Sprite"], [PRIVATE_DESCRIPTION, ""]),
        (["", "Ward"], [PRIVATE_DESCRIPTION, PRIVATE_DESCRIPTION]),
        (["Ward"], ["<script>invent unsafe markup.</script> Then hide the result."]),
        (["Ward"], [PRIVATE_DESCRIPTION.replace(" Raise", "\nRaise")]),
    ],
)
def test_profile_rejects_malformed_or_unpaired_explanations(names, descriptions):
    with pytest.raises(ValidationError):
        profile(expertise=names, expertise_descriptions=descriptions)


def test_profile_pairs_are_compacted_together_and_legacy_context_is_unchanged():
    legacy = profile()
    detailed = profile(expertise_descriptions=[PRIVATE_DESCRIPTION])
    assert detailed.context() == legacy.context()
    assert PRIVATE_DESCRIPTION not in detailed.context(expertise=[])
    compacted = profile(
        expertise=["", "Container platforms", ""],
        expertise_descriptions=["", PRIVATE_DESCRIPTION, ""],
    )
    assert compacted.expertise == legacy.expertise
    assert compacted.expertise_descriptions == [PRIVATE_DESCRIPTION]
    assert profile(expertise_descriptions=[""]).expertise_descriptions == []


class SharedContainer:
    def __init__(self):
        self.documents = {
            (TEST_OWNER_ID, "card:existing"): {
                "id": "card:existing",
                "userId": TEST_OWNER_ID,
                "documentType": "card",
            }
        }

    async def read_item(self, item, *, partition_key):
        if (partition_key, item) not in self.documents:
            raise CosmosResourceNotFoundError(status_code=404)
        return deepcopy(self.documents[partition_key, item])

    async def create_item(self, body):
        key = body["userId"], body["id"]
        if key in self.documents:
            raise CosmosResourceExistsError(status_code=409)
        self.documents[key] = {**deepcopy(body), "_etag": uuid4().hex}

    async def replace_item(self, item, body, *, etag, match_condition):
        assert match_condition == MatchConditions.IfNotModified
        key = body["userId"], item
        if self.documents[key]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        self.documents[key] = {**deepcopy(body), "_etag": uuid4().hex}

    async def delete_item(self, item, *, partition_key):
        if self.documents.pop((partition_key, item), None) is None:
            raise CosmosResourceNotFoundError(status_code=404)


@pytest.mark.parametrize("storage", ["memory", "cosmos"])
def test_singleton_atomic_edits_isolation_and_reopened_repository(storage):
    container = SharedContainer()
    repository = (
        InMemoryProfileRepository()
        if storage == "memory"
        else AzureCosmosProfileRepository.__new__(AzureCosmosProfileRepository)
    )
    if storage == "cosmos":
        repository._container = container

    async def scenario():
        results = await asyncio.gather(
            repository.save(
                TEST_OWNER_ID, profile(expertise_descriptions=[PRIVATE_DESCRIPTION]), None
            ),
            repository.save(
                TEST_OWNER_ID,
                profile(display_name="Grace", expertise_descriptions=[PRIVATE_DESCRIPTION]),
                None,
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert any(isinstance(result, ProblemDetails) for result in results)
        assert await repository.get(OTHER_OWNER) is None
        await repository.save(OTHER_OWNER, profile(display_name="Other person"), None)
        saved = await repository.get(TEST_OWNER_ID)
        assert saved.profile.expertise_descriptions == [PRIVATE_DESCRIPTION]
        await repository.save(
            TEST_OWNER_ID,
            profile(display_name="Updated", expertise_descriptions=[PRIVATE_DESCRIPTION]),
            saved.revision,
        )
        with pytest.raises(ProblemDetails) as conflict:
            await repository.save(TEST_OWNER_ID, profile(display_name="Lost edit"), saved.revision)
        assert conflict.value.status_code == 409
        reopened = repository
        if storage == "cosmos":
            reopened = AzureCosmosProfileRepository.__new__(AzureCosmosProfileRepository)
            reopened._container = container
            assert container.documents[TEST_OWNER_ID, PROFILE_DOCUMENT_ID]["ttl"] == -1
            assert container.documents[TEST_OWNER_ID, "card:existing"]["documentType"] == "card"
        assert (await reopened.get(TEST_OWNER_ID)).profile.display_name == "Updated"
        assert (await reopened.get(TEST_OWNER_ID)).profile.expertise_descriptions == [
            PRIVATE_DESCRIPTION
        ]
        await reopened.delete_owner(TEST_OWNER_ID)
        assert await reopened.get(TEST_OWNER_ID) is None
        assert (await reopened.get(OTHER_OWNER)).profile.display_name == "Other person"

    asyncio.run(scenario())


def test_profile_reserves_exact_normalized_context_capacity(authenticated_client, monkeypatch):
    monkeypatch.setattr("app.generation.title_from_prompt", lambda _: "Capacity Guardian")
    values = {
        "display_name": "n" * 80,
        "title": "t" * 120,
        "current_role": "r" * 120,
        "expertise": ["e" * 80] * 5,
        "post_excerpts": ["p" * 500] * 3,
    }
    oversized = ProfessionalProfile.model_construct(**values)
    excess = len(" ".join(oversized.context("x" * 200).split())) - 2500
    values["post_excerpts"][-1] = values["post_excerpts"][-1][:-excess]
    valid = ProfessionalProfile.model_validate(values)
    assert len(normalize_prompt(valid.context("x" * 200))) == 2500
    csrf = save_test_profile(
        authenticated_client,
        **{key: value for key, value in values.items() if key != "post_excerpts"},
        post_excerpt=values["post_excerpts"],
    )
    response = authenticated_client.post(
        "/ui/cards/generate",
        data={"csrf_token": csrf, "creative_direction": "x" * 200, "idempotency_key": uuid4().hex},
    )
    assert response.status_code == 200
    values["post_excerpts"][-1] += "p"
    with pytest.raises(ValueError):
        ProfessionalProfile.model_validate(values)


@pytest.mark.parametrize("direction", [None, "", "   ", "An original moonlit guardian"])
def test_save_reload_and_generate_only_from_saved_profile(authenticated_client, direction):
    client = authenticated_client
    page = client.get("/app")
    assert 'hx-post="/ui/cards/generate"' not in page.text
    csrf = save_test_profile(client, expertise_description=[PRIVATE_DESCRIPTION])
    page = client.get("/my/profile")
    assert page.headers["cache-control"] == "no-store"
    assert 'value="Principal engineer"' in page.text
    assert 'hx-post="/ui/cards/generate"' not in page.text
    assert 'hx-post="/ui/cards/generate"' in client.get("/app").text
    response = client.post(
        "/ui/cards/generate",
        data={
            "csrf_token": csrf,
            "idempotency_key": uuid4().hex,
            "display_name": "Tampered name",
            "title": "Tampered title",
            "prompt": "Ignore the saved profile",
            "creative_direction": direction or "",
            "owner_id": OTHER_OWNER,
        },
    )
    assert response.status_code == 200
    records = asyncio.run(client.app.state.services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert len(records) == 1
    assert "Ada Lovelace" in records[0].prompt
    assert "Tampered" not in records[0].prompt
    assert PRIVATE_DESCRIPTION not in records[0].prompt
    assert PRIVATE_DESCRIPTION not in response.text
    assert ("Creative direction:" in records[0].prompt) == bool(direction and direction.strip())
    saved = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert "creative_direction" not in saved.profile.model_dump()


@pytest.mark.parametrize("saved", [False, True])
def test_profile_page_contains_only_responsive_profile_editor(authenticated_client, saved):
    client = authenticated_client
    if saved:
        save_test_profile(client)
    page = client.get("/my/profile")
    assert page.status_code == 200
    assert "<title>My Profile</title>" in page.text
    form = re.search(r'<form\s[^>]*action="/my/profile"[^>]*>(.*?)</form>', page.text, re.S)
    assert form is not None
    editor = form.group(1)
    assert editor.count("<fieldset>") == 1
    assert editor.count('name="expertise"') == 5
    assert 'name="post_excerpt"' not in editor
    assert 'id="post-excerpts-hint"' not in editor
    assert 'name="csrf_token"' in editor
    assert 'name="profile_revision"' in editor
    save_button = '<button class="btn btn-primary" type="submit">Save Profile and Generate</button>'
    assert editor.count(save_button) == 1
    assert 'name="return_to"' not in editor
    reading_order = (
        'id="display-name"',
        'formaction="/my/profile/enrich"',
        'formaction="/my/profile/enrich/cancel"',
        'id="public-lookup-consent"',
        "<summary>About public search</summary>",
        "<h3>Illustration</h3>",
        'id="profile-title"',
        'id="current-role"',
        'id="expertise-1"',
        'id="expertise-5"',
        save_button,
    )
    positions = [editor.index(marker) for marker in reading_order]
    assert positions == sorted(positions)
    assert 'id="enrichment-heading"' not in editor
    assert re.search(r"<button\b[^>]*\bhidden\b[^>]*data-enrichment-cancel", editor)
    assert editor.index("data-profile-default-save") < editor.index(
        'formaction="/my/profile/enrich"'
    )
    assert editor.count('class="profile-editor__actions"') == 1
    actions = re.search(r'<div class="profile-editor__actions">(.*?)</div>', editor, re.S)
    assert actions is not None
    hint = "Save only accurate information and excerpts you are authorized to use."
    assert hint not in page.text
    intro = (
        "Your profile is saved for this signed-in account. "
        "Editing it does not change existing cards."
    )
    assert page.text.count(intro) == 1
    assert actions.group(1).index(intro) < actions.group(1).index(save_button)
    assert 'class="field__hint"' not in actions.group(1)
    for hint_id in (
        "display-name-hint",
        "profile-title-hint",
        "current-role-hint",
        "expertise-hint",
    ):
        assert f'id="{hint_id}"' in editor
    assert '<h1 id="page-heading">Your professional profile</h1>' in page.text
    assert '<section class="panel" aria-labelledby="page-heading">' in page.text
    for removed in (
        'id="generator-form-heading"',
        'class="empty-state"',
        'id="generation-result"',
        'id="generate-card"',
        'id="saved-profile-generation-heading"',
        'class="saved-photo-picker"',
        'name="creative_direction"',
        'hx-post="/ui/cards/generate"',
        ">Generate card</button>",
    ):
        assert removed not in page.text
    workshop = client.get("/app")
    assert 'action="/my/profile"' not in workshop.text
    assert hint not in workshop.text
    assert intro not in workshop.text


@pytest.mark.parametrize("count", [1, 3, 5])
def test_generator_preselects_first_three_expertise_options(authenticated_client, count):
    client = authenticated_client
    expertise = [f"Expertise option {index}" for index in range(count)]
    save_test_profile(client, expertise=expertise)
    page = client.get("/app")
    assert page.status_code == 200
    options = re.findall(r'name="expertise_index" value="(\d+)"([^>]+)>', page.text)
    assert [(int(value), "checked" in attrs) for value, attrs in options] == [
        (index, index < 3) for index in range(count)
    ]
    assert all(f"<span>{item}</span>" in page.text for item in expertise)
    assert extract_hidden_value(page.text, "expertise_profile_revision")


@pytest.mark.parametrize("choices", [["2"], ["4", "1", "3"], ["0", "1", "2"], [], None])
def test_generator_filters_expertise_without_editing_saved_profile(authenticated_client, choices):
    client = authenticated_client
    expertise = ["Cloud migration", "Incident response", "FinOps", "Accessibility", "Observability"]
    csrf = save_test_profile(client, expertise=expertise)
    revision = extract_hidden_value(client.get("/app").text, "expertise_profile_revision")
    payload = {"csrf_token": csrf, "idempotency_key": uuid4().hex}
    if choices is not None:
        payload.update(expertise_profile_revision=revision, expertise_index=choices)
    response = client.post("/ui/cards/generate", data=payload)
    assert response.status_code == 200, response.text

    records = asyncio.run(client.app.state.services.card_repository.list_by_owner(TEST_OWNER_ID))
    (record,) = records
    expected = (
        expertise
        if choices is None
        else [item for index, item in enumerate(expertise) if str(index) in choices]
    )
    assert [item for item in expertise if item in record.prompt] == expected
    assert ("Explicit expertise" in record.prompt) == bool(expected)
    assert "Platform architect" in record.prompt
    abilities = expected if choices is not None else []
    assert [item["name"] for item in record.validated_payload["abilities"]] == abilities
    for html in (
        response.text,
        client.get(f"/my/cards/{record.id}").text,
        client.post("/ui/cards/generate", data=payload).text,
    ):
        assert re.findall(r"<li><strong>([^<]+)</strong>", html) == abilities
        assert ("absorbs the next spell" in html) == bool(abilities)
    saved = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert saved.revision == revision
    assert saved.profile.expertise == expertise


@pytest.mark.parametrize(
    ("choices", "stale", "status", "code"),
    [
        (["Free-form content"], False, 422, "invalid_expertise_selection"),
        (["5"], False, 422, "invalid_expertise_selection"),
        (["0", "1", "2", "3"], False, 422, "invalid_expertise_selection"),
        (["0"], True, 409, "expertise_selection_stale"),
    ],
)
def test_generator_rejects_invalid_or_stale_expertise_selection(
    authenticated_client, choices, stale, status, code
):
    client = authenticated_client
    csrf = save_test_profile(client)
    revision = extract_hidden_value(client.get("/app").text, "expertise_profile_revision")
    if stale:
        csrf = save_test_profile(client, expertise=["New expertise"])
    response = client.post(
        "/ui/cards/generate",
        data={
            "csrf_token": csrf,
            "idempotency_key": uuid4().hex,
            "expertise_profile_revision": revision,
            "expertise_index": choices,
        },
    )
    assert response.status_code == status
    assert response.headers["X-Generation-Error"] == code
    assert not asyncio.run(client.app.state.services.card_repository.list_by_owner(TEST_OWNER_ID))


def test_saving_profile_without_excerpt_controls_preserves_existing_excerpts(authenticated_client):
    client = authenticated_client
    excerpts = ["A recent post about maintaining reliable platforms."]
    save_test_profile(client, post_excerpt=excerpts)
    save_test_profile(client, title="Staff engineer")

    saved = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert saved is not None
    assert saved.profile.title == "Staff engineer"
    assert saved.profile.post_excerpts == excerpts


@pytest.mark.parametrize("excerpts", [[], ["A recent post about maintaining reliable platforms."]])
def test_generator_summarizes_saved_profile_without_editable_fields(authenticated_client, excerpts):
    client = authenticated_client
    save_test_profile(
        client,
        display_name="<Ada>&",
        expertise=["Container platforms", "Responsible AI"],
        post_excerpt=excerpts,
    )
    page = client.get("/app")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    summary = re.search(r'<section class="profile-summary"[^>]*>(.*?)</section>', page.text, re.S)
    assert summary is not None
    text = summary.group(1)
    assert "&lt;Ada&gt;&amp;" in text
    assert "<Ada>" not in text
    for value in ("Principal engineer", "Platform architect"):
        assert value in text
    assert "Container platforms" not in text
    assert 'href="/my/profile">Edit profile</a>' in text
    assert not re.search(r"<(?:input|textarea|form|button)\b", text)
    assert 'action="/my/profile"' not in page.text
    assert ">Save Profile and Generate</button>" not in page.text
    assert "Add one to five concrete expertise statements" not in page.text
    assert ('class="profile-summary__posts"' in text) == bool(excerpts)
    if excerpts:
        assert excerpts[0] in text
        assert 'class="profile-summary__posts" open' not in text
    assert 'name="creative_direction"' in page.text
    assert 'name="art_style"' in page.text
    assert '<option value="paper_arcana" selected>' in page.text
    assert '<option value="engraved_legends"' in page.text
    assert '<option value="paper_cut_myths"' in page.text
    assert "Paper Arcana" in page.text
    assert "Engraved Legends" in page.text
    assert "Paper-Cut Myths" in page.text
    assert 'name="saved_photo_id"' in page.text
    assert 'hx-post="/ui/cards/generate"' in page.text
    assert '<section id="generate-card" aria-label="Card generation">' in page.text
    assert "saved-profile-generation-heading" not in page.text
    assert "Generate from your saved profile" not in page.text
    form = re.search(r"<form data-card-generator-form[^>]*>(.*?)</form>", page.text, re.S)
    assert form is not None
    button = '<button class="btn btn-primary" type="submit">Generate card</button>'
    assert form.group(1).lstrip().startswith(button)
    assert form.group(1).count(button) == 1
    assert "<span>Container platforms</span>" in form.group(1)
    assert "<span>Responsible AI</span>" in form.group(1)
    assert form.group(1).index(button) < form.group(1).index('id="creative-direction"')
    assert form.group(1).index(button) < form.group(1).index('class="reference-photo-section"')
    assert 'hx-target="#generation-content"' in page.text
    assert "hx-disabled-elt=\"find button[type='submit']\"" in page.text
    assert 'id="generation-pending"' not in form.group(1)
    assert page.text.count('id="generation-pending"') == 1
    assert page.text.count('id="generation-content"') == 1
    loading = re.search(r'<div class="empty-state generation-loading">(.*?)</div>', page.text, re.S)
    assert loading is not None
    assert (
        'id="generation-pending" class="htmx-indicator loading-state" role="status"'
        in loading.group(1)
    )
    assert "htmx-request" not in loading.group(1)
    assert page.text.index('id="generation-result"') < page.text.index('id="generation-pending"')
    assert page.text.index('id="generation-pending"') < page.text.index('id="generation-content"')


def test_generator_without_profile_links_to_editor_and_does_not_offer_generation(
    authenticated_client,
):
    page = authenticated_client.get("/app")
    assert page.status_code == 200
    assert 'href="/my/profile">Create profile</a>' in page.text
    assert "Create and save your profile" in page.text
    assert 'action="/my/profile"' not in page.text
    assert 'hx-post="/ui/cards/generate"' not in page.text
    assert 'name="creative_direction"' not in page.text
    assert 'name="saved_photo_id"' not in page.text


@pytest.mark.parametrize("submitted", ["", "A quiet forest in ink.", "<moonlight> & silver"])
def test_additional_creative_direction_is_empty_and_restores_explicit_values(
    authenticated_client, submitted
):
    save_test_profile(authenticated_client)
    page = authenticated_client.get("/app")
    pattern = (
        r'<textarea(?P<attributes>[^>]*id="creative-direction"[^>]*)>(?P<value>.*?)</textarea>'
    )
    initial = re.search(pattern, page.text, re.S)
    assert initial is not None
    assert unescape(initial["value"]).strip() == ""
    for attribute in ("readonly", "disabled", "required"):
        assert attribute not in initial["attributes"]
    rendered = page.template.render({**page.context, "creative_direction": submitted})
    restored = re.search(pattern, rendered, re.S)
    assert restored is not None
    assert unescape(restored["value"]) == submitted
    assert "<moonlight>" not in restored["value"]


@pytest.mark.parametrize(
    ("return_to", "destination"),
    [
        ("/my/profile", "/my/profile?saved=true"),
        ("https://untrusted.example", "/app?saved=true#generate-card"),
        ("", "/app?saved=true#generate-card"),
    ],
)
def test_profile_save_returns_to_known_page_and_reloads(
    authenticated_client, return_to, destination
):
    client = authenticated_client
    page = client.get("/my/profile")
    response = client.post(
        "/my/profile",
        data={
            "display_name": "Ada Lovelace",
            "title": "Principal engineer",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            "csrf_token": extract_hidden_value(page.text, "csrf_token"),
            "profile_revision": extract_hidden_value(page.text, "profile_revision"),
            "creation_draft_id": extract_hidden_value(page.text, "creation_draft_id"),
            "return_to": return_to,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == destination
    confirmation = client.get(destination)
    assert "Principal engineer" in confirmation.text
    if return_to == "/my/profile":
        assert "Profile saved." in confirmation.text
    assert ('hx-post="/ui/cards/generate"' in confirmation.text) == (return_to != "/my/profile")


@pytest.mark.parametrize("existing", [False, True])
def test_profile_editor_save_opens_generator_without_automatic_generation(
    authenticated_client, monkeypatch, existing
):
    async def unexpected_generation(*args, **kwargs):
        raise AssertionError("Saving a profile must not start card generation.")

    monkeypatch.setattr(CardGenerationService, "generate_card", unexpected_generation)
    client = authenticated_client
    if existing:
        save_test_profile(client)
    page = client.get("/my/profile")
    response = client.post(
        "/my/profile",
        data={
            "display_name": "Updated Ada",
            "title": "Principal engineer",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            **{
                name: extract_hidden_value(page.text, name)
                for name in ("csrf_token", "profile_revision", "creation_draft_id")
            },
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app?saved=true#generate-card"
    generator = client.get(response.headers["location"])
    assert generator.status_code == 200
    assert "Updated Ada" in generator.text
    assert 'hx-post="/ui/cards/generate"' in generator.text
    assert not client.app.state.services.card_repository._records


def test_profile_validation_preserves_editor_without_generation(authenticated_client):
    client = authenticated_client
    csrf = save_test_profile(client)
    response = client.post(
        "/my/profile",
        data={
            "display_name": "Retained name",
            "title": "x",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 422
    assert "location" not in response.headers
    assert 'role="alert"' in response.text
    assert 'value="Retained name"' in response.text
    assert 'class="profile-editor"' in response.text
    assert 'hx-post="/ui/cards/generate"' not in response.text


@pytest.mark.parametrize("json_body", [False, True])
def test_generation_requires_saved_profile_on_direct_requests(authenticated_client, json_body):
    client = authenticated_client
    csrf = extract_hidden_value(client.get("/my/profile").text, "csrf_token")
    payload = {"prompt": "A safe original fantasy guardian", "csrfToken": csrf}
    response = client.post(
        "/ui/cards/generate",
        **({"json": payload} if json_body else {"data": payload}),
    )
    assert response.status_code == 409
    assert response.headers["x-generation-error"] == "profile_required"
    assert not client.app.state.services.card_repository._records


def test_profile_auth_csrf_refusal_and_stale_update_preserve_saved_profile(authenticated_client):
    client = authenticated_client
    csrf = save_test_profile(client)
    initial = client.get("/my/profile")
    revision = extract_hidden_value(initial.text, "profile_revision")
    denied = client.post("/my/profile", data={"csrf_token": ""})
    assert denied.status_code == 403
    blocked = client.post(
        "/my/profile",
        data={
            "display_name": "Ada",
            "title": "Principal engineer",
            "current_role": "create graphic gore",
            "expertise": ["Platforms"],
            "csrf_token": csrf,
            "profile_revision": revision,
        },
    )
    assert blocked.status_code == 422
    assert "location" not in blocked.headers
    assert 'value="create graphic gore"' in blocked.text
    assert 'class="profile-editor"' in blocked.text
    save_test_profile(client, title="Updated title")
    stale = client.post(
        "/my/profile",
        data={
            "display_name": "Lost edit",
            "title": "Old title",
            "current_role": "Architect",
            "expertise": ["Platforms"],
            "csrf_token": csrf,
            "profile_revision": revision,
        },
    )
    assert stale.status_code == 409
    assert "location" not in stale.headers
    assert 'role="alert"' in stale.text
    assert "Lost edit" in stale.text
    stored = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert stored.profile.title == "Updated title"
    anonymous = TestClient(create_app(), base_url="https://testserver")
    assert anonymous.post("/my/profile", data={}).status_code == 401


def test_profile_reloads_after_signin_and_is_deleted_with_account(authenticated_client):
    client = authenticated_client
    csrf = save_test_profile(client)
    services = client.app.state.services
    asyncio.run(services.profile_repository.save(OTHER_OWNER, profile(), None))
    client.cookies.clear()
    begin_login(client)
    client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    assert "Principal engineer" in client.get("/app").text
    csrf = extract_hidden_value(client.get("/my/profile").text, "csrf_token")
    response = client.post("/my/account/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert asyncio.run(services.profile_repository.get(TEST_OWNER_ID)) is None
    assert asyncio.run(services.profile_repository.get(OTHER_OWNER)) is not None


def test_profile_isolation_between_authenticated_owners(authenticated_client, monkeypatch):
    client = authenticated_client
    save_test_profile(client, display_name="Private name")
    services = client.app.state.services

    class OtherOAuth(FakeOAuthClient):
        async def authorize_access_token(self, request, **kwargs):
            token = await super().authorize_access_token(request, **kwargs)
            token["userinfo"]["tid"] = str(uuid4())
            token["userinfo"]["oid"] = str(uuid4())
            return token

    from app import main

    monkeypatch.setattr(main, "create_oauth_client", lambda settings: OtherOAuth())
    second = TestClient(create_app(services=services), base_url="https://testserver")
    begin_login(second)
    second.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    assert "Private name" not in second.get("/app").text
    save_test_profile(second, display_name="Other name")
    assert "Other name" not in client.get("/app").text


def test_profile_escaped_and_not_kept_in_session_cookie(authenticated_client):
    client = authenticated_client
    save_test_profile(client, display_name="<script>private-name</script>")
    page = client.get("/app")
    assert "<script>private-name</script>" not in page.text
    assert "&lt;script&gt;private-name&lt;/script&gt;" in page.text
    cookie = client.cookies.get("fantasy_cards_session").split(".")[0]
    session = json.loads(base64.b64decode(cookie))
    assert "private-name" not in json.dumps(session)
    assert "Principal engineer" not in json.dumps(session)


def test_invalid_direction_does_not_generate_or_update_profile(authenticated_client):
    client = authenticated_client
    csrf = save_test_profile(client)
    before = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    response = client.post(
        "/ui/cards/generate",
        data={"csrf_token": csrf, "creative_direction": "x" * 201, "idempotency_key": uuid4().hex},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert 'id="creative-direction"' not in response.text
    assert "Anime storybook style" not in response.text
    assert not client.app.state.services.card_repository._records
    after = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert after.revision == before.revision


def test_invalid_art_style_does_not_generate_or_update_profile(authenticated_client):
    client = authenticated_client
    csrf = save_test_profile(client)
    before = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    response = client.post(
        "/ui/cards/generate",
        data={
            "csrf_token": csrf,
            "art_style": "unknown_style",
            "idempotency_key": uuid4().hex,
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert not client.app.state.services.card_repository._records
    after = asyncio.run(client.app.state.services.profile_repository.get(TEST_OWNER_ID))
    assert after.revision == before.revision


def test_profile_storage_failure_is_explicit_and_preserves_input(
    authenticated_client, monkeypatch, caplog
):
    client = authenticated_client
    csrf = save_test_profile(client)
    repository = client.app.state.services.profile_repository
    saved = asyncio.run(repository.get(TEST_OWNER_ID))

    async def unavailable(*args):
        raise ServiceRequestError("sensitive exception payload must not be logged")

    monkeypatch.setattr(repository, "save", unavailable)
    response = client.post(
        "/my/profile",
        data={
            "display_name": "Unsaved private name",
            "title": "Principal engineer",
            "current_role": "Architect",
            "expertise": ["Platforms"],
            "profile_revision": saved.revision,
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 503
    assert "Unsaved private name" in response.text
    assert "could not be loaded or saved" in response.text
    assert "sensitive exception" not in caplog.text
    assert "Unsaved private name" not in caplog.text
    assert (asyncio.run(repository.get(TEST_OWNER_ID))).revision == saved.revision
