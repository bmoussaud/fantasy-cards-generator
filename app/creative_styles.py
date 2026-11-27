from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ArtStyleId = Literal["paper_arcana", "engraved_legends", "paper_cut_myths"]
DEFAULT_ART_STYLE: ArtStyleId = "paper_arcana"


@dataclass(frozen=True, slots=True)
class CreativeStyle:
    id: ArtStyleId
    title: str
    prompt: str


CREATIVE_STYLES: tuple[CreativeStyle, ...] = (
    CreativeStyle(
        id="paper_arcana",
        title="Paper Arcana",
        prompt=(
            "Vintage fantasy trading-card illustration in traditional 2D animation with a warm, "
            "hand-crafted finish. Use expressive, welcoming and mischievous character acting, "
            "simplified but credible anatomy, and large expressive eyes without manga "
            "exaggeration. "
            "Draw clean, slightly irregular variable-width lines: stronger outer contours, fine "
            "interior details, grouped graphic hair shapes and clearly described clothing folds. "
            "Paint with opaque gouache-like shapes, two or three distinct shadow values and only "
            "subtle skin transitions. Light softly from the upper left with restrained highlights "
            "and readable volumes, never photographic. Add fine paper grain, gentle pigment "
            "granulation and a matte, subtly aged print finish. Compose a dominant bust portrait "
            "with a clear silhouette and natural dynamic pose; simplify the background into broad, "
            "low-contrast clouds and magical wisps. Avoid photorealism, 3D rendering, plastic "
            "skin, "
            "realistic video-game art, manga, thick oil paint, airbrush gradients, dramatic "
            "cinematic lighting, excessive detail and text."
        ),
    ),
    CreativeStyle(
        id="engraved_legends",
        title="Engraved Legends",
        prompt=(
            "Fantasy illustration rendered like an antique hand-printed engraving from a noble "
            "book of legends, with credible, lightly stylized anatomy. Use incisive, fine and "
            "slightly irregular contours. Construct volume through hatching and cross-hatching of "
            "varying density; make clothing folds angular and hair a set of parallel line bundles. "
            "Keep the face vivid and benevolent with an intense gaze, shaped by fine hatching "
            "around the eyes, nose and cheekbones while remaining immediately readable. Use no "
            "painted fills or smooth gradients: light comes from untouched paper and shadow from "
            "accumulated marks, with a clear upper-left light source and firm contrast. Show "
            "fibrous paper, subtle plate pressure, uneven ink and small printing imperfections "
            "with a matte "
            "finish. Compose a majestic bust portrait with a clear silhouette; render landscapes, "
            "clouds and magical wisps as lighter engraved motifs behind the subject. Avoid "
            "photorealism, 3D rendering, smooth gradients, airbrushing, photographic effects, "
            "overloaded micro-detail and text."
        ),
    ),
    CreativeStyle(
        id="paper_cut_myths",
        title="Paper-Cut Myths",
        prompt=(
            "Fantasy illustration built from layered cut paper, between miniature theatre and "
            "editorial illustration, with clean shapes and elegant, slightly exaggerated "
            "proportions. Use no drawn outlines: define forms with crisp cut edges and subtle "
            "hand-made irregularities. Organize hair, clothing and accessories into distinct large "
            "masses. Build a memorable expression from very few shapes, reducing eyes, eyebrows "
            "and smile to simple expressive cut pieces; suggest facial volume with two or three "
            "overlapping layers. Use flat colors without painted modelling. Create depth through "
            "stacked layers and fine cast shadows, lit softly from the side to reveal paper "
            "relief. "
            "Show thick matte paper, discreet fibers, slightly fuzzy edges and tactile depth. "
            "Compose a theatrical, dynamic bust portrait with a strong silhouette and a staged "
            "multi-plane setting of simplified clouds and magical elements; concentrate detail on "
            "the face and principal prop. Avoid photorealism, plastic surfaces, realistic 3D "
            "modelling, airbrush gradients, inked contours, tiny details and text."
        ),
    ),
)

CREATIVE_STYLES_BY_ID = {style.id: style for style in CREATIVE_STYLES}


def creative_style_prompt(style_id: ArtStyleId | None) -> str | None:
    return CREATIVE_STYLES_BY_ID[style_id].prompt if style_id is not None else None
