"""Deterministic, local inscription of already validated card abilities."""

from __future__ import annotations

import math
import random
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from app.card_styles import DEFAULT_LAYOUT, ArtworkLayout, CardStyle, TypeStyle, load_recipe

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSans.ttf"
SERIF_PATH = FONT_PATH.parent / "eb-garamond"


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
        line = ""
        for character in paragraph:
            candidate = line + character
            if line and font.getlength(candidate) > width:
                split = line.rfind(" ")
                if split > 0:
                    lines.append(line[:split])
                    line = line[split + 1 :] + character
                else:
                    lines.append(line)
                    line = character
            else:
                line = candidate
        lines.append(line)
    return lines


def _validate_abilities(abilities: list[tuple[str, str]]) -> None:
    if len(abilities) > 5 or any(
        not 2 <= len(name) <= 80 or not 20 <= len(description) <= 180
        for name, description in abilities
    ):
        raise ValueError("invalid_card_abilities")


def _open_artwork(content: bytes) -> Image.Image:
    if len(content) > 32 * 1024 * 1024:
        raise ValueError("artwork_too_large")
    with Image.open(BytesIO(content), formats=["PNG", "JPEG", "WEBP"]) as opened:
        if max(opened.size) > 4096 or opened.width * opened.height > 16_777_216:
            raise ValueError("artwork_too_large")
        artwork = ImageOps.exif_transpose(opened).convert("RGBA")
    width = max(640, artwork.width)
    artwork_height = round(artwork.height * width / artwork.width)
    if artwork_height > 4096:
        raise ValueError("artwork_too_large")
    if artwork.width < width:
        artwork = artwork.resize((width, artwork_height), Image.Resampling.LANCZOS)
    return artwork


def _validate_characters(text: str, font: ImageFont.FreeTypeFont) -> None:
    missing = font.getmask(chr(0x10FFFF))
    missing_glyph = (missing.size, bytes(missing))
    for character in set(text):
        if ord(character) < 32 and character not in "\n\r\t":
            raise ValueError("invalid_card_text")
        if not character.isspace():
            glyph = font.getmask(character)
            if (glyph.size, bytes(glyph)) == missing_glyph:
                raise ValueError("unsupported_card_character")


def inscribe_card_abilities(content: bytes, abilities: list[tuple[str, str]]) -> bytes:
    """Keep the original footer layout for previously pending artwork retries."""
    if not abilities:
        return content
    _validate_abilities(abilities)
    artwork = _open_artwork(content)
    width = artwork.width
    padding = round(width * 0.045)
    text_width = width - 2 * padding
    font = ImageFont.truetype(str(FONT_PATH), size=round(width / 25))
    name_font = ImageFont.truetype(str(FONT_PATH), size=round(width / 23))
    _validate_characters("".join(name + description for name, description in abilities), font)
    line_height = round(width / 20)
    name_line_height = round(width / 18)
    gap = round(width / 35)
    layouts = [
        (_wrap_text(name, name_font, text_width), _wrap_text(description, font, text_width))
        for name, description in abilities
    ]
    footer_height = 2 * padding + sum(
        len(names) * name_line_height + len(descriptions) * line_height + gap
        for names, descriptions in layouts
    )
    height = artwork.height + footer_height
    if height > 16_384 or width * height > 32_000_000:
        raise ValueError("inscribed_artwork_too_large")
    canvas = Image.new("RGB", (width, height), "#191625")
    canvas.paste(artwork, (0, 0), artwork)
    draw = ImageDraw.Draw(canvas)
    draw.line(
        [(padding, artwork.height), (width - padding, artwork.height)],
        fill="#dbba7c",
        width=max(2, width // 256),
    )
    y = artwork.height + padding
    for names, descriptions in layouts:
        for line in names:
            draw.text((padding, y), line, font=name_font, fill="#ffe0a6", anchor="lt")
            y += name_line_height
        for line in descriptions:
            draw.text((padding, y), line, font=font, fill="#fcf7ed", anchor="lt")
            y += line_height
        y += gap
    result = BytesIO()
    canvas.save(result, format="PNG")
    return result.getvalue()


def _compose_legacy_card_artwork(
    content: bytes,
    abilities: list[tuple[str, str]],
    *,
    name: str,
    card_type: str,
    rarity: str,
    mana_cost: int,
    attack: int,
    health: int,
    flavor_text: str,
) -> bytes:
    """Frame illustration-only artwork and validated card text, never extra rules."""
    if not abilities:
        return content
    _validate_abilities(abilities)
    if (
        not 3 <= len(name) <= 80
        or card_type not in {"hero", "creature", "artifact", "spell"}
        or rarity not in {"common", "uncommon", "rare", "legendary"}
        or len(flavor_text) > 280
        or not 0 <= mana_cost <= 12
        or not 0 <= attack <= 20
        or not 1 <= health <= 20
    ):
        raise ValueError("invalid_card_metadata")
    artwork = _open_artwork(content)
    width = artwork.width
    inset, padding, gap = round(width * 0.045), round(width * 0.025), round(width * 0.018)
    x = inset + padding
    text_width = width - 2 * x
    title_font = ImageFont.truetype(str(FONT_PATH), size=round(width / 24))
    name_font = ImageFont.truetype(str(FONT_PATH), size=round(width / 25))
    font = ImageFont.truetype(str(FONT_PATH), size=round(width / 28))
    small_font = ImageFont.truetype(str(FONT_PATH), size=round(width / 34))
    _validate_characters(
        name + flavor_text + "".join(n + description for n, description in abilities), font
    )
    line_height, name_height = round(font.size * 1.5), round(name_font.size * 1.5)
    title_height, small_height = round(title_font.size * 1.5), round(small_font.size * 1.5)
    mana = f"Mana {mana_cost}"
    title_lines = _wrap_text(
        name, title_font, text_width - round(small_font.getlength(mana)) - padding
    )
    header_height = 2 * padding + len(title_lines) * title_height
    type_top = inset + header_height + gap
    art_top = type_top + small_height + gap
    art_height = round(width * 0.68)
    rules_top = art_top + art_height + gap
    layouts = [
        (_wrap_text(n, name_font, text_width), _wrap_text(description, font, text_width))
        for n, description in abilities
    ]
    rules_height = (
        2 * padding
        + sum(
            len(names) * name_height + len(descriptions) * line_height + gap
            for names, descriptions in layouts
        )
        - gap
    )
    flavor_lines = _wrap_text(flavor_text, small_font, text_width) if flavor_text else []
    stats_top = rules_top + rules_height + gap + len(flavor_lines) * small_height
    height = stats_top + small_height + 2 * padding + inset
    if height > 16_384 or width * height > 32_000_000:
        raise ValueError("inscribed_artwork_too_large")
    accent = {
        "common": "#b9b6bd",
        "uncommon": "#86d5ac",
        "rare": "#91c6ee",
        "legendary": "#eac574",
    }[rarity]
    canvas = Image.new("RGB", (width, height), "#191625")
    draw = ImageDraw.Draw(canvas)
    edge = max(2, round(width * 0.016))
    draw.rounded_rectangle(
        (edge, edge, width - edge, height - edge),
        radius=round(width * 0.025),
        outline=accent,
        width=max(2, round(width * 0.005)),
    )
    draw.rectangle((inset, inset, width - inset, inset + header_height), fill="#302b40")
    y = inset + padding
    for line in title_lines:
        draw.text((x, y), line, font=title_font, fill="#fff3d8", anchor="lt")
        y += title_height
    draw.text(
        (width - x - small_font.getlength(mana), inset + padding),
        mana,
        font=small_font,
        fill=accent,
        anchor="lt",
    )
    draw.text(
        (x, type_top),
        f"{card_type.capitalize()} / {rarity.capitalize()}",
        font=small_font,
        fill=accent,
        anchor="lt",
    )
    illustration = ImageOps.contain(
        artwork, (width - 2 * inset, art_height), method=Image.Resampling.LANCZOS
    )
    canvas.paste(
        illustration,
        ((width - illustration.width) // 2, art_top + (art_height - illustration.height) // 2),
        illustration,
    )
    draw.rectangle(
        (inset, art_top, width - inset, art_top + art_height),
        outline=accent,
        width=max(2, width // 512),
    )
    draw.rectangle(
        (inset, rules_top, width - inset, rules_top + rules_height),
        fill="#252132",
        outline=accent,
        width=max(2, width // 512),
    )
    y = rules_top + padding
    for names, descriptions in layouts:
        for line in names:
            draw.text((x, y), line, font=name_font, fill="#ffe0a6", anchor="lt")
            y += name_height
        for line in descriptions:
            draw.text((x, y), line, font=font, fill="#fcf7ed", anchor="lt")
            y += line_height
        y += gap
    y = rules_top + rules_height + gap
    for line in flavor_lines:
        draw.text((x, y), line, font=small_font, fill="#d1c9df", anchor="lt")
        y += small_height
    draw.text(
        (x, stats_top + padding),
        f"Attack {attack}   /   Health {health}",
        font=small_font,
        fill=accent,
        anchor="lt",
    )
    result = BytesIO()
    canvas.save(result, format="PNG")
    return result.getvalue()


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def normalize_scene(content: bytes) -> bytes:
    """Store metadata-free opaque PNGs so later recomposition needs no image call."""
    image = _open_artwork(content)
    opaque = Image.new("RGB", image.size, "#F1E8D3")
    opaque.paste(image, mask=image.getchannel("A"))
    return _png(opaque)


def validate_subject_cutout(content: bytes, scene: bytes) -> bytes:
    """Reject opaque, empty or independently reframed extraction results."""
    subject, source = _open_artwork(content), _open_artwork(scene)
    if subject.size != source.size:
        raise ValueError("subject_dimensions_mismatch")
    histogram = subject.getchannel("A").histogram()
    transparent = histogram[0] / (subject.width * subject.height)
    low, high = load_recipe().portrait_overflow.transparent_fraction
    if not low <= transparent <= high:
        raise ValueError("invalid_subject_transparency")
    return _png(subject)


def _serif(style: TypeStyle, height: int, *, minimum: bool = False) -> ImageFont.FreeTypeFont:
    size = style.min if minimum and style.min is not None else style.size
    return _load_serif(math.ceil(height * size / 100), style.weight, style.style == "italic")


@lru_cache(maxsize=64)
def _load_serif(size: int, weight: str, italic: bool = False) -> ImageFont.FreeTypeFont:
    filename = "EBGaramond-Italic.ttf" if italic else "EBGaramond.ttf"
    font = ImageFont.truetype(str(SERIF_PATH / filename), size)
    font.set_variation_by_axes([{"regular": 400, "semibold": 600, "bold": 700}[weight]])
    return font


def _mix(first: str, second: str, fraction: float) -> tuple[int, int, int]:
    return tuple(
        round(a * (1 - fraction) + b * fraction)
        for a, b in zip(ImageColor.getrgb(first), ImageColor.getrgb(second), strict=True)
    )


def _plaque(draw: ImageDraw.ImageDraw, box, *, fill, edge, highlight, cut=8) -> None:
    x0, y0, x1, y1 = box
    points = [
        (x0 + cut, y0),
        (x1 - cut, y0),
        (x1, y0 + cut),
        (x1, y1 - cut),
        (x1 - cut, y1),
        (x0 + cut, y1),
        (x0, y1 - cut),
        (x0, y0 + cut),
    ]
    draw.polygon([(x + 2, y + 3) for x, y in points], fill=edge)
    draw.polygon(points, fill=fill, outline=edge, width=2)
    draw.line([(x0 + cut, y0 + 3), (x1 - cut, y0 + 3)], fill=highlight, width=2)


def _icon(draw: ImageDraw.ImageDraw, kind: str, box, color: str, *, rarity=False) -> None:
    """Use the original geometry from templates/partials/icons.html."""
    x, y, right, bottom = box
    sx, sy = (right - x) / 24, (bottom - y) / 24

    def points(values):
        return [(x + a * sx, y + b * sy) for a, b in values]

    def circle(cx, cy, radius, fill=None):
        draw.ellipse(
            (
                x + (cx - radius) * sx,
                y + (cy - radius) * sy,
                x + (cx + radius) * sx,
                y + (cy + radius) * sy,
            ),
            fill=fill,
            outline=color,
            width=max(1, round(1.8 * sx)),
        )

    stroke = max(1, round(1.8 * sx))
    if rarity:
        shapes = {
            "uncommon": [(12, 3), (21, 12), (12, 21), (3, 12)],
            "rare": [
                (12, 2.5),
                (14.6, 8.8),
                (21.4, 9.4),
                (16.2, 13.9),
                (17.8, 20.6),
                (12, 17),
                (6.2, 20.6),
                (7.8, 13.9),
                (2.6, 9.4),
                (9.4, 8.8),
            ],
            "legendary": [(3, 8), (7, 11), (12, 5), (17, 11), (21, 8), (19.4, 18), (4.6, 18)],
        }
        if kind == "common":
            circle(12, 12, 6, color)
        else:
            draw.polygon(points(shapes[kind]), fill=color)
    elif kind == "hero":
        shield = [
            (12, 2.5),
            (19, 5.5),
            (19, 10.7),
            (18, 14.5),
            (15, 18.8),
            (12, 21.5),
            (9, 18.8),
            (6, 14.5),
            (5, 10.7),
            (5, 5.5),
        ]
        draw.line(points(shield + [shield[0]]), fill=color, width=stroke, joint="curve")
    elif kind == "creature":
        circle(8, 7, 1.6)
        circle(16, 7, 1.6)
        draw.arc(
            (x + 4 * sx, y + 10 * sy, x + 20 * sx, y + 26 * sy), 180, 360, fill=color, width=stroke
        )
    elif kind == "artifact":
        shape = [(12, 2.5), (18, 7.5), (15.6, 19.9), (8.4, 19.9), (6, 7.5), (12, 2.5)]
        draw.line(points(shape), fill=color, width=stroke, joint="curve")
        draw.line(points([(6, 7.5), (18, 7.5)]), fill=color, width=stroke)
    else:
        for a, b in (
            ((12, 3), (12, 7)),
            ((12, 17), (12, 21)),
            ((3, 12), (7, 12)),
            ((17, 12), (21, 12)),
            ((6, 6), (8.6, 8.6)),
            ((15.4, 15.4), (18, 18)),
            ((18, 6), (15.4, 8.6)),
            ((8.6, 15.4), (6, 18)),
        ):
            draw.line(points([a, b]), fill=color, width=stroke)


def _rules_layout(abilities, flavor, recipe, width, height):
    panel = recipe.geometry.ability_panel.pixels(width, height)
    inset = round(width * 0.023)
    left, top, right, bottom = panel
    medal = recipe.geometry.stat_medallion
    radius = medal.diameter_pct_w * width / 200
    medal_left = medal.center[0] * width / 100 - radius - inset
    medal_top = medal.center[1] * height / 100 - radius - inset
    styles = recipe.typography

    def attempt(minimum: bool, with_flavor: bool):
        name_font = _serif(styles.ability_name, height, minimum=minimum)
        body_font = _serif(styles.ability_text, height, minimum=minimum)
        flavor_font = _serif(styles.flavor, height, minimum=minimum)
        result = []
        y = top + inset

        def append(text, font, color):
            nonlocal y
            line_height = max(
                math.ceil(font.size * 1.12),
                font.getbbox(text.replace("\n", ""), anchor="lt")[3] + 2,
            )
            # Reserve the medallion only for lines that actually reach its area.
            remaining = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
            while remaining:
                limit = medal_left if y + line_height >= medal_top else right - inset
                lines = _wrap_text(remaining, font, int(limit - left - inset))
                line = lines[0]
                result.append(((left + inset, y), line, font, color))
                y += line_height
                consumed = len(line)
                remaining = remaining[consumed:].lstrip(" ")
                if remaining.startswith("\n"):
                    remaining = remaining[1:]
                if y > bottom - inset:
                    return False
            return True

        for name, description in abilities:
            if not append(name, name_font, recipe.palette.fixed.ink) or not append(
                description, body_font, recipe.palette.fixed.ink
            ):
                return None
            y += round(height * 0.007)
        if with_flavor and flavor:
            y += round(height * 0.009)
            if not append(flavor, flavor_font, recipe.palette.fixed.flavor_ink):
                return None
        return result

    for minimum, with_flavor in ((False, True), (True, True), (True, False)):
        layout = attempt(minimum, with_flavor)
        if layout is not None:
            return layout
    raise ValueError("card_text_does_not_fit")


def compose_card_artwork(
    content: bytes,
    abilities: list[tuple[str, str]],
    *,
    name: str,
    card_type: str,
    rarity: str,
    mana_cost: int,
    attack: int,
    health: int,
    flavor_text: str,
    layout: ArtworkLayout = DEFAULT_LAYOUT,
    card_style: CardStyle | None = None,
    subject_cutout: bytes | None = None,
) -> bytes:
    """Compose a versioned original card without asking the image model to letter it."""
    metadata = dict(
        name=name,
        card_type=card_type,
        rarity=rarity,
        mana_cost=mana_cost,
        attack=attack,
        health=health,
        flavor_text=flavor_text,
    )
    if layout == "illustration_only_v1":
        return _compose_legacy_card_artwork(content, abilities, **metadata)
    if layout != DEFAULT_LAYOUT:
        raise ValueError("unsupported_artwork_layout")
    from app.card_styles import normalize_accent

    style = card_style or CardStyle(accent=normalize_accent(None, card_type))
    recipe = load_recipe(style.recipe, style.version)
    _validate_abilities(abilities)
    if (
        not 3 <= len(name) <= 80
        or card_type not in {"hero", "creature", "artifact", "spell"}
        or rarity not in recipe.rarity
        or len(flavor_text) > 280
        or not 0 <= mana_cost <= 12
        or not 0 <= attack <= 20
        or not 1 <= health <= 20
    ):
        raise ValueError("invalid_card_metadata")
    width, height = recipe.canvas.reference_px
    palette, geometry, typography = recipe.palette.fixed, recipe.geometry, recipe.typography
    _validate_characters(
        name + flavor_text + "".join(n + d for n, d in abilities),
        _serif(typography.ability_text, height),
    )
    rules = _rules_layout(abilities, flavor_text, recipe, width, height)
    accent = style.accent
    canvas = Image.new(
        "RGB", (width, height), _mix(palette.ivory, accent, recipe.palette.accent.border_tint)
    )
    draw = ImageDraw.Draw(canvas)
    rng = random.Random(1)
    for _ in range(5500):
        x, y = rng.randrange(width), rng.randrange(height)
        draw.point((x, y), fill=_mix(palette.ivory, palette.brass, rng.uniform(0.05, 0.2)))
    outer = round(width * 0.012)
    draw.rounded_rectangle(
        (outer, outer, width - outer - 1, height - outer - 1),
        radius=round(recipe.canvas.corner_radius_pct_w * width / 100),
        outline=palette.ink,
        width=2,
    )
    trim = geometry.trim
    inset = round(trim.inset_pct_w * width / 100)
    trim_box = (inset, inset, width - inset, height - inset)
    _plaque(
        draw,
        trim_box,
        fill=palette.brass,
        edge=palette.brass_dark,
        highlight=palette.ivory,
        cut=round(width * 0.028),
    )
    stroke = max(2, round(trim.stroke_pct_w * width / 100))
    inner = tuple(v + stroke if i < 2 else v - stroke for i, v in enumerate(trim_box))
    _plaque(
        draw,
        inner,
        fill=palette.parchment,
        edge=palette.brass_dark,
        highlight=palette.ivory,
        cut=round(width * 0.027),
    )
    title = geometry.title_plaque.pixels(width, height)
    _plaque(draw, title, fill=palette.ivory, edge=palette.brass_dark, highlight="#FFF8E7", cut=12)
    diameter = geometry.cost_token.diameter_pct_w * width / 100
    cost_x = title[2] - diameter / 2 - width * 0.015
    cost_y = (title[1] + title[3]) / 2
    title_width = int(cost_x - diameter / 2 - title[0] - width * 0.055)
    title_font = _serif(typography.title, height)
    minimum = _serif(typography.title, height).size
    if typography.title.min is not None:
        minimum = math.ceil(height * typography.title.min / 100)
    while title_font.getlength(name) > title_width and title_font.size > minimum:
        title_font = _load_serif(title_font.size - 1, "semibold")
    title_lines = _wrap_text(name, title_font, title_width)
    title_heights = [title_font.getbbox(line, anchor="lt")[3] + 2 for line in title_lines]
    if sum(title_heights) > title[3] - title[1] - 10:
        raise ValueError("card_title_does_not_fit")
    y = cost_y - sum(title_heights) / 2
    for line, line_height in zip(title_lines, title_heights, strict=True):
        draw.text(
            (title[0] + width * 0.025, y), line, font=title_font, fill=palette.ink, anchor="lt"
        )
        y += line_height
    radius = diameter / 2
    token = (cost_x - radius, cost_y - radius, cost_x + radius, cost_y + radius)
    draw.ellipse(token, fill=accent, outline=palette.ink, width=3)
    draw.arc(
        (token[0] + 4, token[1] + 4, token[2] - 4, token[3] - 4), 180, 320, fill="#FFF8E7", width=2
    )
    # The numeral has its own ivory face; accent is never a text background.
    draw.ellipse(
        (token[0] + 10, token[1] + 10, token[2] - 10, token[3] - 10),
        fill=palette.ivory,
        outline=palette.brass,
        width=2,
    )
    draw.text(
        (cost_x, cost_y),
        str(mana_cost),
        font=_serif(typography.cost, height),
        fill=palette.ink,
        anchor="mm",
    )
    band = geometry.overflow_band.pixels(width, height)
    art = geometry.art_window.pixels(width, height)
    scene = _open_artwork(content)
    full_size = (art[2] - art[0], art[3] - band[1])
    fitted = ImageOps.fit(scene, full_size, centering=(0.5, 0.3))
    band_height = art[1] - band[1]
    canvas.paste(fitted.crop((0, band_height, full_size[0], full_size[1])), (art[0], art[1]))
    draw.rectangle(
        (art[0] - 2, art[1] - 2, art[2] + 1, art[3] + 1), outline=palette.brass_dark, width=3
    )
    if subject_cutout is not None:
        cutout = _open_artwork(validate_subject_cutout(subject_cutout, content))
        fitted_subject = ImageOps.fit(cutout, full_size, centering=(0.5, 0.3))
        canvas.paste(fitted_subject, (band[0], band[1]), fitted_subject)
    type_box = geometry.type_plaque.pixels(width, height)
    _plaque(
        draw, type_box, fill=palette.ivory, edge=palette.brass_dark, highlight="#FFF8E7", cut=10
    )
    icon_size = height * 0.038
    icon_y = (type_box[1] + type_box[3] - icon_size) / 2
    _icon(
        draw,
        card_type,
        (type_box[0] + 15, icon_y, type_box[0] + 15 + icon_size, icon_y + icon_size),
        accent,
    )
    labels = {"hero": "Héros", "creature": "Créature", "artifact": "Artefact", "spell": "Sort"}
    draw.text(
        (type_box[0] + 28 + icon_size, (type_box[1] + type_box[3]) / 2),
        labels[card_type],
        font=_serif(typography.type_line, height),
        fill=palette.ink,
        anchor="lm",
    )
    emblem_x = type_box[2] - icon_size - 15
    finish = recipe.rarity[rarity]
    if finish.emblem_glow:
        draw.ellipse(
            (emblem_x - 4, icon_y - 4, emblem_x + icon_size + 4, icon_y + icon_size + 4),
            fill=_mix(palette.ivory, finish.emblem, 0.18),
        )
    _icon(
        draw,
        rarity,
        (emblem_x, icon_y, emblem_x + icon_size, icon_y + icon_size),
        finish.emblem,
        rarity=True,
    )
    panel = geometry.ability_panel.pixels(width, height)
    _plaque(draw, panel, fill=palette.ivory, edge=palette.brass_dark, highlight="#FFF8E7", cut=5)
    for position, text, font, color in rules:
        draw.text(position, text, font=font, fill=color, anchor="lt")
    medal = geometry.stat_medallion
    cx, cy = medal.center[0] * width / 100, medal.center[1] * height / 100
    radius = medal.diameter_pct_w * width / 200
    draw.ellipse(
        (cx - radius - 3, cy - radius - 3, cx + radius + 3, cy + radius + 3),
        fill=palette.brass,
        outline=palette.ink,
        width=2,
    )
    draw.ellipse(
        (cx - radius + 3, cy - radius + 3, cx + radius - 3, cy + radius - 3),
        fill=palette.ivory,
        outline=palette.brass_dark,
        width=2,
    )
    draw.line(
        [(cx, cy - radius * 0.63), (cx, cy + radius * 0.65)], fill=palette.brass_dark, width=1
    )
    for offset, label, value in ((-0.43, "ATQ", attack), (0.43, "PV", health)):
        draw.text(
            (cx + radius * offset, cy - radius * 0.43),
            label,
            font=_serif(typography.stats_label, height),
            fill=palette.ink,
            anchor="mm",
        )
        draw.text(
            (cx + radius * offset, cy + radius * 0.25),
            str(value),
            font=_serif(typography.stats_value, height),
            fill=palette.ink,
            anchor="mm",
        )
    if finish.finish != "matte":
        overlay = Image.new("RGB", canvas.size, palette.brass)
        sheen = ImageDraw.Draw(overlay)
        for row in range(height):
            factor = (math.sin(row / 21) + 1) / 2
            color = _mix(palette.brass, finish.emblem, factor * 0.65)
            sheen.line((0, row, width, row), fill=color)
        if finish.finish == "gold_leaf_trim_engraved":
            for row in range(0, height, 12):
                sheen.line((0, row, width, row + width), fill=palette.brass_dark, width=1)
        mask = Image.new("L", canvas.size)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rectangle(trim_box, outline=255, width=stroke)
        # Ornament stays on the trim, outside every text/portrait region.
        canvas.paste(overlay, mask=mask)
    alpha = Image.new("L", canvas.size)
    ImageDraw.Draw(alpha).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=round(recipe.canvas.corner_radius_pct_w * width / 100),
        fill=255,
    )
    canvas.putalpha(alpha)
    return _png(canvas)
