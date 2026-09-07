"""Offline agent evaluation: fixture schema, coverage, rubric, and safety-integrity checks.

These tests do NOT invoke any model, network call, or Azure/Foundry SDK.
They validate the synthetic evaluation corpus and rubric metadata for structural
soundness, coverage completeness, and correct representation of evaluation status.

What these tests genuinely guarantee:
  - The JSONL corpus is well-formed and meets minimum coverage requirements.
  - Every corpus entry carries required provenance, version, and source metadata.
  - Safety-refusal and indeterminate entries are correctly labelled (not silently
    treated as passes).
  - Inactive safety layers are recorded as not_applicable, never as a pass or allow.
  - Active required layers with missing evidence are classified as INDETERMINATE.
  - No fabricated baseline scores or tautological pass conditions are present.
  - Synthetic example outputs are explicitly labelled as synthetic.
  - No copyrighted franchise markers appear in query text.
  - The expected card output schema (when applicable) is compatible with
    GeneratedCardModel from app.generation.
  - The seed-019 'copyrighted logo' query is deterministically blocked by the
    current HeuristicModerationService (verified offline without network).

What these tests do NOT guarantee:
  - Real model quality or safety compliance on actual inference.
  - That the Foundry-hosted agent exists or is correctly deployed.
  - That any measured quality scores are accurate.
  - Prompt-injection resistance: requires actual local/live agent execution,
    NOT tested by static fixtures.
  See docs/agent-evaluation.md § Limitations.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.generation import GeneratedCardModel, HeuristicModerationService

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path("tests/fixtures/eval/card-orchestrator-eval-seed-v1.jsonl")

REQUIRED_ROW_FIELDS = frozenset(
    {
        "id",
        "version",
        "source",
        "query",
        "category",
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

VALID_SAFETY_OUTCOMES = frozenset({"allow", "block", "indeterminate"})

VALID_SCHEMA_COMPLIANCE = frozenset({"required", "not_applicable"})

# Valid unrun evaluation statuses.  Any other string (including None, "passing",
# "pass", "inconclusive") is rejected for unrun corpora.
VALID_EVALUATION_STATUSES = frozenset({"not_evaluated", "not_applicable"})

# Valid stage identifiers used by the current pipeline:
# pre_prompt, post_text, post_art_prompt, post_image.
# foundry_hosted_guardrails is a future/proposed layer, not a pipeline stage.
KNOWN_LAYER_KEYS = frozenset(
    {"pre_prompt", "post_text", "post_art_prompt", "post_image", "foundry_hosted_guardrails"}
)

# Inactive undeployed layers must use not_applicable, not inconclusive or pass.
_INACTIVE_ABSENT_COUNTS_AS = "not_applicable"

# Franchise/copyright markers that must not appear in query text.
_COPYRIGHT_PATTERNS = [
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
]

# ---------------------------------------------------------------------------
# Evidence outcome constants (evaluator-local contract, not runtime enforcement)
# ---------------------------------------------------------------------------

NOT_APPLICABLE = "not_applicable"
INDETERMINATE = "indeterminate"
INCONCLUSIVE = "inconclusive"
ALLOW = "allow"
BLOCK = "block"


# ---------------------------------------------------------------------------
# Reusable corpus-row validator
# ---------------------------------------------------------------------------


def validate_corpus_row(row: dict[str, Any]) -> None:
    """Validate a single corpus row against the full evaluation schema.

    Raises ValueError with a descriptive message on the first violation found.
    Callers can use this for both positive (valid rows pass) and negative
    (mutated bad rows raise) tests.
    """
    row_id = row.get("id", "<missing-id>")

    # Required top-level fields
    missing = REQUIRED_ROW_FIELDS - set(row)
    if missing:
        raise ValueError(f"Row {row_id}: missing required fields: {sorted(missing)}")

    # Field types: id, version, source, query, category, provenance must be non-empty strings
    for str_field in ("id", "version", "source", "query", "category", "provenance"):
        val = row.get(str_field)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"Row {row_id}: {str_field!r} must be a non-empty string, got {val!r}")

    # expected_behavior and rubric_metadata must be dicts
    for dict_field in ("expected_behavior", "rubric_metadata"):
        val = row.get(dict_field)
        if not isinstance(val, dict):
            raise ValueError(
                f"Row {row_id}: {dict_field!r} must be a dict, got {type(val).__name__}"
            )

    # source must be "synthetic"
    if row["source"] != "synthetic":
        raise ValueError(f"Row {row_id}: source must be 'synthetic', got {row['source']!r}")

    # version must be "v1"
    if row["version"] != "v1":
        raise ValueError(f"Row {row_id}: version must be 'v1', got {row['version']!r}")

    # category must be a known value
    cat = row["category"]
    if cat not in VALID_CATEGORIES:
        raise ValueError(
            f"Row {row_id}: unknown category {cat!r}; valid: {sorted(VALID_CATEGORIES)}"
        )

    eb = row["expected_behavior"]

    # safety_outcome must be a known value
    outcome = eb.get("safety_outcome")
    if outcome not in VALID_SAFETY_OUTCOMES:
        raise ValueError(
            f"Row {row_id}: invalid safety_outcome {outcome!r}; "
            f"valid: {sorted(VALID_SAFETY_OUTCOMES)}"
        )

    # schema_compliance, if present, must be a known value
    comp = eb.get("schema_compliance")
    if comp is not None and comp not in VALID_SCHEMA_COMPLIANCE:
        raise ValueError(
            f"Row {row_id}: invalid schema_compliance {comp!r}; "
            f"valid: {sorted(VALID_SCHEMA_COMPLIANCE)}"
        )

    rm = row["rubric_metadata"]

    # evaluation_status must be in VALID_EVALUATION_STATUSES
    status = rm.get("evaluation_status")
    if status not in VALID_EVALUATION_STATUSES:
        raise ValueError(
            f"Row {row_id}: invalid evaluation_status {status!r}; "
            f"valid (unrun corpus): {sorted(VALID_EVALUATION_STATUSES)}"
        )

    # safety_layer_applicability values must be dicts with required sub-fields
    sla = rm.get("safety_layer_applicability")
    if sla is not None:
        if not isinstance(sla, dict):
            raise ValueError(f"Row {row_id}: safety_layer_applicability must be a dict")
        for layer_key, info in sla.items():
            if not isinstance(info, dict):
                raise ValueError(
                    f"Row {row_id}, layer {layer_key!r}: layer info must be a dict, "
                    f"got {type(info).__name__}"
                )
            if "currently_active" not in info:
                raise ValueError(
                    f"Row {row_id}, layer {layer_key!r}: missing required key 'currently_active'"
                )
            absent = info.get("absent_counts_as")
            if absent is None:
                raise ValueError(
                    f"Row {row_id}, layer {layer_key!r}: missing required key 'absent_counts_as'"
                )
            # Inactive layers must use not_applicable
            if info.get("currently_active") is False and info.get("required") is False:
                if absent not in (_INACTIVE_ABSENT_COUNTS_AS,):
                    raise ValueError(
                        f"Row {row_id}, layer {layer_key!r}: inactive non-required layer must use "
                        f"absent_counts_as={_INACTIVE_ABSENT_COUNTS_AS!r}, got {absent!r}"
                    )
            # No layer may use pass or allow as absent_counts_as
            if absent in ("pass", "allow"):
                raise ValueError(
                    f"Row {row_id}, layer {layer_key!r}: absent_counts_as={absent!r} violates "
                    f"the safety contract; absent safety evidence is never a pass."
                )

    # expected_card_fields must reference only real GeneratedCardModel fields
    model_fields = set(GeneratedCardModel.model_fields.keys())
    for field in eb.get("expected_card_fields", []):
        if field not in model_fields:
            raise ValueError(
                f"Row {row_id}: expected_card_fields references unknown field {field!r}; "
                f"GeneratedCardModel fields: {sorted(model_fields)}"
            )


# ---------------------------------------------------------------------------
# Evidence outcome classification helper (evaluator-local contract)
# ---------------------------------------------------------------------------


def classify_layer_outcome(layer_info: dict[str, Any]) -> str:
    """Classify a single safety layer's evaluation outcome from its rubric metadata.

    This is an OFFLINE evaluator contract; it is not enforced by the runtime app.

    Rules (in priority order):
    1. Layer info must be a dict; raises ValueError if not.
    2. Inactive (currently_active=False) and not required => NOT_APPLICABLE.
    3. Required active layer with explicitly simulated unavailable evidence
       (absent_counts_as='indeterminate') => INDETERMINATE.
    4. No evidence provided at all (absent key) => INCONCLUSIVE.
    5. Explicit block evidence => BLOCK.
    6. Explicit allow evidence => ALLOW.

    Returns one of the module-level constants: NOT_APPLICABLE, INDETERMINATE,
    INCONCLUSIVE, ALLOW, BLOCK.
    """
    if not isinstance(layer_info, dict):
        raise ValueError(
            f"classify_layer_outcome: layer_info must be a dict, got {type(layer_info).__name__}"
        )

    active = layer_info.get("currently_active")
    required = layer_info.get("required", False)
    absent_counts_as = layer_info.get("absent_counts_as")
    decision = layer_info.get("decision")  # optional explicit runtime decision

    # Inactive non-required => not applicable for current run
    if active is False and not required:
        return NOT_APPLICABLE

    # Required active layer with simulated missing evidence
    if absent_counts_as == "indeterminate":
        return INDETERMINATE

    # Explicit runtime decision present
    if decision == "block":
        return BLOCK
    if decision == "allow":
        return ALLOW

    # No runtime decision available; fall back on absent_counts_as hint
    if absent_counts_as == "block":
        return BLOCK
    if absent_counts_as == "not_applicable":
        return NOT_APPLICABLE

    return INCONCLUSIVE


# ---------------------------------------------------------------------------
# Fixture: load the corpus once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    """Load and parse all non-empty lines from the JSONL corpus."""
    assert FIXTURE_PATH.exists(), f"Corpus fixture not found: {FIXTURE_PATH}"
    rows: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Line {line_no} is not valid JSON: {exc}")
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 1. Fixture file presence and basic parse
# ---------------------------------------------------------------------------


def test_fixture_file_exists() -> None:
    assert FIXTURE_PATH.exists(), f"Expected corpus at {FIXTURE_PATH}"


def test_fixture_is_jsonl() -> None:
    """Every non-empty line must parse as a JSON object."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)  # raises on parse error
            assert isinstance(obj, dict), f"Line {line_no}: expected JSON object, got {type(obj)}"


# ---------------------------------------------------------------------------
# 2. Coverage requirements
# ---------------------------------------------------------------------------


def test_minimum_entry_count(corpus: list[dict[str, Any]]) -> None:
    assert len(corpus) >= 20, f"Corpus has {len(corpus)} entries; need >= 20"


def test_all_queries_unique(corpus: list[dict[str, Any]]) -> None:
    """All 20 queries must be distinct; duplicate queries indicate copy-paste errors."""
    queries = [row["query"] for row in corpus]
    unique = set(queries)
    assert len(unique) == len(
        queries
    ), f"Duplicate queries found: {len(queries) - len(unique)} non-unique entries"


def test_all_nine_categories_present(corpus: list[dict[str, Any]]) -> None:
    """All nine intended categories must be represented in the corpus."""
    cats = {row["category"] for row in corpus}
    missing = VALID_CATEGORIES - cats
    assert not missing, f"Missing categories: {sorted(missing)}"


def test_concept_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "concept" for r in corpus), "No 'concept' category entries"


def test_lore_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "lore" for r in corpus), "No 'lore' category entries"


def test_art_prompt_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "art-prompt" for r in corpus), "No 'art-prompt' category entries"


def test_ambiguity_format_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(
        r["category"] == "ambiguity-format" for r in corpus
    ), "No 'ambiguity-format' category entries"


def test_multi_step_coherence_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(
        r["category"] == "multi-step-coherence" for r in corpus
    ), "No 'multi-step-coherence' category entries"


def test_scope_boundary_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(
        r["category"] == "scope-boundary" for r in corpus
    ), "No 'scope-boundary' category entries"


def test_prompt_injection_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(
        r["category"] == "prompt-injection" for r in corpus
    ), "No 'prompt-injection' category entries"


def test_safety_refusal_entries_present(corpus: list[dict[str, Any]]) -> None:
    refusals = [r for r in corpus if r["category"] == "safety-refusal"]
    assert refusals, "No 'safety-refusal' entries in corpus"


def test_safety_indeterminate_entries_present(corpus: list[dict[str, Any]]) -> None:
    indet = [r for r in corpus if r["category"] == "safety-indeterminate"]
    assert indet, "No 'safety-indeterminate' entries in corpus"


def test_routing_specialists_cover_all_paths(corpus: list[dict[str, Any]]) -> None:
    """Concept, lore, and art-prompt specialists must each appear in routing_specialists."""
    all_paths: set[str] = set()
    for row in corpus:
        all_paths.update(row.get("routing_specialists", []))
    assert "concept" in all_paths, "No entry exercises the concept specialist path"
    assert "lore" in all_paths, "No entry exercises the lore specialist path"
    assert "art-prompt" in all_paths, "No entry exercises the art-prompt specialist path"


# ---------------------------------------------------------------------------
# 3. Schema integrity: full validator exercises every valid row
# ---------------------------------------------------------------------------


def test_all_required_fields_present(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        missing = REQUIRED_ROW_FIELDS - set(row)
        assert not missing, f"Row {row.get('id', '?')} missing fields: {missing}"


def test_all_ids_unique(corpus: list[dict[str, Any]]) -> None:
    ids = [row["id"] for row in corpus]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"Duplicate IDs found: {duplicates}"


def test_all_corpus_rows_pass_validator(corpus: list[dict[str, Any]]) -> None:
    """Every row in the corpus must pass validate_corpus_row without raising."""
    for row in corpus:
        validate_corpus_row(row)


def test_all_sources_synthetic(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        assert (
            row.get("source") == "synthetic"
        ), f"Row {row.get('id', '?')}: source must be 'synthetic', got {row.get('source')!r}"


def test_all_versions_match_v1(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        assert (
            row.get("version") == "v1"
        ), f"Row {row.get('id', '?')}: version must be 'v1', got {row.get('version')!r}"


def test_all_categories_valid(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        cat = row.get("category")
        assert (
            cat in VALID_CATEGORIES
        ), f"Row {row.get('id', '?')}: unknown category {cat!r}. Valid: {sorted(VALID_CATEGORIES)}"


def test_all_safety_outcomes_valid(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        outcome = row.get("expected_behavior", {}).get("safety_outcome")
        assert outcome in VALID_SAFETY_OUTCOMES, (
            f"Row {row.get('id', '?')}: invalid safety_outcome {outcome!r}. "
            f"Valid: {sorted(VALID_SAFETY_OUTCOMES)}"
        )


def test_schema_compliance_values_valid(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        comp = row.get("expected_behavior", {}).get("schema_compliance")
        if comp is not None:
            assert (
                comp in VALID_SCHEMA_COMPLIANCE
            ), f"Row {row.get('id', '?')}: invalid schema_compliance {comp!r}"


def test_provenance_field_nonempty(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        prov = row.get("provenance", "")
        assert (
            isinstance(prov, str) and prov.strip()
        ), f"Row {row.get('id', '?')}: provenance must be a non-empty string"


def test_queries_are_non_empty_strings(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        q = row.get("query", "")
        assert (
            isinstance(q, str) and q.strip()
        ), f"Row {row.get('id', '?')}: query must be a non-empty string"


def test_layer_keys_are_known_identifiers(corpus: list[dict[str, Any]]) -> None:
    """All safety_layer_applicability keys must be known stage identifiers."""
    for row in corpus:
        sla = row.get("rubric_metadata", {}).get("safety_layer_applicability", {})
        for key in sla:
            assert key in KNOWN_LAYER_KEYS, (
                f"Row {row['id']}: unknown layer key {key!r}. " f"Known: {sorted(KNOWN_LAYER_KEYS)}"
            )


# ---------------------------------------------------------------------------
# 4. Safety labelling correctness
# ---------------------------------------------------------------------------


def test_safety_refusal_rows_have_block_outcome(corpus: list[dict[str, Any]]) -> None:
    """safety-refusal rows must use safety_outcome='block', not 'allow'."""
    for row in corpus:
        if row["category"] == "safety-refusal":
            outcome = row["expected_behavior"].get("safety_outcome")
            assert outcome == "block", (
                f"Row {row['id']}: safety-refusal category must have "
                f"outcome='block', got {outcome!r}"
            )


def test_safety_indeterminate_rows_not_allow(corpus: list[dict[str, Any]]) -> None:
    """safety-indeterminate rows must use 'indeterminate', not 'allow'."""
    for row in corpus:
        if row["category"] == "safety-indeterminate":
            outcome = row["expected_behavior"].get("safety_outcome")
            assert outcome == "indeterminate", (
                f"Row {row['id']}: safety-indeterminate category must have "
                f"outcome='indeterminate', got {outcome!r}"
            )


def test_prompt_injection_rows_not_allow(corpus: list[dict[str, Any]]) -> None:
    """prompt-injection rows must use 'block', not 'allow'.

    Note: 'block' here reflects PROPOSED/FUTURE desired behavior.  Current
    HeuristicModerationService does not block these injection queries.
    The fixture marks behavior_expectation='proposed' to distinguish future
    contract from current capability.
    """
    for row in corpus:
        if row["category"] == "prompt-injection":
            outcome = row["expected_behavior"].get("safety_outcome")
            assert outcome == "block", (
                f"Row {row['id']}: prompt-injection category must have "
                f"outcome='block' (proposed future), got {outcome!r}"
            )


# ---------------------------------------------------------------------------
# 5. Inactive safety layers are not_applicable, never a pass or allow
# ---------------------------------------------------------------------------


def test_inactive_safety_layers_are_not_applicable(corpus: list[dict[str, Any]]) -> None:
    """An inactive non-required safety layer must use absent_counts_as='not_applicable'.

    Inactive/undeployed layers (currently_active=False, required=False) are NOT
    part of the current evaluation run.  They must not be recorded as inconclusive,
    pass, or allow — that would incorrectly imply evidence was gathered.
    """
    for row in corpus:
        layers = row.get("rubric_metadata", {}).get("safety_layer_applicability", {})
        for layer_name, info in layers.items():
            assert isinstance(info, dict), (
                f"Row {row['id']}, layer {layer_name!r}: layer info must be a dict, "
                f"got {type(info).__name__}"
            )
            if info.get("currently_active") is False and not info.get("required", False):
                absent = info.get("absent_counts_as")
                assert absent == _INACTIVE_ABSENT_COUNTS_AS, (
                    f"Row {row['id']}, layer {layer_name!r}: inactive non-required layer must use "
                    f"absent_counts_as={_INACTIVE_ABSENT_COUNTS_AS!r}, got {absent!r}"
                )


def test_no_layer_uses_pass_or_allow_as_absent(corpus: list[dict[str, Any]]) -> None:
    """No layer (active or inactive) may use absent_counts_as='pass' or 'allow'."""
    for row in corpus:
        layers = row.get("rubric_metadata", {}).get("safety_layer_applicability", {})
        for layer_name, info in layers.items():
            if not isinstance(info, dict):
                continue
            absent = info.get("absent_counts_as")
            assert absent not in ("pass", "allow"), (
                f"Row {row['id']}, layer {layer_name!r}: absent_counts_as={absent!r} violates "
                f"the safety contract; absent evidence is never a pass."
            )


def test_foundry_hosted_guardrails_not_marked_active(corpus: list[dict[str, Any]]) -> None:
    """The Foundry hosted guardrails layer must not be marked currently_active=True."""
    for row in corpus:
        layers = row.get("rubric_metadata", {}).get("safety_layer_applicability", {})
        fhg = layers.get("foundry_hosted_guardrails", {})
        if isinstance(fhg, dict):
            active = fhg.get("currently_active")
            assert active is not True, (
                f"Row {row['id']}: foundry_hosted_guardrails.currently_active must not be True "
                f"(the hosted agent is not deployed yet)"
            )


# ---------------------------------------------------------------------------
# 6. No fabricated baseline scores; evaluation_status within allowed set
# ---------------------------------------------------------------------------


def test_no_fabricated_baseline_scores(corpus: list[dict[str, Any]]) -> None:
    """No row may claim a measured baseline score — no deployment means no measurement."""
    for row in corpus:
        rm = row.get("rubric_metadata", {})
        assert "measured_score" not in rm, (
            f"Row {row['id']}: 'measured_score' field found — fabricated scores are not allowed. "
            f"Use evaluation_status='not_evaluated'."
        )
        assert (
            "baseline_score" not in rm
        ), f"Row {row['id']}: 'baseline_score' field found — no baseline has been measured."


def test_evaluation_status_within_valid_set(corpus: list[dict[str, Any]]) -> None:
    """evaluation_status must be one of VALID_EVALUATION_STATUSES for an unrun corpus.

    None, 'pass', 'garbage', 'passing', 'inconclusive', and any other string are
    all rejected here.  Only explicit 'not_evaluated' or 'not_applicable' are
    acceptable before a live baseline run is completed.
    """
    for row in corpus:
        status = row.get("rubric_metadata", {}).get("evaluation_status")
        assert status in VALID_EVALUATION_STATUSES, (
            f"Row {row['id']}: evaluation_status={status!r} is not valid for an unrun corpus. "
            f"Valid: {sorted(VALID_EVALUATION_STATUSES)}"
        )


# ---------------------------------------------------------------------------
# 7. Synthetic example outputs labelled correctly
# ---------------------------------------------------------------------------


def test_synthetic_example_outputs_labelled(corpus: list[dict[str, Any]]) -> None:
    """Any example_output embedded in rubric_metadata must be labelled as synthetic."""
    for row in corpus:
        rm = row.get("rubric_metadata", {})
        if "synthetic_example_output" in rm or "example_output" in rm:
            source = rm.get("example_output_source")
            assert source == "synthetic", (
                f"Row {row['id']}: example output present but example_output_source != 'synthetic' "
                f"(got {source!r}). Label must distinguish synthetic examples from model baseline."
            )


# ---------------------------------------------------------------------------
# 8. No copyrighted franchise markers in query text
# ---------------------------------------------------------------------------


def test_no_copyrighted_franchise_markers(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        text = row.get("query", "").lower()
        for pattern in _COPYRIGHT_PATTERNS:
            assert not re.search(pattern, text, re.IGNORECASE), (
                f"Row {row['id']}: copyrighted franchise marker matched pattern {pattern!r} "
                f"in query text."
            )


# ---------------------------------------------------------------------------
# 9. Schema compatibility: completed rows reference GeneratedCardModel fields
# ---------------------------------------------------------------------------


def test_expected_card_fields_match_generated_card_model(corpus: list[dict[str, Any]]) -> None:
    """Rows expecting card output must only list fields that exist on GeneratedCardModel."""
    model_fields = set(GeneratedCardModel.model_fields.keys())
    for row in corpus:
        eb = row.get("expected_behavior", {})
        expected_card_fields = eb.get("expected_card_fields", [])
        for field in expected_card_fields:
            assert field in model_fields, (
                f"Row {row['id']}: expected_card_fields references unknown field {field!r}. "
                f"GeneratedCardModel fields: {sorted(model_fields)}"
            )


def test_synthetic_example_outputs_valid_against_schema() -> None:
    """If a synthetic_example_output is embedded, it must validate against GeneratedCardModel."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            rm = row.get("rubric_metadata", {})
            example = rm.get("synthetic_example_output")
            if example and isinstance(example, dict):
                GeneratedCardModel.model_validate(example)


def test_valid_synthetic_card_passes_model() -> None:
    """A representative valid synthetic card must pass GeneratedCardModel validation."""
    valid_card = {
        "schemaVersion": 1,
        "name": "Ember Drake",
        "cardType": "creature",
        "rarity": "uncommon",
        "manaCost": 4,
        "attack": 5,
        "health": 3,
        "rulesText": "When Ember Drake enters the battlefield, deal 2 damage to any target.",
        "flavorText": "Born from the dying breath of a volcano.",
        "artBrief": "Ember drake with orange scales soaring over a volcanic crater at dusk.",
    }
    GeneratedCardModel.model_validate(valid_card)


def test_invalid_card_mutation_fails_model() -> None:
    """A card with cardType set to an invalid value must fail GeneratedCardModel validation."""
    from pydantic import ValidationError

    invalid_card = {
        "schemaVersion": 1,
        "name": "Ember Drake",
        "cardType": "villain",  # not a valid Literal
        "rarity": "uncommon",
        "manaCost": 4,
        "attack": 5,
        "health": 3,
        "rulesText": "When Ember Drake enters the battlefield, deal 2 damage to any target.",
        "flavorText": "Born from the dying breath of a volcano.",
        "artBrief": "Ember drake with orange scales soaring over a volcanic crater at dusk.",
    }
    with pytest.raises(ValidationError):
        GeneratedCardModel.model_validate(invalid_card)


# ---------------------------------------------------------------------------
# 10. Heuristic moderation: seed-019 is deterministically blocked offline
# ---------------------------------------------------------------------------


def test_seed_019_query_blocked_by_heuristic(corpus: list[dict[str, Any]]) -> None:
    """seed-v1-019 query must be blocked by the current HeuristicModerationService.

    This verifies the fixture's pre_prompt block claim against the actual
    production code — no network or model call is needed.
    """
    row = next(r for r in corpus if r["id"] == "seed-v1-019")
    svc = HeuristicModerationService("test-policy")
    decision = asyncio.run(svc.moderate_text(row["query"], stage="pre_prompt"))
    assert not decision.allowed, (
        f"seed-v1-019 query was NOT blocked by HeuristicModerationService. "
        f"Fixture claims pre_prompt block but heuristic allowed it. "
        f"Query: {row['query']!r}"
    )
    assert (
        decision.reasonCode == "copyrighted-logo"
    ), f"seed-v1-019 expected reason_code='copyrighted-logo', got {decision.reasonCode!r}"


def test_prompt_injection_queries_not_blocked_by_current_heuristic(
    corpus: list[dict[str, Any]],
) -> None:
    """Prompt-injection rows must NOT be blocked by the current heuristic.

    The injection safety_outcome='block' is PROPOSED/FUTURE behavior.  If the
    heuristic were to block these, the 'behavior_expectation=proposed' label
    would be stale and should be re-evaluated.
    """
    svc = HeuristicModerationService("test-policy")
    for row in corpus:
        if row["category"] == "prompt-injection":
            decision = asyncio.run(svc.moderate_text(row["query"], stage="pre_prompt"))
            assert decision.allowed, (
                f"Row {row['id']}: prompt-injection query is now blocked by the current heuristic "
                f"(reason: {decision.reasonCode}). Update the corpus to remove the "
                f"'behavior_expectation=proposed' label and verify the block is intentional."
            )


# ---------------------------------------------------------------------------
# 11. Evidence classification helper unit tests
# ---------------------------------------------------------------------------


def test_classify_inactive_non_required_layer_returns_not_applicable() -> None:
    layer = {"currently_active": False, "required": False, "absent_counts_as": "not_applicable"}
    assert classify_layer_outcome(layer) == NOT_APPLICABLE


def test_classify_required_active_simulated_unavailable_returns_indeterminate() -> None:
    layer = {
        "currently_active": True,
        "required": True,
        "absent_counts_as": "indeterminate",
    }
    assert classify_layer_outcome(layer) == INDETERMINATE


def test_classify_active_no_evidence_returns_inconclusive() -> None:
    """Active layer with no explicit decision and absent_counts_as not set to a terminal value."""
    layer = {"currently_active": True, "required": False, "absent_counts_as": "inconclusive"}
    assert classify_layer_outcome(layer) == INCONCLUSIVE


def test_classify_active_explicit_block_decision() -> None:
    layer = {
        "currently_active": True,
        "required": True,
        "absent_counts_as": "block",
        "decision": "block",
    }
    assert classify_layer_outcome(layer) == BLOCK


def test_classify_active_explicit_allow_decision() -> None:
    layer = {
        "currently_active": True,
        "required": True,
        "absent_counts_as": "block",
        "decision": "allow",
    }
    assert classify_layer_outcome(layer) == ALLOW


def test_classify_non_dict_raises() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        classify_layer_outcome("not-a-dict")  # type: ignore[arg-type]


def test_classify_non_dict_list_raises() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        classify_layer_outcome(["currently_active", False])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 12. Malformed-fixture rejection via validate_corpus_row (real validation path)
# ---------------------------------------------------------------------------


def test_missing_id_raises() -> None:
    bad: dict[str, Any] = {
        "version": "v1",
        "source": "synthetic",
        "query": "Some card query",
        "category": "concept",
        "expected_behavior": {
            "safety_outcome": "allow",
            "schema_compliance": "required",
        },
        "rubric_metadata": {
            "evaluation_status": "not_evaluated",
            "safety_layer_applicability": {},
        },
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="missing required fields"):
        validate_corpus_row(bad)


def test_invalid_source_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "model_generated",  # invalid
        "query": "Some card query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "allow"},
        "rubric_metadata": {"evaluation_status": "not_evaluated"},
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="source must be 'synthetic'"):
        validate_corpus_row(bad)


def test_invalid_safety_outcome_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "uncertain_maybe_possibly"},
        "rubric_metadata": {"evaluation_status": "not_evaluated"},
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="invalid safety_outcome"):
        validate_corpus_row(bad)


def test_invalid_evaluation_status_raises() -> None:
    """None, 'pass', 'passing', 'garbage' must all be rejected."""
    for bad_status in (None, "pass", "passing", "garbage", "inconclusive"):
        bad: dict[str, Any] = {
            "id": "test-001",
            "version": "v1",
            "source": "synthetic",
            "query": "Some query",
            "category": "concept",
            "expected_behavior": {"safety_outcome": "allow"},
            "rubric_metadata": {"evaluation_status": bad_status},
            "provenance": "test",
        }
        with pytest.raises(ValueError, match="invalid evaluation_status"):
            validate_corpus_row(bad)


def test_non_dict_layer_info_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "allow"},
        "rubric_metadata": {
            "evaluation_status": "not_evaluated",
            "safety_layer_applicability": {
                "pre_prompt": "should-be-a-dict",  # not a dict
            },
        },
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="layer info must be a dict"):
        validate_corpus_row(bad)


def test_layer_absent_counts_as_pass_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "allow"},
        "rubric_metadata": {
            "evaluation_status": "not_evaluated",
            "safety_layer_applicability": {
                "pre_prompt": {
                    "currently_active": False,
                    "required": False,
                    "absent_counts_as": "pass",  # violates safety contract
                },
            },
        },
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="inactive non-required layer|safety contract"):
        validate_corpus_row(bad)


def test_inactive_non_required_layer_inconclusive_raises() -> None:
    """Inactive non-required layer using 'inconclusive' instead of 'not_applicable' is invalid."""
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "allow"},
        "rubric_metadata": {
            "evaluation_status": "not_evaluated",
            "safety_layer_applicability": {
                "foundry_hosted_guardrails": {
                    "currently_active": False,
                    "required": False,
                    "absent_counts_as": "inconclusive",  # must be not_applicable
                },
            },
        },
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="inactive non-required layer"):
        validate_corpus_row(bad)


def test_unknown_card_field_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some card query",
        "category": "concept",
        "expected_behavior": {
            "safety_outcome": "allow",
            "expected_card_fields": ["name", "nonExistentField"],  # unknown field
        },
        "rubric_metadata": {"evaluation_status": "not_evaluated"},
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="unknown field"):
        validate_corpus_row(bad)


def test_non_dict_expected_behavior_raises() -> None:
    bad: dict[str, Any] = {
        "id": "test-001",
        "version": "v1",
        "source": "synthetic",
        "query": "Some query",
        "category": "concept",
        "expected_behavior": ["not", "a", "dict"],  # wrong type
        "rubric_metadata": {"evaluation_status": "not_evaluated"},
        "provenance": "test",
    }
    with pytest.raises(ValueError, match="expected_behavior.*must be a dict"):
        validate_corpus_row(bad)
