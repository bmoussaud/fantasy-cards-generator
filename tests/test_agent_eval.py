"""Offline evaluation-corpus checks for the proposed card-orchestrator agent.

These tests validate fixture shape, rubric honesty, and evaluator-local safety
classification only. They do not call Azure, Foundry, models, or agent runtimes.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.generation import GeneratedCardModel, HeuristicModerationService

FIXTURE_PATH = Path("tests/fixtures/eval/card-orchestrator-eval-seed-v1.jsonl")

REQUIRED_ROW_FIELDS = frozenset(
    {
        "id",
        "version",
        "source",
        "query",
        "category",
        "routing_specialists",
        "expected_behavior",
        "rubric_metadata",
        "provenance",
    }
)
VALID_CATEGORIES = frozenset(
    {
        "concept",
        "lore",
        "art-prompt",
        "ambiguity-format",
        "multi-step-coherence",
        "scope-boundary",
        "prompt-injection",
        "safety-refusal",
        "safety-indeterminate",
    }
)
VALID_SPECIALISTS = frozenset({"concept", "lore", "art-prompt"})
VALID_STATUSES = frozenset({"completed", "refused", "routing_defer", "held"})
VALID_SAFETY_OUTCOMES = frozenset({"allow", "block", "indeterminate"})
VALID_SCHEMA_COMPLIANCE = frozenset({"required", "not_applicable"})
VALID_QUALITY_DIMENSIONS = frozenset(
    {
        "concept_clarity",
        "schema_validity",
        "lore_originality",
        "art_prompt_safety",
        "thematic_coherence",
    }
)
KNOWN_LAYER_KEYS = frozenset(
    {"pre_prompt", "post_text", "post_art_prompt", "post_image", "foundry_hosted_guardrails"}
)
PROPOSED_AGENT_FIELDS = frozenset(
    {"schemaVersion", "status", "card", "artPrompt", "metadata", "safetyHints"}
)
CARD_FIELDS = frozenset(GeneratedCardModel.model_fields)

NOT_APPLICABLE = "not_applicable"
INDETERMINATE = "indeterminate"
INCONCLUSIVE = "inconclusive"
ALLOW = "allow"
BLOCK = "block"
VALID_LAYER_OUTCOMES = frozenset({NOT_APPLICABLE, INDETERMINATE, ALLOW, BLOCK})

_COPYRIGHT_PATTERNS = (
    r"\bmagic:\s*the\s*gathering\b",
    r"\bpokemon\b",
    r"\bpikachu\b",
    r"\bharry\s+potter\b",
    r"\bstar\s+wars\b",
    r"\byoda\b",
    r"\bhermione\b",
    r"\bdumbledore\b",
    r"\bfrodo\s+baggins\b",
    r"\bsauron\b",
    r"\bwarhammer\b",
    r"\bdungeons\s*&\s*dragons\b",
)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _expect_nonempty_strings(value: object, *, label: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not value and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    if not all(_nonempty_str(item) for item in value):
        raise ValueError(f"{label} must contain only non-empty strings")
    return value


def _validate_layer_config(row_id: str, layer_name: str, info: object) -> Mapping[str, Any]:
    if layer_name not in KNOWN_LAYER_KEYS:
        raise ValueError(f"Row {row_id}: unknown layer key {layer_name!r}")
    if not isinstance(info, dict):
        raise ValueError(f"Row {row_id}, layer {layer_name!r}: layer info must be a dict")

    for key in ("currently_active", "required"):
        if key not in info or not isinstance(info[key], bool):
            raise ValueError(f"Row {row_id}, layer {layer_name!r}: {key} must be a bool")

    absent = info.get("absent_counts_as")
    if absent not in {INDETERMINATE, NOT_APPLICABLE}:
        raise ValueError(
            f"Row {row_id}, layer {layer_name!r}: absent_counts_as must be "
            f"{INDETERMINATE!r} or {NOT_APPLICABLE!r}; policy metadata is not evidence"
        )
    if info["currently_active"] and info["required"] and absent != INDETERMINATE:
        raise ValueError(
            f"Row {row_id}, layer {layer_name!r}: active required layers must use "
            f"absent_counts_as={INDETERMINATE!r}"
        )
    if (not info["currently_active"] or not info["required"]) and absent != NOT_APPLICABLE:
        raise ValueError(
            f"Row {row_id}, layer {layer_name!r}: inactive or non-required layers must use "
            f"absent_counts_as={NOT_APPLICABLE!r}"
        )
    return info


def validate_corpus_row(row: object) -> None:
    """Validate one JSONL row; reject malformed objects instead of skipping them."""
    if not isinstance(row, dict):
        raise ValueError(f"corpus row must be a JSON object, got {type(row).__name__}")

    row_id = row.get("id", "<missing-id>")
    missing = REQUIRED_ROW_FIELDS - set(row)
    if missing:
        raise ValueError(f"Row {row_id}: missing required fields: {sorted(missing)}")

    for field in ("id", "version", "source", "query", "category", "provenance"):
        if not _nonempty_str(row.get(field)):
            raise ValueError(f"Row {row_id}: {field} must be a non-empty string")
    if not re.fullmatch(r"seed-v1-\d{3}", row["id"]):
        raise ValueError(f"Row {row_id}: id must match seed-v1-NNN")
    if row["version"] != "v1" or row["source"] != "synthetic":
        raise ValueError(f"Row {row_id}: version/source must be v1/synthetic")
    if row["category"] not in VALID_CATEGORIES:
        raise ValueError(f"Row {row_id}: unknown category {row['category']!r}")

    routes = _expect_nonempty_strings(
        row["routing_specialists"], label=f"Row {row_id}: routing_specialists", allow_empty=True
    )
    if len(routes) != len(set(routes)) or any(route not in VALID_SPECIALISTS for route in routes):
        raise ValueError(f"Row {row_id}: routing_specialists contains duplicates or unknown paths")

    eb = row["expected_behavior"]
    rm = row["rubric_metadata"]
    if not isinstance(eb, dict):
        raise ValueError(f"Row {row_id}: expected_behavior must be a dict")
    if not isinstance(rm, dict):
        raise ValueError(f"Row {row_id}: rubric_metadata must be a dict")

    for key in ("should_complete", "expected_status", "safety_outcome", "schema_compliance"):
        if key not in eb:
            raise ValueError(f"Row {row_id}: expected_behavior missing {key!r}")
    if not isinstance(eb["should_complete"], bool):
        raise ValueError(f"Row {row_id}: should_complete must be a bool")
    if eb["expected_status"] not in VALID_STATUSES:
        raise ValueError(f"Row {row_id}: invalid expected_status {eb['expected_status']!r}")
    if eb["safety_outcome"] not in VALID_SAFETY_OUTCOMES:
        raise ValueError(f"Row {row_id}: invalid safety_outcome {eb['safety_outcome']!r}")
    if eb["schema_compliance"] not in VALID_SCHEMA_COMPLIANCE:
        raise ValueError(f"Row {row_id}: invalid schema_compliance {eb['schema_compliance']!r}")
    if eb.get("agent_response_contract") != "proposed_generate_card_agent_response":
        raise ValueError(f"Row {row_id}: agent response fields/statuses must be labelled proposed")
    _expect_nonempty_strings(eb.get("manual_review_anchors"), label=f"Row {row_id}: anchors")

    status = eb["expected_status"]
    should_complete = eb["should_complete"]
    outcome = eb["safety_outcome"]
    compliance = eb["schema_compliance"]
    if should_complete != (status == "completed"):
        raise ValueError(f"Row {row_id}: should_complete must match expected_status")
    if status == "completed" and (outcome != ALLOW or compliance != "required"):
        raise ValueError(f"Row {row_id}: completed rows require allow + schema compliance")
    if status == "refused" and (outcome != BLOCK or compliance != "not_applicable"):
        raise ValueError(f"Row {row_id}: refused rows require block + schema not_applicable")
    if status == "routing_defer" and (outcome != ALLOW or compliance != "not_applicable"):
        raise ValueError(f"Row {row_id}: routing_defer rows require allow + schema not_applicable")
    if status == "held" and (outcome != INDETERMINATE or compliance != "not_applicable"):
        raise ValueError(f"Row {row_id}: held rows require indeterminate + schema not_applicable")

    if status == "completed":
        fields = frozenset(
            _expect_nonempty_strings(
                eb.get("expected_output_fields"), label=f"Row {row_id}: expected_output_fields"
            )
        )
        if fields != PROPOSED_AGENT_FIELDS:
            raise ValueError(f"Row {row_id}: expected_output_fields must match proposed envelope")
        card_fields = frozenset(
            _expect_nonempty_strings(
                eb.get("expected_card_fields"), label=f"Row {row_id}: expected_card_fields"
            )
        )
        if card_fields != CARD_FIELDS:
            raise ValueError(f"Row {row_id}: expected_card_fields must match GeneratedCardModel")
    for pattern in eb.get("prohibited_output_patterns", []):
        if not _nonempty_str(pattern):
            raise ValueError(f"Row {row_id}: prohibited_output_patterns must contain strings")
        re.compile(pattern)

    quality = rm.get("quality_dimensions")
    if not isinstance(quality, dict) or set(quality) != VALID_QUALITY_DIMENSIONS:
        raise ValueError(f"Row {row_id}: quality_dimensions must contain all valid dimensions")
    if not all(_nonempty_str(value) for value in quality.values()):
        raise ValueError(f"Row {row_id}: quality dimension anchors must be non-empty strings")
    if rm.get("evaluation_status") != "not_evaluated":
        raise ValueError(f"Row {row_id}: evaluation_status must be exactly 'not_evaluated'")
    if rm.get("example_output_source") != "none" and "synthetic_example_output" not in rm:
        raise ValueError(f"Row {row_id}: example output source must not imply a run")
    for forbidden in ("measured_score", "baseline_score", "scores", "raw_run_artifact"):
        if forbidden in rm:
            raise ValueError(f"Row {row_id}: {forbidden!r} is not allowed in seed corpus")

    layers = rm.get("safety_layer_applicability")
    if not isinstance(layers, dict):
        raise ValueError(f"Row {row_id}: safety_layer_applicability must be a dict")
    if set(layers) != KNOWN_LAYER_KEYS:
        raise ValueError(f"Row {row_id}: safety_layer_applicability must include exact layer map")
    for layer_name, info in layers.items():
        _validate_layer_config(row_id, layer_name, info)


def classify_layer_outcome(layer_info: Mapping[str, Any], decision: str | None = None) -> str:
    """Classify one configured layer using observed decision evidence only."""
    if not isinstance(layer_info, Mapping):
        raise ValueError(f"layer info must be a mapping, got {type(layer_info).__name__}")
    for key in ("currently_active", "required"):
        if key not in layer_info or not isinstance(layer_info[key], bool):
            raise ValueError(f"{key} must be a bool")

    observed = decision if decision is not None else layer_info.get("decision")
    if observed is not None:
        if observed not in {ALLOW, BLOCK}:
            raise ValueError(f"unknown observed layer decision: {observed!r}")
        return observed
    if not layer_info["currently_active"] or not layer_info["required"]:
        return NOT_APPLICABLE
    return INDETERMINATE


def classify_evaluation(
    layers: Mapping[str, Mapping[str, Any]], evidence: Mapping[str, str] | None = None
) -> str:
    """Aggregate evaluator-local layer outcomes for one run.

    ``evidence is None`` means no run happened, so the aggregate is inconclusive.
    ``evidence == {}`` means a run started but yielded no required evidence.
    """
    if evidence is None:
        return INCONCLUSIVE
    if not isinstance(layers, Mapping) or not layers:
        raise ValueError("layer manifest must be a non-empty mapping")

    validated: dict[str, Mapping[str, Any]] = {}
    for layer_name, layer_info in layers.items():
        if layer_name not in KNOWN_LAYER_KEYS:
            raise ValueError(f"unknown layer key {layer_name!r}")
        info = _validate_layer_config("<evaluator>", layer_name, layer_info)
        validated[layer_name] = info
        observed = evidence.get(layer_name)
        if observed not in {None, ALLOW, BLOCK}:
            raise ValueError(f"unknown observed layer decision: {observed!r}")
        if observed == BLOCK and info["currently_active"]:
            return BLOCK

    saw_required = False
    saw_required_allow = False
    for layer_name, info in validated.items():
        outcome = classify_layer_outcome(info, evidence.get(layer_name))
        if info["currently_active"] and info["required"]:
            saw_required = True
            if outcome == INDETERMINATE:
                return INDETERMINATE
            if outcome == ALLOW:
                saw_required_allow = True
    if not saw_required:
        raise ValueError("layer manifest has no applicable required layers")
    return ALLOW if saw_required_allow else INDETERMINATE


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                pytest.fail(f"Line {line_no}: expected JSON object")
            rows.append(row)
    return rows


def test_fixture_file_is_jsonl(corpus: list[dict[str, Any]]) -> None:
    assert FIXTURE_PATH.exists()
    assert len(corpus) == 20
    assert len({row["id"] for row in corpus}) == 20
    assert len({row["query"] for row in corpus}) == 20


def test_corpus_coverage_and_shared_validator(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        validate_corpus_row(row)
    assert {row["category"] for row in corpus} == VALID_CATEGORIES
    routed = {route for row in corpus for route in row["routing_specialists"]}
    assert routed == VALID_SPECIALISTS


def test_queries_do_not_use_copyrighted_franchise_markers(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        for pattern in _COPYRIGHT_PATTERNS:
            assert re.search(pattern, row["query"], re.IGNORECASE) is None


def test_scope_and_copyright_refusals_do_not_forbid_safe_refusal_words(
    corpus: list[dict[str, Any]],
) -> None:
    for row_id in ("seed-v1-015", "seed-v1-016", "seed-v1-019"):
        row = next(row for row in corpus if row["id"] == row_id)
        assert row["expected_behavior"].get("prohibited_output_patterns", []) == []
        assert row["expected_behavior"]["manual_review_anchors"]


def test_completed_rows_use_generated_card_model_fields(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        if row["expected_behavior"]["expected_status"] == "completed":
            assert set(row["expected_behavior"]["expected_card_fields"]) == CARD_FIELDS


def test_representative_generated_card_model_validation() -> None:
    valid_card = {
        "schemaVersion": 1,
        "name": "Ember Drake",
        "cardType": "creature",
        "rarity": "uncommon",
        "manaCost": 4,
        "attack": 5,
        "health": 3,
        "rulesText": "When Ember Drake enters play, deal 2 damage to any target.",
        "flavorText": "Born from the dying breath of a volcano.",
        "artBrief": "Orange-scaled ember drake soaring over a volcanic crater at dusk.",
    }
    GeneratedCardModel.model_validate(valid_card)
    invalid_card = valid_card | {"cardType": "villain"}
    with pytest.raises(ValidationError):
        GeneratedCardModel.model_validate(invalid_card)


def test_seed_019_query_blocked_by_current_heuristic(corpus: list[dict[str, Any]]) -> None:
    row = next(row for row in corpus if row["id"] == "seed-v1-019")
    decision = asyncio.run(
        HeuristicModerationService("test-policy").moderate_text(row["query"], stage="pre_prompt")
    )
    assert decision.allowed is False
    assert decision.reasonCode == "copyrighted-logo"


def test_prompt_injection_rows_are_proposed_not_current_heuristic_blocks(
    corpus: list[dict[str, Any]],
) -> None:
    service = HeuristicModerationService("test-policy")
    for row in corpus:
        if row["category"] == "prompt-injection":
            assert row["expected_behavior"]["expected_status"] == "refused"
            assert row["expected_behavior"]["behavior_expectation"] == "proposed"
            decision = asyncio.run(service.moderate_text(row["query"], stage="pre_prompt"))
            assert decision.allowed is True


@pytest.mark.parametrize(
    ("layer", "decision", "expected"),
    [
        (
            {"currently_active": False, "required": False, "absent_counts_as": NOT_APPLICABLE},
            None,
            NOT_APPLICABLE,
        ),
        (
            {"currently_active": True, "required": False, "absent_counts_as": NOT_APPLICABLE},
            None,
            NOT_APPLICABLE,
        ),
        (
            {"currently_active": True, "required": True, "absent_counts_as": INDETERMINATE},
            None,
            INDETERMINATE,
        ),
        ({"currently_active": True, "required": True}, None, INDETERMINATE),
        (
            {"currently_active": True, "required": True, "absent_counts_as": BLOCK},
            None,
            INDETERMINATE,
        ),
        (
            {"currently_active": True, "required": True, "absent_counts_as": INDETERMINATE},
            BLOCK,
            BLOCK,
        ),
        (
            {"currently_active": True, "required": True, "absent_counts_as": INDETERMINATE},
            ALLOW,
            ALLOW,
        ),
    ],
)
def test_classify_layer_outcome_uses_observed_evidence_only(
    layer: dict[str, Any], decision: str | None, expected: str
) -> None:
    assert classify_layer_outcome(layer, decision) == expected


def test_classify_layer_outcome_rejects_bad_decisions_and_policy_as_terminal() -> None:
    with pytest.raises(ValueError, match="currently_active"):
        classify_layer_outcome({"required": True, "absent_counts_as": INDETERMINATE})
    with pytest.raises(ValueError, match="unknown observed"):
        classify_layer_outcome(
            {"currently_active": True, "required": True, "absent_counts_as": INDETERMINATE},
            "defer",
        )


def _layers_for_required(*required: str) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "currently_active": name != "foundry_hosted_guardrails",
            "required": name in required,
            "absent_counts_as": INDETERMINATE if name in required else NOT_APPLICABLE,
        }
        for name in KNOWN_LAYER_KEYS
    }


def test_classify_evaluation_no_run_is_inconclusive() -> None:
    layers = _layers_for_required("pre_prompt")
    assert classify_evaluation(layers, None) == INCONCLUSIVE


def test_classify_evaluation_started_missing_required_is_indeterminate() -> None:
    layers = _layers_for_required("pre_prompt", "post_text")
    assert classify_evaluation(layers, {}) == INDETERMINATE
    assert classify_evaluation(layers, {"pre_prompt": ALLOW}) == INDETERMINATE


def test_classify_evaluation_block_dominates_and_all_required_allow() -> None:
    layers = _layers_for_required("pre_prompt", "post_text")
    assert classify_evaluation(layers, {"pre_prompt": ALLOW, "post_text": ALLOW}) == ALLOW
    assert classify_evaluation(layers, {"pre_prompt": ALLOW, "post_text": BLOCK}) == BLOCK
    assert classify_evaluation(layers, {"post_image": BLOCK}) == BLOCK


def test_classify_evaluation_rejects_vacuous_or_unknown_manifests() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        classify_evaluation({}, {})
    with pytest.raises(ValueError, match="unknown layer"):
        classify_evaluation({"surprise": {"currently_active": True, "required": True}}, {})  # type: ignore[arg-type]


def test_seed_020_explicit_missing_post_text_evidence_holds(corpus: list[dict[str, Any]]) -> None:
    row = next(row for row in corpus if row["id"] == "seed-v1-020")
    layers = row["rubric_metadata"]["safety_layer_applicability"]
    assert classify_evaluation(layers, None) == INCONCLUSIVE
    assert classify_evaluation(layers, {"pre_prompt": ALLOW}) == INDETERMINATE
    assert row["expected_behavior"]["expected_status"] == "held"


RowMutation = Callable[[dict[str, Any]], None]


def _delete_path(path: tuple[str, ...]) -> RowMutation:
    def mutate(row: dict[str, Any]) -> None:
        target: dict[str, Any] = row
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _set_path(path: tuple[str, ...], value: Any) -> RowMutation:
    def mutate(row: dict[str, Any]) -> None:
        target: dict[str, Any] = row
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (_delete_path(("expected_behavior", "should_complete")), "should_complete"),
        (_delete_path(("expected_behavior", "expected_status")), "expected_status"),
        (_set_path(("expected_behavior", "should_complete"), False), "should_complete"),
        (_set_path(("expected_behavior", "expected_status"), "refused"), "should_complete"),
        (_set_path(("expected_behavior", "safety_outcome"), "maybe"), "safety_outcome"),
        (_set_path(("expected_behavior", "schema_compliance"), "optional"), "schema_compliance"),
        (_delete_path(("expected_behavior", "manual_review_anchors")), "anchors"),
        (
            _set_path(("expected_behavior", "expected_output_fields"), ["status"]),
            "proposed envelope",
        ),
        (_set_path(("expected_behavior", "expected_card_fields"), ["name"]), "GeneratedCardModel"),
        (_set_path(("expected_behavior", "prohibited_output_patterns"), ["["]), "unterminated"),
        (_set_path(("routing_specialists",), ["concept", "unknown"]), "routing_specialists"),
        (_set_path(("rubric_metadata", "quality_dimensions"), {}), "quality_dimensions"),
        (_delete_path(("rubric_metadata", "quality_dimensions")), "quality_dimensions"),
        (_set_path(("rubric_metadata", "evaluation_status"), None), "evaluation_status"),
        (_set_path(("rubric_metadata", "baseline_score"), 4.2), "baseline_score"),
        (
            _delete_path(("rubric_metadata", "safety_layer_applicability")),
            "safety_layer_applicability",
        ),
        (
            _set_path(("rubric_metadata", "safety_layer_applicability"), None),
            "safety_layer_applicability",
        ),
        (
            _set_path(
                ("rubric_metadata", "safety_layer_applicability", "pre_prompt", "currently_active"),
                None,
            ),
            "currently_active",
        ),
        (
            _delete_path(
                ("rubric_metadata", "safety_layer_applicability", "pre_prompt", "required")
            ),
            "required",
        ),
        (
            _set_path(
                ("rubric_metadata", "safety_layer_applicability", "pre_prompt", "absent_counts_as"),
                BLOCK,
            ),
            "absent_counts_as",
        ),
        (
            _set_path(
                ("rubric_metadata", "safety_layer_applicability", "post_image", "required"),
                True,
            ),
            "active required",
        ),
    ],
)
def test_validate_corpus_row_rejects_targeted_mutations(
    corpus: list[dict[str, Any]], mutation: RowMutation, match: str
) -> None:
    row = copy.deepcopy(corpus[0])
    validate_corpus_row(row)
    mutation(row)
    with pytest.raises((ValueError, re.error), match=match):
        validate_corpus_row(row)


def test_validate_corpus_row_rejects_non_object_and_unknown_layer(
    corpus: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        validate_corpus_row(["not", "an", "object"])

    row = copy.deepcopy(corpus[0])
    layers = row["rubric_metadata"]["safety_layer_applicability"]
    layers["unknown_stage"] = layers.pop("pre_prompt")
    with pytest.raises(ValueError, match="exact layer map"):
        validate_corpus_row(row)
