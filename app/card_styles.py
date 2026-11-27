"""Validated, versioned local card recipes and deterministic accent normalization."""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Percent = Annotated[float, Field(ge=0, le=100)]
CardType = Literal["hero", "creature", "artifact", "spell"]
Rarity = Literal["common", "uncommon", "rare", "legendary"]
ArtworkLayout = Literal["illustration_only_v1", "portrait_debordant_v1", "cadre_classique_v1"]
DEFAULT_LAYOUT: ArtworkLayout = "cadre_classique_v1"
DEFAULT_RECIPE = "cadre-classique"
RECIPE_LAYOUTS = {DEFAULT_LAYOUT: DEFAULT_RECIPE, "portrait_debordant_v1": "portrait-debordant"}
IDENTITIES = {
    "ivory": "#DDD3B5",
    "blue": "#2860A0",
    "rose": "#BB5073",
    "green": "#35795A",
    "violet": "#55446F",
    "gold": "#BE923E",
}
AccentChoiceId = Literal["random", "ivory", "blue", "rose", "green", "violet", "gold"]
DEFAULT_ACCENT_CHOICE: AccentChoiceId = "random"


@dataclass(frozen=True, slots=True)
class AccentChoice:
    id: AccentChoiceId
    title: str
    color: str | None


ACCENT_CHOICES: tuple[AccentChoice, ...] = (
    AccentChoice("random", "Random (AI selected)", None),
    AccentChoice("ivory", "Ivory", IDENTITIES["ivory"]),
    AccentChoice("blue", "Blue", IDENTITIES["blue"]),
    AccentChoice("rose", "Rose", IDENTITIES["rose"]),
    AccentChoice("green", "Green", IDENTITIES["green"]),
    AccentChoice("violet", "Violet", IDENTITIES["violet"]),
    AccentChoice("gold", "Gold", IDENTITIES["gold"]),
)


def accent_choice_color(choice: AccentChoiceId | None) -> str | None:
    if choice in {None, "random"}:
        return None
    return IDENTITIES[choice]


class RecipeField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Box(RecipeField):
    x: Percent
    y: Percent
    w: Annotated[float, Field(gt=0, le=100)]
    h: Annotated[float, Field(gt=0, le=100)]

    @model_validator(mode="after")
    def inside_canvas(self) -> Self:
        if self.x + self.w > 100 or self.y + self.h > 100:
            raise ValueError("recipe_box_outside_canvas")
        return self

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            round(self.x * width / 100),
            round(self.y * height / 100),
            round((self.x + self.w) * width / 100),
            round((self.y + self.h) * height / 100),
        )


class Canvas(RecipeField):
    aspect_ratio: str
    reference_px: tuple[Annotated[int, Field(ge=640, le=2048)], Annotated[int, Field(le=4096)]]
    corner_radius_pct_w: Percent


class Trim(RecipeField):
    inset_pct_w: Percent
    stroke_pct_w: Percent


class CostToken(RecipeField):
    anchor: Literal["title_plaque.right"]
    diameter_pct_w: Percent


class Medallion(RecipeField):
    center: tuple[Percent, Percent]
    diameter_pct_w: Percent


class Geometry(RecipeField):
    border: Box
    trim: Trim
    title_plaque: Box
    cost_token: CostToken
    overflow_band: Box | None = None
    art_window: Box
    type_plaque: Box
    ability_panel: Box
    stat_medallion: Medallion

    @model_validator(mode="after")
    def ordered_regions(self) -> Self:
        title, band, art = self.title_plaque, self.overflow_band, self.art_window
        if not (
            title.y + title.h < (band or art).y
            and art.y + art.h < self.type_plaque.y
            and self.type_plaque.y + self.type_plaque.h < self.ability_panel.y
        ):
            raise ValueError("invalid_recipe_geometry_order")
        if band and not (
            abs(band.y + band.h - art.y) < 0.0001 and band.x == art.x and band.w == art.w
        ):
            raise ValueError("invalid_recipe_overflow_band")
        return self


class Overflow(RecipeField):
    direction: Literal["up_only"]
    max_extent: Literal["overflow_band"]
    requires: Literal["subject_mask"]
    technique: Literal["edit_extract"]
    transparent_fraction: tuple[float, float]
    fallback: Literal["none"]


class FixedPalette(RecipeField):
    ivory: Color
    parchment: Color
    brass: Color
    brass_dark: Color
    ink: Color
    flavor_ink: Color


class AccentPalette(RecipeField):
    saturation: tuple[float, float]
    lightness: tuple[float, float]
    fallback_by_card_type: dict[CardType, Color]
    border_tint: Annotated[float, Field(ge=0, le=1)]


class Palette(RecipeField):
    fixed: FixedPalette
    accent: AccentPalette


class TypeStyle(RecipeField):
    weight: Literal["regular", "semibold", "bold"] = "regular"
    style: Literal["normal", "italic"] = "normal"
    size: Annotated[float, Field(gt=0, le=10)]
    min: Annotated[float, Field(gt=0, le=10)] | None = None

    @model_validator(mode="after")
    def minimum_size(self) -> Self:
        if self.min is not None and self.min > self.size:
            raise ValueError("invalid_recipe_font_minimum")
        return self


class Typography(RecipeField):
    family: Literal["EB Garamond"]
    title: TypeStyle
    cost: TypeStyle
    type_line: TypeStyle
    ability_name: TypeStyle
    ability_text: TypeStyle
    flavor: TypeStyle
    stats_label: TypeStyle
    stats_value: TypeStyle


class GenerationLimits(RecipeField):
    abilities_max: Literal[3]
    ability_description_chars: tuple[Literal[20], Literal[120]]
    flavor_text_chars_max: Literal[90]


class RarityFinish(RecipeField):
    emblem: Color
    finish: Literal["matte", "satin_brushed_trim", "pearly_sheen_trim", "gold_leaf_trim_engraved"]
    emblem_glow: Literal["soft"] | None = None


class CardRecipe(RecipeField):
    id: str
    version: Annotated[int, Field(ge=1)]
    label: str
    canvas: Canvas
    layers: list[str]
    geometry: Geometry
    portrait_overflow: Overflow | None = None
    palette: Palette
    typography: Typography
    generation_limits: GenerationLimits
    text_fit_order: list[str]
    rarity: dict[Rarity, RarityFinish]

    @model_validator(mode="after")
    def supported_layers(self) -> Self:
        layers = [
            "border",
            "trim",
            "title_plaque",
            "overflow_band",
            "art_window",
            "portrait_overflow",
            "type_plaque",
            "ability_panel",
            "stat_medallion",
            "finish_overlay",
        ]
        band = self.geometry.overflow_band
        if self.id == DEFAULT_RECIPE:
            layers.remove("overflow_band")
            layers.remove("portrait_overflow")
            if band or self.portrait_overflow:
                raise ValueError("unexpected_recipe_overflow")
        elif self.id != "portrait-debordant" or not (band and self.portrait_overflow):
            raise ValueError("unsupported_recipe")
        if self.layers != layers:
            raise ValueError("unsupported_recipe_layers")
        if self.text_fit_order != ["shrink_ability_text_to_min", "drop_flavor_text"]:
            raise ValueError("unsupported_recipe_text_fit")
        if set(self.rarity) != {"common", "uncommon", "rare", "legendary"} or set(
            self.palette.accent.fallback_by_card_type
        ) != {"hero", "creature", "artifact", "spell"}:
            raise ValueError("incomplete_recipe_variants")
        ranges = [self.palette.accent.saturation, self.palette.accent.lightness]
        if self.portrait_overflow:
            ranges.append(self.portrait_overflow.transparent_fraction)
        for low, high in ranges:
            if not 0 <= low < high <= 1:
                raise ValueError("invalid_recipe_range")
        width, height = self.canvas.reference_px
        title = self.geometry.title_plaque
        token_bottom = (title.y + title.h / 2) * height / 100 + (
            self.geometry.cost_token.diameter_pct_w * width / 200
        )
        if token_bottom >= (band or self.geometry.art_window).y * height / 100:
            raise ValueError("recipe_cost_intersects_overflow")
        return self


class CardStyle(RecipeField):
    recipe: str = DEFAULT_RECIPE
    version: int = 1
    accent: Color


@lru_cache(maxsize=16)
def load_recipe(recipe_id: str = DEFAULT_RECIPE, version: int = 1) -> CardRecipe:
    if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", recipe_id) or not 1 <= version <= 999:
        raise ValueError("invalid_recipe_identity")
    filename = f"{recipe_id.replace('-', '_')}_v{version}.yaml"
    path = Path(__file__).parent / "card_recipes" / filename
    recipe = CardRecipe.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if (recipe.id, recipe.version) != (recipe_id, version):
        raise ValueError("recipe_identity_mismatch")
    return recipe


def normalize_accent(value: str | None, card_type: str, recipe: CardRecipe | None = None) -> str:
    recipe = recipe or load_recipe()
    palette = recipe.palette.accent
    if card_type not in palette.fallback_by_card_type:
        raise ValueError("invalid_card_type")
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return palette.fallback_by_card_type[card_type]
    rgb = tuple(int(value[index : index + 2], 16) / 255 for index in (1, 3, 5))
    hue, lightness, saturation = colorsys.rgb_to_hls(*rgb)
    if recipe.id == DEFAULT_RECIPE:
        degrees = hue * 360
        if value.upper() in IDENTITIES.values():
            return value.upper()
        if lightness < 0.18 or (saturation < 0.20 and lightness < 0.55):
            return IDENTITIES["violet"]
        if saturation < 0.20 or (lightness > 0.82 and 30 <= degrees <= 65):
            return IDENTITIES["ivory"]
        if 24 <= degrees < 65:
            return IDENTITIES["gold"]
        if 65 <= degrees < 165:
            return IDENTITIES["green"]
        if 165 <= degrees < 255:
            return IDENTITIES["blue"]
        if 255 <= degrees < 305:
            return IDENTITIES["violet"]
        return IDENTITIES["rose"]
    lightness = min(max(lightness, palette.lightness[0]), palette.lightness[1])
    saturation = min(max(saturation, palette.saturation[0]), palette.saturation[1])
    return "#" + "".join(
        f"{round(component * 255):02X}"
        for component in colorsys.hls_to_rgb(hue, lightness, saturation)
    )
