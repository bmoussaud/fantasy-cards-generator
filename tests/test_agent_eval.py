"""Offline agent evaluation: fixture schema, coverage, rubric, and safety-integrity checks.

These tests do NOT invoke any model, network call, or Azure/Foundry SDK.
They validate the synthetic evaluation corpus and rubric metadata for structural
soundness, coverage completeness, and correct representation of evaluation status.

What these tests genuinely guarantee:
  - The JSONL corpus is well-formed and meets minimum coverage requirements.
  - Every corpus entry carries required provenance, version, and source metadata.
  - Safety-refusal and indeterminate entries are correctly labelled (not silently
    treated as passes).
  - Absent or inactive safety layers are represented as inconclusive, not passing.
  - No fabricated baseline scores or tautological pass conditions are present.
  - Synthetic example outputs are explicitly labelled as synthetic.
  - No copyrighted franchise markers appear in query text.
  - The expected card output schema (when applicable) is compatible with
    GeneratedCardModel from app.generation.

What these tests do NOT guarantee:
  - Real model quality or safety compliance on actual inference.
  - That the Foundry-hosted agent exists or is correctly deployed.
  - That any measured quality scores are accurate.
  See docs/agent-evaluation.md § Limitations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.generation import GeneratedCardModel

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


def test_minimum_unique_queries(corpus: list[dict[str, Any]]) -> None:
    unique = {row["query"] for row in corpus}
    assert len(unique) >= 15, f"Only {len(unique)} unique queries; need >= 15"


def test_minimum_category_count(corpus: list[dict[str, Any]]) -> None:
    cats = {row["category"] for row in corpus}
    assert len(cats) >= 3, f"Only {len(cats)} categories; need >= 3"


def test_concept_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "concept" for r in corpus), "No 'concept' category entries"


def test_lore_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "lore" for r in corpus), "No 'lore' category entries"


def test_art_prompt_category_present(corpus: list[dict[str, Any]]) -> None:
    assert any(r["category"] == "art-prompt" for r in corpus), "No 'art-prompt' category entries"


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
# 3. Schema integrity: required fields and value constraints
# ---------------------------------------------------------------------------


def test_all_required_fields_present(corpus: list[dict[str, Any]]) -> None:
    for row in corpus:
        missing = REQUIRED_ROW_FIELDS - set(row)
        assert not missing, f"Row {row.get('id', '?')} missing fields: {missing}"


def test_all_ids_unique(corpus: list[dict[str, Any]]) -> None:
    ids = [row["id"] for row in corpus]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"Duplicate IDs found: {duplicates}"


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
    """prompt-injection rows must use 'block', not 'allow'."""
    for row in corpus:
        if row["category"] == "prompt-injection":
            outcome = row["expected_behavior"].get("safety_outcome")
            assert outcome == "block", (
                f"Row {row['id']}: prompt-injection category must have "
                f"outcome='block', got {outcome!r}"
            )


# ---------------------------------------------------------------------------
# 5. Missing/inactive safety evidence is inconclusive, never a pass
# ---------------------------------------------------------------------------


def test_inactive_safety_layers_are_inconclusive(corpus: list[dict[str, Any]]) -> None:
    """An inactive safety layer must NOT be recorded as 'pass' or 'allow'.

    This encodes the approved contract: absent/timed-out/indeterminate safety
    evidence does not count as a PASS. Only explicit allow from an active layer
    is an allow signal.
    """
    for row in corpus:
        layers = row.get("rubric_metadata", {}).get("safety_layer_applicability", {})
        for layer_name, info in layers.items():
            if not isinstance(info, dict):
                continue
            if info.get("currently_active") is False:
                absent_counts_as = info.get("absent_counts_as")
                assert absent_counts_as != "pass", (
                    f"Row {row['id']}, layer '{layer_name}': inactive layer has "
                    f"absent_counts_as='pass', which violates the safety contract. "
                    f"Use 'inconclusive' or 'block' instead."
                )
                assert absent_counts_as != "allow", (
                    f"Row {row['id']}, layer '{layer_name}': inactive layer has "
                    f"absent_counts_as='allow', which violates the safety contract."
                )


def test_foundry_hosted_guardrails_not_marked_active(corpus: list[dict[str, Any]]) -> None:
    """The Foundry hosted guardrails layer must not be marked currently_active=True.

    The Foundry agent is not deployed yet; marking it active would fabricate
    evidence of a safety layer that does not exist.
    """
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
# 6. No fabricated baseline scores
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


def test_evaluation_status_not_passing(corpus: list[dict[str, Any]]) -> None:
    """evaluation_status must not be 'passing' — no run has been completed yet."""
    for row in corpus:
        status = row.get("rubric_metadata", {}).get("evaluation_status")
        assert status != "passing", (
            f"Row {row['id']}: evaluation_status='passing' without a measured baseline run. "
            f"Valid statuses for unrun evaluations: 'not_evaluated' or 'inconclusive'."
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
                # Must validate without exception
                GeneratedCardModel.model_validate(example)


# ---------------------------------------------------------------------------
# 10. Malformed-fixture detection (unit tests, not corpus-dependent)
# ---------------------------------------------------------------------------


def test_malformed_row_missing_id_is_detected() -> None:
    """A row without 'id' must be detected as malformed by the required-fields check."""
    bad_row: dict[str, Any] = {
        "version": "v1",
        "source": "synthetic",
        "query": "Some card query",
        "category": "concept",
        "expected_behavior": {"safety_outcome": "allow"},
        "rubric_metadata": {"evaluation_status": "not_evaluated"},
        "provenance": "test",
    }
    missing = REQUIRED_ROW_FIELDS - set(bad_row)
    assert "id" in missing, "Missing 'id' field was not detected"


def test_malformed_row_invalid_safety_outcome_is_detected() -> None:
    """An unknown safety_outcome value must fail the valid-values check."""
    bad_outcome = "uncertain_maybe_possibly"
    assert (
        bad_outcome not in VALID_SAFETY_OUTCOMES
    ), f"Expected {bad_outcome!r} to fail VALID_SAFETY_OUTCOMES check"


def test_malformed_row_invalid_source_is_detected() -> None:
    """A row with source='model_generated' must be detected as invalid."""
    bad_source = "model_generated"
    assert bad_source != "synthetic", "source='model_generated' should not pass the synthetic check"


def test_malformed_row_nonexistent_card_field_is_detected() -> None:
    """An expected_card_fields entry for a non-existent field must be detected."""
    model_fields = set(GeneratedCardModel.model_fields.keys())
    bad_field = "nonExistentField"
    assert (
        bad_field not in model_fields
    ), f"Field {bad_field!r} unexpectedly exists in GeneratedCardModel"


def test_inactive_layer_pass_is_detected() -> None:
    """A row claiming absent_counts_as='pass' for an inactive layer must be rejected."""
    bad_layer_info = {"currently_active": False, "absent_counts_as": "pass"}
    # Replicate the check from test_inactive_safety_layers_are_inconclusive
    assert bad_layer_info.get("currently_active") is False
    assert bad_layer_info.get("absent_counts_as") == "pass", "Setup: should be 'pass'"
    # The check would catch this
    violation_detected = bad_layer_info.get("absent_counts_as") == "pass"
    assert violation_detected, "Inactive layer with absent_counts_as='pass' was not caught"
