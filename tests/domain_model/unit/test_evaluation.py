from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.domain_model.evaluation import (
    NaturalLanguageEvaluationSuite,
    load_evaluation_suite,
    load_packaged_evaluation_suite,
    packaged_evaluation_suite_bytes,
)


def _suite_path(repo_root: Path) -> Path:
    return repo_root / "data" / "domain_model" / "gold" / "natural_language_intent_v1.json"


def test_natural_language_suite_strictly_loads_fifty_distinct_cases(
    repo_root: Path,
) -> None:
    suite = load_evaluation_suite(_suite_path(repo_root))

    assert suite.schema_version == "1.1.0"
    assert suite.claim_scope == "synthetic-engineering-evaluation-only"
    assert len(suite.cases) == 50
    assert len({item.case_id for item in suite.cases}) == 50
    assert {item.expected.status for item in suite.cases} == {
        "resolved",
        "needs_clarification",
        "unsupported",
        "egress_blocked",
    }
    assert sum(item.critical for item in suite.cases) >= 20
    assert packaged_evaluation_suite_bytes() == _suite_path(repo_root).read_bytes()
    assert load_packaged_evaluation_suite(repo_root) == suite


def test_suite_rejects_duplicate_case_ids(repo_root: Path) -> None:
    raw = json.loads(_suite_path(repo_root).read_text(encoding="utf-8"))
    raw["cases"][1]["case_id"] = raw["cases"][0]["case_id"]

    with pytest.raises(ValueError, match="case ids must be unique"):
        NaturalLanguageEvaluationSuite.from_mapping(raw)


def test_resolved_case_must_reference_an_existing_template(repo_root: Path) -> None:
    suite = load_evaluation_suite(_suite_path(repo_root))
    first = suite.cases[0]
    invalid = replace(first.expected, template_id="missing-template")
    raw = json.loads(_suite_path(repo_root).read_text(encoding="utf-8"))
    raw["cases"][0]["expected"] = {
        "status": invalid.status,
        "template_id": invalid.template_id,
    }

    with pytest.raises(ValueError, match="unknown template"):
        NaturalLanguageEvaluationSuite.from_mapping(raw)
