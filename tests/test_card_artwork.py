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
from app.card_styles import IDENTITIES, CardRecipe, CardStyle, load_recipe, normalize_accent
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
        layout="illustration_only_v1",
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
    # Ordinary full-length briefs must still fit the bounded image trace projection.
    plain = card.model_copy(update={"artBrief": "x" * 300})
    assert len(generation.derive_illustration_prompt(plain)) <= 1000


def test_classic_card_prints_and_displays_verbatim_french_expertise_on_retry(
    authenticated_client, monkeypatch
):
    from tests.test_foundry_agent_client import (
        FakeCredential,
        completed_agent_payload,
        responses_envelope,
    )

    client = authenticated_client
    services = client.app.state.services
    payload = completed_agent_payload(metadata={"agentVersion": "classic-test"})
    payload["card"]["abilities"] = [
        {"name": ABILITY_NAME, "description": "Shelter one ally behind a living ward."}
    ]
    payload["metadata"]["inscription"] = {"subtype": "Human Warden", "abilityNames": ["Wild Ward"]}
    payload["metadata"]["cardStyle"] = {"accentColor": "#FA89B1"}
    services.agent_client = FoundryAgentClient(
        replace(services.settings, foundry_agent_expected_version="classic-test"),
        credential=FakeCredential(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses_envelope(payload))
        ),
    )
    original_image = services.ai_client.generate_image
    allow = False

    async def image(*args, **kwargs):
        if not allow:
            raise generation.UpstreamServiceError("foundry-image", "unavailable", status_code=503)
        return await original_image(*args, **kwargs)

    drawn = []
    original_text = ImageDraw.ImageDraw.text

    def text(draw, position, string, **kwargs):
        drawn.append(string)
        return original_text(draw, position, string, **kwargs)

    monkeypatch.setattr(services.ai_client, "generate_image", image)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    result, form = generate_selected(client)
    assert result.status_code == 200, result.text
    (pending,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert pending.status == "awaiting_artwork_retry"
    assert pending.validated_payload["abilities"][0]["name"] == ABILITY_NAME
    assert "inscription" not in pending.validated_payload
    allow = True
    result = client.post(
        f"/api/v1/cards/{pending.id}/artwork/retry",
        json={"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex},
    )
    assert result.status_code == 200, result.text
    (record,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert record.status == "completed" and record.card_style.accent == IDENTITIES["rose"]
    assert ABILITY_NAME in " ".join(drawn) and "Wild Ward" not in " ".join(drawn)
    assert record.card_payload().printed_type() in drawn
    assert "Illus. Azure AI Foundry" in drawn
    for page in (
        client.post("/ui/cards/generate", data=form),
        client.get(f"/my/cards/{record.id}"),
    ):
        assert page.status_code == 200
        assert ABILITY_NAME in page.text and "Wild Ward" not in page.text
    assert (
        asyncio.run(
            generation.recompose_stored_card(
                StoredCard.from_document(record.to_document()), services.asset_store
            )
        )
        == client.get(record.image_url_path).content
    )
    asyncio.run(services.agent_client.aclose())


def test_classic_illustration_stays_inside_its_window_and_identities_are_distinct():
    recipe = load_recipe()
    assert recipe.portrait_overflow is None and recipe.geometry.overflow_band is None
    colors = [normalize_accent(raw, "hero") for raw in ("#FFFFEE", "#FF68AD", "#1874CF")]
    assert colors == [IDENTITIES[key] for key in ("ivory", "rose", "blue")]

    def render(content, accent):
        return Image.open(
            BytesIO(
                compose_card_artwork(
                    content,
                    [],
                    name="Coast Warden",
                    card_type="hero",
                    rarity="rare",
                    mana_cost=4,
                    attack=3,
                    health=5,
                    flavor_text="Every dawn brings a new tide.",
                    card_style=CardStyle(accent=accent),
                )
            )
        ).convert("RGB")

    alternate = BytesIO()
    Image.new("RGB", (640, 480), "#C16A54").save(alternate, "PNG")
    first, second = render(artwork_bytes(), colors[0]), render(alternate.getvalue(), colors[0])
    art = recipe.geometry.art_window.pixels(*first.size)
    diff = ImageChops.difference(first, second).getbbox()
    assert diff and art[0] <= diff[0] < diff[2] <= art[2] and art[1] <= diff[1] < diff[3] <= art[3]
    # Same rarity and black edge; the exposed frame itself must change with the identity.
    border = (38, 400, 48, 800)
    assert len({render(artwork_bytes(), color).crop(border).tobytes() for color in colors}) == 3


def test_classic_panel_fits_three_max_length_expertise_names_and_effects(monkeypatch):
    recipe = load_recipe()
    width, height = recipe.canvas.reference_px
    panel = recipe.geometry.ability_panel.pixels(width, height)
    drawn = []
    original = ImageDraw.ImageDraw.text

    def text(draw, xy, value, **kwargs):
        if kwargs.get("fill") == recipe.palette.fixed.ink and panel[1] < xy[1] < panel[3]:
            left, top, right, bottom = draw.textbbox(
                xy, value, font=kwargs["font"], anchor=kwargs["anchor"]
            )
            assert panel[0] < left <= right < panel[2] and panel[1] < top <= bottom < panel[3]
            drawn.append((value, top, bottom))
        return original(draw, xy, value, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    effect = ("Guard each neighboring ally until the last lantern on the sea wall fades. " * 2)[
        :120
    ].strip()
    abilities = [(letter * 80, effect) for letter in ("W", "Œ", "É")]
    compose_card_artwork(
        artwork_bytes(),
        abilities,
        name="Keeper of the Last Lantern on the Western Sea Wall",
        card_type="hero",
        rarity="legendary",
        mana_cost=12,
        attack=10,
        health=20,
        flavor_text="Even the longest night has an edge.",
    )

    def squash(value):
        return "".join(value.split())

    assert squash(" ".join(v for v, _, _ in drawn)) == squash(" ".join(n + d for n, d in abilities))
    assert all(a[2] <= b[1] for a, b in zip(drawn, drawn[1:]))


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
    agent_calls, image_calls, extraction_calls = [], [], []
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
        if request.url.path.endswith("/images/edits"):
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
            if "background" in parts:
                assert parts["background"].get_payload(decode=True) == b"transparent"
                assert parts["output_format"].get_payload(decode=True) == b"png"
                extraction_calls.append(prompt)
                return httpx.Response(
                    200, json={"data": [{"b64_json": base64.b64encode(source).decode()}]}
                )
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
        assert pending.artwork_layout == "cadre_classique_v1"
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
    assert record.card_frame_composed
    assert record.to_document()["artworkLayout"] == "cadre_classique_v1"
    assert visual_brief in effective
    assert agent_payload["artPrompt"] not in effective
    assert (
        "No text" in effective and "No card, border, frame, panels, UI, stats, rules" in effective
    )
    assert ABILITY_NAME not in effective
    assert not extraction_calls and set(record.artwork_assets) == {"scene"}
    assert "1.19:1" in effective and record.card_style.accent in effective
    restored = StoredCard.from_document(record.to_document())
    image = client.get(record.image_url_path).content
    assert image != source
    assert asyncio.run(generation.recompose_stored_card(restored, services.asset_store)) == image
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
    assert record.card_frame_composed
    assert StoredCard.from_document(record.to_document()).abilities_embedded == selected
    assert pairs == [
        (ability["name"], ability["description"])
        for ability in record.validated_payload["abilities"]
    ]
    assert PRIVATE_NOTE not in repr(pairs)
    assert served.content != source
    with Image.open(BytesIO(served.content)) as final:
        assert final.size == (1024, 1536)
    for html in (
        response.text,
        client.get(f"/my/cards/{record.id}").text,
        client.post("/ui/cards/generate", data=payload).text,
    ):
        assert ('class="card-face__rules card-face__rules--embedded"' in html) == selected
        assert 'class="card-face card-face--composed"' in html
        assert ("absorbs the next spell" in html) == selected
        assert PRIVATE_NOTE not in html
    assert "card-summary__media--inscribed" in client.get("/my/cards").text
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
    assert partial.artwork_layout == "cadre_classique_v1"
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


def subject_bytes(size=(640, 480), fraction=0.5):
    image = Image.new("RGBA", size)
    if fraction < 1:
        ImageDraw.Draw(image).rectangle(
            (0, 0, round(size[0] * (1 - fraction)) - 1, size[1]), fill="#FF20AA"
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def recipe_card(*, cutout=None, rarity="rare", accent="#2F5DA8", abilities=None, flavor=""):
    return compose_card_artwork(
        artwork_bytes(),
        abilities or [("Marée guérisseuse", "Restaure deux points de vie à un allié.")],
        name="Mage des marées",
        card_type="hero",
        rarity=rarity,
        mana_cost=4,
        attack=3,
        health=5,
        flavor_text=flavor,
        layout="portrait_debordant_v1",
        card_style=CardStyle(recipe="portrait-debordant", accent=accent),
        subject_cutout=cutout,
    )


def test_recipe_rejects_geometry_that_touches_identity_or_reorders_layers():
    recipe = load_recipe("portrait-debordant")
    assert (recipe.id, recipe.version) == ("portrait-debordant", 1)
    for changed in (
        {
            "geometry": recipe.geometry.model_dump()
            | {"overflow_band": {"x": 6, "y": 12.0, "w": 88, "h": 5.5}}
        },
        {
            "geometry": recipe.geometry.model_dump()
            | {"cost_token": {"anchor": "title_plaque.right", "diameter_pct_w": 30}}
        },
        {"layers": list(reversed(recipe.layers))},
    ):
        with pytest.raises(ValueError):
            CardRecipe.model_validate(recipe.model_dump() | changed)
    with pytest.raises(ValueError):
        load_recipe("../outside")


@pytest.mark.parametrize("card_type", ["hero", "creature", "artifact", "spell"])
def test_accent_clamp_and_type_fallback(card_type):
    import colorsys

    recipe = load_recipe("portrait-debordant")
    fallback = recipe.palette.accent.fallback_by_card_type[card_type]
    assert normalize_accent(None, card_type, recipe) == fallback
    assert normalize_accent("invalid", card_type, recipe) == fallback
    for raw in ("#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0202FE"):
        accent = normalize_accent(raw, card_type, recipe)
        rgb = [int(accent[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        _, lightness, saturation = colorsys.rgb_to_hls(*rgb)
        assert 0.345 <= saturation <= 0.755
        assert 0.295 <= lightness <= 0.555


@pytest.mark.parametrize("rarity", ["common", "uncommon", "rare", "legendary"])
def test_overflow_changes_only_pixels_inside_the_band(rarity):
    before = Image.open(BytesIO(recipe_card(rarity=rarity))).convert("RGB")
    after = Image.open(BytesIO(recipe_card(cutout=subject_bytes(), rarity=rarity))).convert("RGB")
    difference = ImageChops.difference(before, after)
    geometry = load_recipe("portrait-debordant").geometry
    band = geometry.overflow_band.pixels(*before.size)
    art = geometry.art_window.pixels(*before.size)
    assert difference.crop(art).getbbox() is not None
    ImageDraw.Draw(difference).rectangle((art[0], art[1], art[2] - 1, art[3] - 1), fill="black")
    bbox = difference.getbbox()
    assert bbox is not None
    assert band[0] <= bbox[0] < bbox[2] <= band[2]
    assert band[1] <= bbox[1] < bbox[3] <= band[3]
    title = geometry.title_plaque.pixels(*before.size)
    assert difference.crop(title).getbbox() is None


@pytest.mark.parametrize("fraction", [0, 0.1, 0.95, 1])
def test_cutout_rejects_invalid_transparency(fraction):
    from app.card_artwork import validate_subject_cutout

    with pytest.raises(ValueError, match="invalid_subject_transparency"):
        validate_subject_cutout(subject_bytes(fraction=fraction), artwork_bytes())


def test_recipe_typography_is_ink_not_accent_and_fits_three_maximum_descriptions(monkeypatch):
    calls = []
    original = ImageDraw.ImageDraw.text

    def draw(drawer, position, text, **kwargs):
        calls.append((position, text, kwargs))
        return original(drawer, position, text, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw)
    description = ("La marée protège chaque allié et repousse les créatures hostiles. " * 2)[:120]
    result = recipe_card(
        accent="#6B3FA0",
        abilities=[(f"Pouvoir {index}", description) for index in range(3)],
        flavor="La mer écoute ceux qui savent lui parler.",
    )
    assert Image.open(BytesIO(result)).size == (750, 1050)
    assert {entry[2]["fill"] for entry in calls} <= {"#2A2118", "#4A3F33"}
    actual = "".join(text for _, text, _ in calls)
    for index in range(3):
        assert f"Pouvoir {index}" in actual
    assert actual.replace(" ", "").count(description.replace(" ", "")) == 3
    for (x, y), text, kwargs in calls:
        assert kwargs["font"].size >= 17
        left, top, right, bottom = kwargs["font"].getbbox(text, anchor=kwargs["anchor"])
        assert 0 <= x + left <= x + right <= 750
        assert 0 <= y + top <= y + bottom <= 1050


def test_recipe_fit_guard_preserves_five_historical_abilities_before_omitting_flavor(monkeypatch):
    from app.card_artwork import _rules_layout

    recipe = load_recipe("portrait-debordant")
    abilities = [(f"Pouvoir {n}", "Une barrière protège les alliés proches.") for n in range(5)]
    layout = _rules_layout(abilities, "Ambiance " * 31, recipe, 750, 1050)
    rendered = "".join(item[1] for item in layout)
    assert "Ambiance" not in rendered
    assert all(name in rendered for name, _ in abilities)
    assert all(font.size >= 17 for _, _, font, _ in layout)


@pytest.mark.parametrize(
    "field,value",
    [
        ("abilities", [{"name": "Pouvoir", "description": "A" * 121}]),
        ("flavorText", "A" * 91),
    ],
)
def test_new_generation_limits_do_not_break_stored_payloads(field, value):
    from tests.test_foundry_agent_client import valid_card

    payload = valid_card() | {field: value}
    with pytest.raises(ValueError):
        generation.GeneratedCardModel.model_validate(payload)
    assert generation.CardPayload.model_validate(payload)
    assert generation.GeneratedCardModel.model_validate(valid_card()).accentColor is None


def test_released_hosted_output_beyond_recipe_bounds_falls_back_to_legacy_layout(
    authenticated_client, monkeypatch
):
    from tests.test_foundry_agent_client import (
        FakeCredential,
        completed_agent_payload,
        responses_envelope,
    )

    client = authenticated_client
    services = client.app.state.services
    # A released hosted version (v19) may return 180-character abilities, 280-character
    # flavor text and no accent while the new Web pin is rolling out.
    agent_payload = completed_agent_payload(metadata={"agentVersion": "profile-explanations-test"})
    description = ("Une barrière protège les alliés proches du gardien. " * 4)[:170]
    agent_payload["card"]["abilities"] = [{"name": ABILITY_NAME, "description": description}]
    agent_payload["card"]["flavorText"] = ("La mer écoute ceux qui savent lui parler. " * 6)[:270]
    agent_payload["card"].pop("accentColor", None)
    services.agent_client = FoundryAgentClient(
        replace(
            services.settings,
            foundry_agent_version="19",
            foundry_agent_expected_version="profile-explanations-test",
        ),
        credential=FakeCredential(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses_envelope(agent_payload))
        ),
    )
    original_image = services.ai_client.generate_image

    async def image(*args, **kwargs):
        result = await original_image(*args, **kwargs)
        return replace(result, image=replace(result.image, content=artwork_bytes()))

    async def extract(*args, **kwargs):
        pytest.fail("Output outside the recipe bounds must not reach subject extraction")

    monkeypatch.setattr(services.ai_client, "generate_image", image)
    monkeypatch.setattr(services.ai_client, "extract_subject", extract, raising=False)
    response, _ = generate_selected(client)
    assert response.status_code == 200, response.text
    (record,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    assert record.status == "completed" and record.abilities_embedded
    assert record.artwork_layout == "illustration_only_v1" and record.card_style is None
    assert record.validated_payload["abilities"][0]["description"] == description.strip()
    assert record.asset_names == [record.blob_name] and not record.artwork_assets
    assert client.get(f"/my/cards/{record.id}").status_code == 200
    asyncio.run(services.agent_client.aclose())


@pytest.mark.parametrize("outcome", ["success", "unsupported", "timeout", "opaque", "refused"])
def test_historical_pending_extraction_recomposition_and_deletion(
    authenticated_client, monkeypatch, outcome
):
    client = authenticated_client
    services = client.app.state.services
    # Image composition runs real Pillow work; a 0.6 s request budget is runner-dependent.
    services.settings = replace(
        services.settings, retry=replace(services.settings.retry, overall_timeout_seconds=5.0)
    )
    calls = []
    original_image = services.ai_client.generate_image

    async def image(*args, **kwargs):
        result = await original_image(*args, **kwargs)
        return replace(result, image=replace(result.image, content=artwork_bytes()))

    async def extract(prompt, **kwargs):
        calls.append(prompt)
        assert len(prompt) < 4000
        assert kwargs["scene"].content == artwork_bytes()
        if outcome in {"unsupported", "refused"}:
            raise generation.UpstreamServiceError(
                "foundry-image",
                "private-provider-error",
                status_code=400,
                error_code="content_filter" if outcome == "refused" else "unsupported_operation",
            )
        if outcome == "timeout":
            raise TimeoutError()
        result = await original_image(prompt, request_id=kwargs["request_id"])
        return replace(
            result,
            image=replace(
                result.image, content=(subject_bytes() if outcome == "success" else artwork_bytes())
            ),
        )

    monkeypatch.setattr(services.ai_client, "generate_image", image)
    monkeypatch.setattr(services.ai_client, "extract_subject", extract, raising=False)
    _, form = generate_selected(client, direction="[[mock:image-500]]")
    (pending,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    pending.artwork_layout = "portrait_debordant_v1"
    pending.card_style = CardStyle(recipe="portrait-debordant", accent="#2F5DA8")
    pending.derived_art_prompt = "A quiet guardian fills the old window."
    asyncio.run(services.card_repository.save(pending))
    response = client.post(
        f"/api/v1/cards/{pending.id}/artwork/retry",
        json={"csrfToken": form["csrf_token"], "idempotencyKey": uuid4().hex},
    )
    assert len(calls) == 1
    records = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    if outcome == "refused":
        assert response.status_code == 422 and not services.asset_store._assets
        assert [r.status for r in records] == ["awaiting_artwork_retry"]
        return
    assert response.status_code == 200, response.text
    (record,) = records
    assert record.card_style.recipe == "portrait-debordant"
    assert ("extraction" in record.usage) == (outcome in {"success", "opaque"})
    # Source layers are snapshots of the single card blob, so earlier revisions that delete
    # only the card blob (with its snapshots) leave no orphaned layer.
    assert record.asset_names == [record.blob_name]
    roles = {"scene", "subject"} if outcome == "success" else {"scene"}
    assert set(record.artwork_assets) == roles
    assert {asset["blobName"] for asset in record.artwork_assets.values()} == {record.blob_name}
    assert len(services.asset_store._snapshots[record.blob_name]) == len(roles)
    assert "accentColor" not in record.validated_payload
    restored = StoredCard.from_document(record.to_document())
    assert restored.card_style == record.card_style
    assert restored.artwork_assets == record.artwork_assets
    composed = asyncio.run(generation.recompose_stored_card(restored, services.asset_store))
    assert composed == client.get(record.image_url_path).content
    assert len(calls) == 1
    deleted = client.delete(
        f"/api/v1/cards/{record.id}", headers={"x-csrf-token": form["csrf_token"]}
    )
    assert deleted.status_code == 204
    assert not services.asset_store._assets and not services.asset_store._snapshots


def test_legacy_five_ability_card_still_loads_in_library(authenticated_client):
    client = authenticated_client
    services = client.app.state.services
    response, _ = generate_selected(client)
    assert response.status_code == 200
    (record,) = asyncio.run(services.card_repository.list_by_owner(TEST_OWNER_ID))
    document = record.to_document()
    document["artworkLayout"] = "illustration_only_v1"
    document["validatedPayload"]["abilities"] *= 5
    document["validatedPayload"]["flavorText"] = "A" * 280
    for ability in document["validatedPayload"]["abilities"]:
        ability["description"] = "A" * 180
    asyncio.run(services.card_repository.save(StoredCard.from_document(document)))
    assert client.get("/my/cards").status_code == 200
    assert client.get(f"/my/cards/{record.id}").status_code == 200


def test_accented_multiline_glyphs_do_not_overlap(monkeypatch):
    boxes = []
    original = ImageDraw.ImageDraw.text

    def text(draw, position, value, **kwargs):
        if kwargs.get("anchor") == "lt":
            left, top, right, bottom = kwargs["font"].getbbox(value, anchor="lt")
            boxes.append((position[1] + top, position[1] + bottom))
        return original(draw, position, value, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    title = "Épée légendaire du gardien des marées et des îles oubliées"
    compose_card_artwork(
        artwork_bytes(),
        [(title, "Écarte les ennemis et protège les alliés du gardien.")],
        name=title,
        layout="portrait_debordant_v1",
        card_type="hero",
        rarity="rare",
        mana_cost=4,
        attack=3,
        health=5,
        flavor_text="",
    )
    assert len([box for box in boxes if box[0] < 150]) == 2
    assert all(current[0] >= previous[1] for previous, current in zip(boxes, boxes[1:]))


@pytest.mark.parametrize("stage", ["scene", "subject", "final", "save", "committed"])
def test_cancelled_persistence_cleans_attempt_but_preserves_committed_card(
    authenticated_client, monkeypatch, stage
):
    async def scenario():
        from tests.test_foundry_agent_client import valid_card

        services = authenticated_client.app.state.services
        owner = generation.AuthenticatedOwner(TEST_OWNER_ID, None, None, "test", None, None)
        payload = generation.GeneratedCardModel.model_validate(valid_card())
        partial = StoredCard(
            id="cancellation-card",
            document_type="card",
            owner_id=TEST_OWNER_ID,
            request_id="initial",
            idempotency_key="initial-card",
            request_hash="hash",
            status="awaiting_artwork_retry",
            validated_payload=payload.model_dump(),
            artwork_layout="portrait_debordant_v1",
        )
        await services.card_repository.save(partial)
        before = partial.to_document()
        reached, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_upload = services.asset_store.upload
        original_delete = services.asset_store.delete
        original_save = services.card_repository.save
        roles = iter(["scene", "subject", "final"])

        async def upload(name, content, mime):
            result = await original_upload(name, content, mime)
            role = next(roles)
            if stage == role:
                reached.set()
                await asyncio.Event().wait()
            return result

        async def delete(name):
            cleaning.set()
            await release.wait()
            await original_delete(name)

        async def save(record):
            if stage == "save":
                reached.set()
                await asyncio.Event().wait()
            result = await original_save(record)
            if stage == "committed":
                reached.set()
                await asyncio.Event().wait()
            return result

        monkeypatch.setattr(services.asset_store, "upload", upload)
        monkeypatch.setattr(services.asset_store, "delete", delete)
        monkeypatch.setattr(services.card_repository, "save", save)
        image = await services.ai_client.generate_image("An original safe scene", request_id="test")
        image = replace(image, image=replace(image.image, content=artwork_bytes()))
        subject = replace(image, image=replace(image.image, content=subject_bytes()))
        operation = asyncio.create_task(
            generation.CardGenerationService(services)._persist_completed(
                owner=owner,
                card_id=partial.id,
                request_id="cancelled",
                idempotency_key="retry-cancelled",
                request_hash="hash",
                prompt="A safe guardian",
                validated_payload=payload,
                derived_art_prompt="An original safe scene",
                moderation=[],
                text_result=generation.AITextResult(
                    payload.model_dump(), image.metadata, image.usage
                ),
                image_result=image,
                existing_card=partial,
                artwork_layout="portrait_debordant_v1",
                subject_result=subject,
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=5)
        operation.cancel()
        if stage != "committed":
            await asyncio.wait_for(cleaning.wait(), timeout=5)
            operation.cancel()
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        current = await services.card_repository.get(TEST_OWNER_ID, partial.id)
        if stage == "committed":
            assert current.status == "completed"
            assert set(services.asset_store._assets) == set(current.asset_names)
        else:
            assert current.to_document() == before
            assert not services.asset_store._assets and not services.asset_store._snapshots

    asyncio.run(scenario())
