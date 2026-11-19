"""Deterministic, local inscription of already validated card abilities."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSans.ttf"


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
