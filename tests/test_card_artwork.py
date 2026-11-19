from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw, ImageFont

from app import generation
from app.card_artwork import _wrap_text, compose_card_artwork, inscribe_card_abilities
from app.foundry_agent_client import FoundryAgentClient
from app.generation import StoredCard
from tests.conftest import TEST_OWNER_ID, extract_hidden_value, save_test_profile

ABILITY_NAME = "Énergie des forêts et résilience"
PRIVATE_NOTE = (
    "A private silver ward guards the moonlit archive. "
    "Invoke it to hide the party from a wandering guardian."
)


def artwork_bytes(format="PNG", size=(640, 480)):
    image = Image.new("RGB", size, "#39697a")
    output = BytesIO()
    image.save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize("count", [1, 5])
@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_inscription_draws_all_exact_text_with_accents_and_long_lines(count, format, monkeypatch):
    source = artwork_bytes(format)
    abilities = [
        (
            "É" * 80 if index else "Énergie des forêts\net résilience",
            (
                "Œ" * 180
                if index
                else "Déployez une barrière d’éclairs autour des alliés.\n"
                "Elle protège leur retraite au cœur de la forêt."
            ),
        )
        for index in range(count)
    ]
    drawn = []
    original = ImageDraw.ImageDraw.text

    def draw(drawer, position, text, **kwargs):
        drawn.append((position, text, kwargs["font"]))
        return original(drawer, position, text, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw)
    result = inscribe_card_abilities(source, abilities)
    with Image.open(BytesIO(source)) as before, Image.open(BytesIO(result)) as after:
        assert after.format == "PNG"
        assert after.width == before.width and after.height > before.height
        assert (
            ImageChops.difference(
                before.convert("RGB"), after.crop((0, 0, before.width, before.height))
            ).getbbox()
            is None
        )
        footer = after.crop((0, before.height, after.width, after.height))
        assert len(footer.getcolors(maxcolors=1_000_000)) > 2
        assert not after.info
        expected = "".join(name + description for name, description in abilities)
        assert "".join(text for _, text, _ in drawn).replace(" ", "") == (
            expected.replace(" ", "").replace("\n", "")
        )
        for (x, y), text, font in drawn:
            left, top, right, bottom = font.getbbox(text, anchor="lt")
            assert 0 <= x + left <= x + right <= after.width
            assert before.height <= y + top <= y + bottom <= after.height
            missing = font.getmask(chr(0x10FFFF))
            for character in set(text) - {" "}:
                glyph = font.getmask(character)
                assert (glyph.size, bytes(glyph)) != (missing.size, bytes(missing))


def test_wrapping_fits_words_and_preserves_explicit_line_breaks():
    font = ImageFont.load_default(size=30)
    text = "Une très longue capacité protège les alliés.\n\n" + "é" * 80
    lines = _wrap_text(text, font, 160)
    assert "" in lines
    assert "".join(lines).replace(" ", "") == text.replace(" ", "").replace("\n", "")
    assert all(font.getlength(line) <= 160 for line in lines)
    assert _wrap_text("Premier\r\nSecond\rTroisième", font, 300) == (
        ["Premier", "Second", "Troisième"]
    )
    assert inscribe_card_abilities(b"unchanged historical bytes", []) == (
        b"unchanged historical bytes"
    )


@pytest.mark.parametrize("content", [b"invalid-image", artwork_bytes(size=(4097, 1))])
def test_invalid_or_oversized_artwork_does_not_claim_success(content):
    with pytest.raises((ValueError, OSError)):
        inscribe_card_abilities(content, [(ABILITY_NAME, "A ward protects every ally nearby.")])


def test_excessive_explicit_lines_are_bounded_before_allocating_final_canvas():
    with pytest.raises(ValueError, match="inscribed_artwork_too_large"):
        inscribe_card_abilities(
            artwork_bytes(size=(4096, 1)),
            [("A\n" * 39 + "AB", "B\n" * 89 + "AB")] * 5,
        )


def test_extreme_aspect_ratio_is_bounded_before_upscaling(monkeypatch):
    source = artwork_bytes(size=(1, 4096))

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized artwork reached image allocation")

    monkeypatch.setattr(Image.Image, "resize", forbidden)
    with pytest.raises(ValueError, match="artwork_too_large"):
        inscribe_card_abilities(source, [(ABILITY_NAME, "A guardian shields the entire party.")])


def test_missing_glyph_is_rejected_rather_than_silently_drawing_a_replacement_box():
    with pytest.raises(ValueError, match="unsupported_card_character"):
        inscribe_card_abilities(
            artwork_bytes(), [("Dragon " + chr(0x10FFFF), "A guardian shields the entire party.")]
        )


def frame_artwork(
    content,
    abilities,
    *,
    name="Gardienne des étoiles",
    flavor_text="La lumière protège les voyageurs.",
):
    return compose_card_artwork(
        content,
        abilities,
        name=name,
        card_type="hero",
        rarity="legendary",
        mana_cost=4,
        attack=5,
        health=8,
        flavor_text=flavor_text,
    )


@pytest.mark.parametrize("count", [1, 5])
def test_complete_card_places_all_selected_abilities_inside_one_frame(count, monkeypatch):
    abilities = [
        (
            "É" * 80 if index else ABILITY_NAME,
            (
                "Œ" * 180
                if index
                else "Une lumière protège les alliés.\nElle accompagne leur retour à la forêt."
            ),
        )
        for index in range(count)
    ]
    drawn, panels, borders = [], [], []
    original_text = ImageDraw.ImageDraw.text
    original_rectangle = ImageDraw.ImageDraw.rectangle
    original_border = ImageDraw.ImageDraw.rounded_rectangle

    def text(drawer, position, value, **kwargs):
        drawn.append((position, value, kwargs["font"]))
        return original_text(drawer, position, value, **kwargs)

    def rectangle(drawer, box, **kwargs):
        if kwargs.get("fill") == "#252132":
            panels.append(box)
        return original_rectangle(drawer, box, **kwargs)

    def border(drawer, box, **kwargs):
        borders.append(box)
        return original_border(drawer, box, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", rectangle)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", border)
    card_name = "É" * 80 if count == 5 else "Gardienne des étoiles"
    flavor = "Œ" * 280 if count == 5 else "La lumière protège les voyageurs."
    output = frame_artwork(artwork_bytes(), abilities, name=card_name, flavor_text=flavor)
    expected = (
        card_name
        + "Mana 4Hero / Legendary"
        + "".join(n + description for n, description in abilities)
        + flavor
        + "Attack 5   /   Health 8"
    )

    def normalize(value):
        return "".join(value.split())

    assert normalize("".join(value for _, value, _ in drawn)) == normalize(expected)
    assert len(panels) == len(borders) == 1
    left, top, right, bottom = panels[0]
    border_left, border_top, border_right, border_bottom = borders[0]
    assert border_left < left < right < border_right
    assert border_top < top < bottom < border_bottom
    ability_text = [
        (position, value, font) for position, value, font in drawn if top <= position[1] <= bottom
    ]
    assert normalize("".join(value for _, value, _ in ability_text)) == normalize(
        "".join(n + description for n, description in abilities)
    )
    with Image.open(BytesIO(output)) as image:
        assert image.format == "PNG" and not image.info
        assert image.height > image.width
        for (x, y), value, font in drawn:
            a, b, c, d = font.getbbox(value, anchor="lt")
            assert 0 < x + a <= x + c < image.width
            assert 0 < y + b <= y + d < image.height
        assert 0 < border_bottom < image.height
    assert frame_artwork(b"unchanged zero selection", []) == b"unchanged zero selection"


def test_complete_card_rejects_line_expansion_before_canvas_allocation(monkeypatch):
    source = artwork_bytes(size=(4096, 1))

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized full-card layout reached allocation")

    monkeypatch.setattr(Image, "new", forbidden)
    with pytest.raises(ValueError, match="inscribed_artwork_too_large"):
        frame_artwork(source, [("A\n" * 39 + "AB", "B\n" * 89 + "AB")] * 5)


def test_illustration_prompt_stays_below_limit_even_with_maximum_json_escaping():
    from tests.test_foundry_agent_client import valid_card

    card = generation.GeneratedCardModel.model_validate(valid_card() | {"artBrief": "\x00" * 300})
    prompt = generation.derive_illustration_prompt(card)
    assert len(prompt) < 4000
    assert "\\u0000" * 300 in prompt


@pytest.mark.parametrize("mode", ["generate", "edit", "retry"])
@pytest.mark.parametrize("selected", [False, True])
def test_hosted_v19_effective_provider_prompt_is_artwork_only_for_selected_cards(
    authenticated_client, monkeypatch, mode, selected
):
    from tests.test_foundry_agent_client import (
        FakeCredential,
        completed_agent_payload,
        responses_envelope,
    )

    client = authenticated_client
    services = client.app.state.services
    agent_calls, image_calls = [], []
    source = artwork_bytes()
    photo = mode == "edit"
    allow_image = mode != "retry"
    agent_payload = completed_agent_payload(
        artPrompt="Draw a complete card with invented powers and readable rules.",
        metadata={"agentVersion": "profile-explanations-test"},
    )
    agent_payload["card"]["abilities"] = (
        [{"name": ABILITY_NAME, "description": "Conjure a ward that protects every ally nearby."}]
        if selected
        else []
    )
    visual_brief = agent_payload["card"]["artBrief"]

    def agent_handler(request):
        agent_calls.append(json.loads(request.content))
        return httpx.Response(200, json=responses_envelope(agent_payload))

    services.agent_client = FoundryAgentClient(
        replace(
            services.settings,
            foundry_agent_version="19",
            foundry_agent_expected_version="profile-explanations-test",
        ),
        credential=FakeCredential(),
        transport=httpx.MockTransport(agent_handler),
    )
    image_client = generation.AzureFoundryAIClient(services.settings)

    async def access_token():
        return "test-image-token"

    monkeypatch.setattr(image_client, "_access_token", access_token)
    services.ai_client = image_client

    def image_handler(request):
        if photo:
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            parts = {
                part.get_param("name", header="content-disposition"): part
                for part in message.iter_parts()
            }
            prompt = parts["prompt"].get_payload(decode=True).decode()
            assert parts["image"].get_payload(decode=True) == source
            assert request.url.path.endswith("/images/edits")
        else:
            prompt = json.loads(request.content)["prompt"]
            assert request.url.path.endswith("/images/generations")
        image_calls.append(prompt)
        if not allow_image:
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(source).decode()}]})

    original_client = httpx.AsyncClient

    def transport_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(image_handler))
        return original_client(**kwargs)

    monkeypatch.setattr(generation.httpx, "AsyncClient", transport_client)
    response, form = generate_selected(client, selected=selected, photo=photo)
    assert response.status_code == 200, response.text
    assert len(agent_calls) == 1
    if mode == "retry":
        (pending,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
        assert pending.status == "awaiting_artwork_retry"
        assert bool(pending.artwork_layout) == selected
        allow_image = True
        response = client.post(
            f"/api/v1/cards/{pending.id}/artwork/retry",
            json={"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex},
        )
        assert response.status_code == 200 and response.json()["status"] == "completed"
        assert len(image_calls) >= 2 and len(agent_calls) == 1
    else:
        assert len(image_calls) == 1
    assert len(set(image_calls)) == 1
    effective = image_calls[0]
    assert len(effective) < 4000 and PRIVATE_NOTE not in effective
    (record,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert record.derived_art_prompt == effective
    assert record.card_frame_composed == selected
    assert ("artworkLayout" in record.to_document()) == selected
    if selected:
        assert visual_brief in effective
        assert agent_payload["artPrompt"] not in effective
        assert (
            "No text" in effective
            and "No card, border, frame, panels, UI, stats, rules" in effective
        )
        assert ABILITY_NAME not in effective
    else:
        assert effective == agent_payload["artPrompt"]
        assert client.get(record.image_url_path).content == source
    asyncio.run(services.agent_client.aclose())


def test_legacy_pending_retry_keeps_old_prompt_and_original_footer(
    authenticated_client, monkeypatch
):
    client = authenticated_client
    services = client.app.state.services
    _, form = generate_selected(client, direction="[[mock:image-500]]")
    (pending,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    legacy_document = pending.to_document()
    legacy_document.pop("artworkLayout")
    legacy_document["derivedArtPrompt"] = "Original safe trading card scene with a complete frame."
    legacy = StoredCard.from_document(legacy_document)
    asyncio.run(services.card_repository.save(legacy))
    original_image = services.ai_client.generate_image
    observed = []

    async def image(prompt, **kwargs):
        observed.append(prompt)
        return await original_image(prompt, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy pending artwork entered the new full-card composer")

    monkeypatch.setattr(services.ai_client, "generate_image", image)
    monkeypatch.setattr(generation, "compose_card_artwork", forbidden)
    response = client.post(
        f"/api/v1/cards/{legacy.id}/artwork/retry",
        json={"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex},
    )
    assert response.status_code == 200 and response.json()["status"] == "completed"
    assert observed == [legacy.derived_art_prompt]
    saved = asyncio.run(services.card_repository.get(TEST_OWNER_ID, legacy.id))
    assert saved.abilities_embedded and not saved.card_frame_composed
    assert "artworkLayout" not in saved.to_document()
    html = client.get(f"/my/cards/{saved.id}").text
    assert "card-face--composed" not in html and "card-face__rules--embedded" in html


def generate_selected(client, *, selected=True, direction="", photo=False):
    csrf = save_test_profile(client, expertise=[ABILITY_NAME], expertise_description=[PRIVATE_NOTE])
    page = client.get("/app")
    payload = {
        "csrf_token": csrf,
        "expertise_profile_revision": extract_hidden_value(page.text, "expertise_profile_revision"),
        "idempotency_key": uuid4().hex,
        "creative_direction": direction,
        "expertise_index": ["0"] if selected else [],
    }
    kwargs = {"files": {"photo": ("reference.png", artwork_bytes(), "image/png")}} if photo else {}
    return client.post("/ui/cards/generate", data=payload, **kwargs), payload


@pytest.mark.parametrize("selected", [True, False])
def test_real_private_image_bytes_metadata_and_accessible_rendering(
    authenticated_client, monkeypatch, selected
):
    client = authenticated_client
    services = client.app.state.services
    original_image = services.ai_client.generate_image
    original_moderation = services.moderation_service.moderate_image
    original_compose = generation.compose_card_artwork
    source = artwork_bytes()
    moderated = False
    pairs = []

    async def image(*args, **kwargs):
        result = await original_image(*args, **kwargs)
        return replace(result, image=replace(result.image, content=source))

    async def moderate(image):
        nonlocal moderated
        decision = await original_moderation(image)
        moderated = decision.allowed
        return decision

    def compose(content, abilities, **metadata):
        assert moderated
        assert content == source
        pairs.extend(abilities)
        return original_compose(content, abilities, **metadata)

    monkeypatch.setattr(services.ai_client, "generate_image", image)
    monkeypatch.setattr(services.moderation_service, "moderate_image", moderate)
    monkeypatch.setattr(generation, "compose_card_artwork", compose)
    response, payload = generate_selected(client, selected=selected)
    assert response.status_code == 200, response.text
    (record,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    served = client.get(record.image_url_path)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert record.blob_sha256 == hashlib.sha256(served.content).hexdigest()
    assert record.blob_size_bytes == len(served.content)
    assert record.blob_content_type == "image/png"
    assert record.abilities_embedded == selected
    assert record.card_frame_composed == selected
    assert StoredCard.from_document(record.to_document()).abilities_embedded == selected
    assert pairs == [
        (ability["name"], ability["description"])
        for ability in record.validated_payload["abilities"]
    ]
    assert PRIVATE_NOTE not in repr(pairs)
    if selected:
        assert served.content != source
        with Image.open(BytesIO(served.content)) as final:
            assert final.height > 480
    else:
        assert served.content == source
    for html in (
        response.text,
        client.get(f"/my/cards/{record.id}").text,
        client.post("/ui/cards/generate", data=payload).text,
    ):
        assert ('class="card-face__rules card-face__rules--embedded"' in html) == selected
        assert ('class="card-face card-face--composed"' in html) == selected
        assert ("absorbs the next spell" in html) == selected
        assert PRIVATE_NOTE not in html
    assert ("card-summary__media--inscribed" in client.get("/my/cards").text) == selected
    assert client.get(record.image_url_path).content == served.content
    anonymous = TestClient(client.app, base_url="https://testserver")
    assert anonymous.get(record.image_url_path).status_code == 401
    foreign = replace(record, id="foreign-inscribed", owner_id="another-owner")
    asyncio.run(services.card_repository.save(foreign))
    assert client.get("/cards/foreign-inscribed/image").status_code == 404


def test_artwork_retry_inscribes_once_and_legacy_image_stays_unchanged(
    authenticated_client, monkeypatch
):
    client = authenticated_client
    services = client.app.state.services
    original = generation.compose_card_artwork
    calls = []

    def compose(content, abilities, **metadata):
        calls.append(abilities)
        return original(content, abilities, **metadata)

    monkeypatch.setattr(generation, "compose_card_artwork", compose)
    response, form = generate_selected(client, direction="[[mock:image-500]]")
    assert response.status_code == 200
    assert "Artwork pending" in response.text
    assert "card-face__rules--embedded" not in response.text
    (partial,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert not calls and not partial.abilities_embedded
    assert partial.artwork_layout == "illustration_only_v1"
    text_attempts = {
        key: value for key, value in services.ai_client._attempts.items() if key.startswith("text:")
    }
    partial.derived_art_prompt = partial.derived_art_prompt.replace("[[mock:image-500]]", "")
    asyncio.run(services.card_repository.save(partial))
    path = f"/api/v1/cards/{partial.id}/artwork/retry"
    retry_body = {"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex}
    retry = client.post(path, json=retry_body)
    assert retry.status_code == 200 and retry.json()["status"] == "completed"
    assert "abilitiesEmbedded" not in retry.json() and "abilities" not in retry.json()
    assert "cardFrameComposed" not in retry.json() and "artworkLayout" not in retry.json()
    assert len(calls) == 1
    assert client.post(path, json=retry_body).status_code == 404
    assert len(calls) == 1
    assert text_attempts == {
        key: value for key, value in services.ai_client._attempts.items() if key.startswith("text:")
    }
    completed = asyncio.run(services.card_repository.get(TEST_OWNER_ID, partial.id))
    assert completed.abilities_embedded
    assert completed.card_frame_composed
    with Image.open(BytesIO(client.get(completed.image_url_path).content)) as image:
        assert image.height > image.width
    source = artwork_bytes()
    legacy = replace(
        completed,
        id="legacy-named-abilities",
        abilities_embedded=False,
        artwork_layout=None,
        blob_name="legacy.png",
        image_url_path="/cards/legacy-named-abilities/image",
    )
    asyncio.run(services.asset_store.upload("legacy.png", source, "image/png"))
    asyncio.run(services.card_repository.save(legacy))
    assert client.get(legacy.image_url_path).content == source
    html = client.get("/my/cards/legacy-named-abilities").text
    assert "absorbs the next spell" in html
    assert "card-face__rules--embedded" not in html
    assert "card-face--composed" not in html
    assert "abilitiesEmbedded" not in legacy.to_document()["blob"]
    assert not StoredCard.from_document(legacy.to_document()).abilities_embedded


@pytest.mark.parametrize("retry", [False, True])
def test_composition_failure_is_explicit_and_never_marks_an_image_embedded(
    authenticated_client, monkeypatch, caplog, retry
):
    client = authenticated_client
    services = client.app.state.services

    def fail(*args, **kwargs):
        raise ValueError("private-failure-payload-canary")

    if retry:
        initial, form = generate_selected(client, direction="[[mock:image-500]]")
        (partial,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
        partial.derived_art_prompt = partial.derived_art_prompt.replace("[[mock:image-500]]", "")
        asyncio.run(services.card_repository.save(partial))
        monkeypatch.setattr(generation, "compose_card_artwork", fail)
        response = client.post(
            f"/api/v1/cards/{partial.id}/artwork/retry",
            json={"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex},
        )
    else:
        monkeypatch.setattr(generation, "compose_card_artwork", fail)
        response, _ = generate_selected(client)
    assert response.status_code == 503
    assert "persistence_failure" in response.text
    assert "private-failure-payload-canary" not in caplog.text + response.text
    records = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert len(records) == int(retry)
    assert all(record.status == "awaiting_artwork_retry" for record in records)
    assert all(not record.abilities_embedded and record.blob_name is None for record in records)


@pytest.mark.parametrize("stage", ["post-text-block", "post-image-block"])
def test_refused_generation_never_enters_inscription(authenticated_client, monkeypatch, stage):
    def forbidden(*args, **kwargs):
        pytest.fail("Refused content reached artwork composition")

    monkeypatch.setattr(generation, "compose_card_artwork", forbidden)
    response, _ = generate_selected(authenticated_client, direction=f"[[mock:{stage}]]")
    assert response.status_code == 422
    assert not asyncio.run(
        authenticated_client.app.state.services.card_repository.list_by_owner(TEST_OWNER_ID)
    )
