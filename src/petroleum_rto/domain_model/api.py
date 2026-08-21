"""Public assembly API for provider-backed, context-free intent interpretation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

from petroleum_rto.rto.communication import (
    ClarificationAnswer,
    CommunicationResult,
    build_intent_communication_service,
)

from ._json import as_sequence, decode_json_object, identifier, sha256_bytes, strict_keys, text
from .adapters import DmxApiAdapter, DmxApiError, HttpTransport
from .credentials import LOCAL_DMX_API_CREDENTIAL_FILE
from .evaluation import (
    NaturalLanguageEvaluationCase,
    NaturalLanguageEvaluationSuite,
    load_evaluation_suite_bytes,
    packaged_evaluation_suite_bytes,
)
from .evidence import EvidenceRecord, EvidenceStore
from .loader import load_provider_catalog
from .models import DMX_PROVIDER_ID, ProviderModelInfo
from .prompt import PromptCompiler
from .runtime import DomainIntentOutcome, DomainIntentRuntime

DEFAULT_EVALUATION_SUITE: Final[Path] = Path(
    "data/domain_model/gold/natural_language_intent_v1.json"
)
EVALUATION_REPETITIONS: Final[int] = 3
_MAX_CLARIFICATION_BYTES: Final[int] = 256 * 1024


class RuntimeFactory(Protocol):
    """Injectable runtime factory used by deterministic, network-free evaluations."""

    def __call__(self, provider_id: str, model_id: str) -> DomainIntentRuntime: ...


def _workspace(project_root: Path | None) -> Path:
    if project_root is not None and not isinstance(project_root, Path):
        raise TypeError("project_root must be pathlib.Path or None")
    return (Path.cwd() if project_root is None else project_root).resolve()


def build_domain_intent_runtime(
    *,
    model_id: str,
    provider_id: str = DMX_PROVIDER_ID,
    repo_root: Path | None = None,
    project_root: Path | None = None,
    transport: HttpTransport | None = None,
) -> DomainIntentRuntime:
    """Assemble one pinned provider/model runtime without solving or simulation imports."""

    return _assemble_domain_intent_runtime(
        model_id=model_id,
        provider_id=provider_id,
        repo_root=repo_root,
        project_root=project_root,
        transport=transport,
        execution_mode=None,
    )


def _assemble_domain_intent_runtime(
    *,
    model_id: str,
    provider_id: str,
    repo_root: Path | None,
    project_root: Path | None,
    transport: HttpTransport | None,
    execution_mode: Literal["production", "validation", "synthetic_test"] | None,
) -> DomainIntentRuntime:
    """Assemble a runtime for one model explicitly listed in the provider catalog."""

    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider(provider_id)
    model = provider.model(model_id)
    compiler = PromptCompiler()
    credential_file = LOCAL_DMX_API_CREDENTIAL_FILE if transport is None else None
    adapter = DmxApiAdapter(
        provider_profile=provider,
        model_profile=model,
        transport=transport,
        credential_file=credential_file,
    )
    communication = build_intent_communication_service(repo_root=repo_root)
    resolved_execution_mode: Literal["production", "validation", "synthetic_test"] = (
        execution_mode
        if execution_mode is not None
        else ("synthetic_test" if transport is not None else "production")
    )
    return DomainIntentRuntime(
        provider_profile=provider,
        model_profile=model,
        port=adapter,
        communication_service=communication,
        prompt_compiler=compiler,
        evidence_store=EvidenceStore(_workspace(project_root)),
        execution_mode=resolved_execution_mode,
    )


def discover_models(
    *,
    provider_id: str = DMX_PROVIDER_ID,
    repo_root: Path | None = None,
    project_root: Path | None = None,
    transport: HttpTransport | None = None,
) -> dict[str, object]:
    """Discover provider models without changing the explicit configured model list."""

    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider(provider_id)
    credential_file = LOCAL_DMX_API_CREDENTIAL_FILE if transport is None else None
    discovery_adapter = DmxApiAdapter(
        provider_profile=provider,
        model_profile=provider.models[0],
        transport=transport,
        credential_file=credential_file,
    )
    invocation = discovery_adapter.discover_models()
    configured = frozenset(item.model_id for item in provider.models)
    report: dict[str, object] = {
        "schema_id": "domain-model-discovery-result",
        "schema_version": "1.2.0",
        "provider_id": provider.provider_id,
        "provider_version": provider.profile_version,
        "provider_profile_fingerprint": provider.fingerprint,
        "endpoint_path": "/models",
        "invocation_id": invocation.invocation_id,
        "status": invocation.status,
        "attempts": [item.as_dict() for item in invocation.attempts],
        "error": None if invocation.error is None else invocation.error.as_dict(),
        "discovery_is_authoritative": False,
        "configured_models": [item.as_dict() for item in provider.models],
        "discovered_models": [
            _model_discovery_summary(item, configured=configured) for item in invocation.models
        ],
    }
    artifact = EvidenceStore(_workspace(project_root)).write_discovery_report(report)
    result = {
        **report,
        "report_fingerprint": artifact.report_fingerprint,
        "evidence_manifest": str(artifact.run_dir / "manifest.json"),
        "evidence_fingerprint": artifact.manifest_fingerprint,
    }
    if invocation.status == "failed":
        assert invocation.error is not None
        raise DmxApiError(
            invocation.error,
            invocation=invocation,
            evidence_manifest=str(artifact.run_dir / "manifest.json"),
            evidence_fingerprint=artifact.manifest_fingerprint,
        )
    return result


def _model_discovery_summary(
    model: ProviderModelInfo,
    *,
    configured: frozenset[str],
) -> dict[str, object]:
    return {
        "model_id": model.id,
        "owned_by": model.owned_by,
        "configured": model.id in configured,
    }


def interpret_intent(
    user_text: str,
    *,
    model_id: str,
    provider_id: str = DMX_PROVIDER_ID,
    repo_root: Path | None = None,
    project_root: Path | None = None,
    transport: HttpTransport | None = None,
) -> DomainIntentOutcome:
    """Interpret one user message through a pinned model and the strict D0 resolver."""

    runtime = build_domain_intent_runtime(
        model_id=model_id,
        provider_id=provider_id,
        repo_root=repo_root,
        project_root=project_root,
        transport=transport,
    )
    return runtime.interpret(user_text)


def load_clarification_answers(
    path: Path,
) -> tuple[str, str, tuple[ClarificationAnswer, ...]]:
    """Load the exact continuation payload and reject duplicate or unknown fields."""

    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValueError("clarification answers must be an existing JSON file")
    raw = decode_json_object(
        resolved.read_bytes(),
        context="clarification answers",
        maximum_bytes=_MAX_CLARIFICATION_BYTES,
    )
    strict_keys(
        raw,
        required={"message_id", "user_text", "answers"},
        context="clarification answers",
    )
    answers = tuple(
        ClarificationAnswer.from_mapping(item)
        for item in as_sequence(raw["answers"], context="clarification answers list")
    )
    if not answers:
        raise ValueError("clarification answers list must not be empty")
    question_ids = tuple(item.question_id for item in answers)
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("clarification answers contain duplicate question_id values")
    return (
        identifier(raw["message_id"], context="message_id"),
        text(raw["user_text"], context="user_text"),
        answers,
    )


def continue_intent(
    *,
    manifest_path: Path,
    answers_path: Path,
    repo_root: Path | None = None,
    project_root: Path | None = None,
    transport: HttpTransport | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> DomainIntentOutcome:
    """Resume a clarification snapshot without permitting provider or model replacement."""

    store = EvidenceStore(_workspace(project_root))
    record = store.read_snapshot(Path(manifest_path))
    state = record.session_state
    if state is None:  # pragma: no cover - read_snapshot always returns state
        raise ValueError("session manifest has no resumable state")
    message_id, user_text, answers = load_clarification_answers(answers_path)
    runtime = (
        build_domain_intent_runtime(
            provider_id=state.provider_id,
            model_id=state.model_id,
            repo_root=repo_root,
            project_root=project_root,
            transport=transport,
        )
        if runtime_factory is None
        else runtime_factory(state.provider_id, state.model_id)
    )
    return runtime.continue_session(
        record,
        message_id=message_id,
        user_text=user_text,
        answers=answers,
    )


def inspect_intent_session(
    manifest_path: Path,
    *,
    project_root: Path | None = None,
) -> dict[str, object]:
    """Strictly reload a session and return a summary without prompt or user-message bodies."""

    record = EvidenceStore(_workspace(project_root)).read_snapshot(Path(manifest_path))
    return _safe_session_summary(record)


def _safe_session_summary(record: EvidenceRecord) -> dict[str, object]:
    state = record.session_state
    if state is None:
        raise ValueError("session manifest has no resumable state")
    final_result = state.steps[-1].communication_result
    return {
        "schema_id": "domain-intent-session-summary",
        "schema_version": "1.0.0",
        "manifest_path": str(record.run_dir / "manifest.json"),
        "manifest_fingerprint": record.manifest_fingerprint,
        "session_id": state.session_id,
        "snapshot_index": state.snapshot_index,
        "previous_manifest_fingerprint": state.previous_manifest_fingerprint,
        "provider_id": state.provider_id,
        "provider_version": state.provider_version,
        "provider_profile_fingerprint": state.provider_profile_fingerprint,
        "model_id": state.model_id,
        "model_profile_fingerprint": state.model_profile_fingerprint,
        "capability_manifest_ref": state.capability_manifest_ref.as_dict(),
        "communication_policy_fingerprint": state.communication_policy_fingerprint,
        "status": state.status,
        "turn_index": state.steps[-1].request.turn_index,
        "semantic_attempt_count": len(state.steps),
        "provider_error": None if state.provider_error is None else state.provider_error.as_dict(),
        "final_result": _safe_result_summary(final_result),
        "invocations": [item.as_dict() for item in record.evidence.invocations],
        "approved_egress_included": False,
        "user_message_bodies_included": False,
    }


def _safe_result_summary(result: CommunicationResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "result_ref": result.ref.as_dict(),
        "status": result.status,
        "resolved_intent": (
            None if result.resolved_intent is None else result.resolved_intent.as_dict()
        ),
        "clarification": (None if result.clarification is None else result.clarification.as_dict()),
        "issues": [item.as_dict() for item in result.issues],
    }


def evaluate_models(
    model_ids: Sequence[str],
    *,
    provider_id: str = DMX_PROVIDER_ID,
    repo_root: Path | None = None,
    project_root: Path | None = None,
    suite_path: Path | None = None,
    case_ids: Sequence[str] | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> dict[str, object]:
    """Run three independent attempts for every selected case and report each model alone."""

    normalized_models = tuple(identifier(item, context="model_id") for item in model_ids)
    if not normalized_models or len(normalized_models) != len(set(normalized_models)):
        raise ValueError("model_ids must be non-empty and unique")
    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider(provider_id)
    for model_id in normalized_models:
        provider.model(model_id)
    workspace = _workspace(project_root)
    official_suite_bytes = packaged_evaluation_suite_bytes()
    if suite_path is None:
        suite = load_evaluation_suite_bytes(official_suite_bytes)
        suite_bytes = official_suite_bytes
    else:
        resolved_suite_path = Path(suite_path).resolve()
        if not resolved_suite_path.is_file():
            raise ValueError("evaluation suite must be an existing JSON file")
        suite_bytes = resolved_suite_path.read_bytes()
        suite = load_evaluation_suite_bytes(suite_bytes)
    official_suite_byte_identical = suite_bytes == official_suite_bytes
    cases = _select_cases(suite, case_ids)
    official_suite_complete = official_suite_byte_identical and cases == suite.cases
    # Online evidence may support an optional quality claim, but never enables or
    # disables a model. Catalog membership is the only model-selection boundary.
    quality_evidence_eligible = runtime_factory is None
    execution_mode = "online_evaluation" if quality_evidence_eligible else "synthetic_injected"
    model_profiles = {model_id: provider.model(model_id) for model_id in normalized_models}
    model_reports = [
        _evaluate_one_model(
            provider_id=provider.provider_id,
            model_id=model_id,
            upstream_family=model_profiles[model_id].upstream_family,
            api_style=model_profiles[model_id].api_style,
            allowed_served_model_ids=model_profiles[model_id].allowed_served_model_ids,
            suite=suite,
            cases=cases,
            official_suite_complete=official_suite_complete,
            quality_evidence_eligible=quality_evidence_eligible,
            runtime=(
                _assemble_domain_intent_runtime(
                    provider_id=provider.provider_id,
                    model_id=model_id,
                    repo_root=repo_root,
                    project_root=workspace,
                    transport=None,
                    execution_mode="validation",
                )
                if runtime_factory is None
                else runtime_factory(provider.provider_id, model_id)
            ),
        )
        for model_id in normalized_models
    ]
    chat_profiles = tuple(
        item for item in model_profiles.values() if item.api_style == "openai_chat"
    )
    chat_families = {item.upstream_family for item in chat_profiles}
    chat_model_ids = {item.model_id for item in chat_profiles}
    chat_reports = tuple(item for item in model_reports if item.get("model_id") in chat_model_ids)
    observed_served_sets = tuple(_reported_served_model_set(item) for item in chat_reports)
    served_model_sets_disjoint = all(
        not left.intersection(right)
        for index, left in enumerate(observed_served_sets)
        for right in observed_served_sets[index + 1 :]
    )
    served_model_snapshots_complete = len(chat_reports) == len(chat_profiles) and all(
        item.get("served_model_snapshot_complete") is True for item in chat_reports
    )
    three_family_chat_coverage_met = (
        len(chat_profiles) >= 3
        and len(chat_families) >= 3
        and served_model_snapshots_complete
        and served_model_sets_disjoint
    )
    report: dict[str, object] = {
        "schema_id": "domain-model-evaluation-report",
        "schema_version": "1.3.0",
        "suite_id": suite.suite_id,
        "claim_scope": suite.claim_scope,
        "suite_sha256": sha256_bytes(suite_bytes),
        "official_suite_sha256": sha256_bytes(official_suite_bytes),
        "official_suite_byte_identical": official_suite_byte_identical,
        "execution_mode": execution_mode,
        "quality_evidence_eligible": quality_evidence_eligible,
        "provider_id": provider.provider_id,
        "repetitions_per_case": EVALUATION_REPETITIONS,
        "selected_case_count": len(cases),
        "comparison_scope": {
            "selected_model_count": len(normalized_models),
            "required_api_style": "openai_chat",
            "eligible_model_count": len(chat_profiles),
            "distinct_upstream_family_count": len(chat_families),
            "minimum_model_count": 3,
            "minimum_upstream_family_count": 3,
            "served_model_snapshots_complete": served_model_snapshots_complete,
            "served_model_sets_disjoint": served_model_sets_disjoint,
            "distinct_observed_served_model_count": len(
                {served for observed in observed_served_sets for served in observed}
            ),
            "three_family_chat_coverage_met": three_family_chat_coverage_met,
        },
        "all_models_meet_quality_target": all(
            item["quality_target_met"] is True for item in model_reports
        ),
        "models": model_reports,
        "cross_model_average": None,
    }
    artifact = EvidenceStore(workspace).write_evaluation_report(report)
    return {
        **report,
        "report_fingerprint": artifact.report_fingerprint,
        "artifact_manifest": str(artifact.run_dir / "manifest.json"),
        "artifact_manifest_fingerprint": artifact.manifest_fingerprint,
    }


def _select_cases(
    suite: NaturalLanguageEvaluationSuite,
    case_ids: Sequence[str] | None,
) -> tuple[NaturalLanguageEvaluationCase, ...]:
    if case_ids is None:
        return suite.cases
    selected_ids = tuple(identifier(item, context="case_id") for item in case_ids)
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise ValueError("case_ids must be non-empty and unique when supplied")
    by_id = {item.case_id: item for item in suite.cases}
    missing = sorted(set(selected_ids) - set(by_id))
    if missing:
        raise ValueError(f"evaluation case_ids are unknown: {missing}")
    return tuple(by_id[item] for item in selected_ids)


def _evaluate_one_model(
    *,
    provider_id: str,
    model_id: str,
    upstream_family: str,
    api_style: str,
    allowed_served_model_ids: tuple[str, ...],
    suite: NaturalLanguageEvaluationSuite,
    cases: tuple[NaturalLanguageEvaluationCase, ...],
    official_suite_complete: bool,
    quality_evidence_eligible: bool,
    runtime: DomainIntentRuntime,
) -> dict[str, object]:
    runs: list[dict[str, object]] = []
    counts = {
        "passed": 0,
        "mismatched": 0,
        "provider_failed": 0,
        "egress_blocked": 0,
        "resolved": 0,
        "needs_clarification": 0,
        "unsupported": 0,
        "failed": 0,
    }
    critical_total = 0
    critical_passed = 0
    resolved_total = 0
    resolved_exact = 0
    strict_contract_passed = 0
    strict_contract_total = 0
    unexpected_egress_blocks = 0
    maximum_semantic_attempts = 0
    served_model_snapshot_expected_runs = 0
    served_model_snapshot_observed_runs = 0
    observed_served_model_ids: set[str] = set()
    for case in cases:
        for repetition in range(1, EVALUATION_REPETITIONS + 1):
            outcome = runtime.interpret(case.user_text)
            mismatch_codes = _evaluation_mismatches(suite, case, outcome)
            passed = not mismatch_codes
            contract_valid = outcome.communication_result is not None and outcome.status in {
                "resolved",
                "needs_clarification",
                "unsupported",
            }
            semantic_attempts = len(outcome.steps)
            served_model_ids = _observed_served_model_ids(outcome)
            observed_served_model_ids.update(served_model_ids)
            if case.expected.status != "egress_blocked":
                served_model_snapshot_expected_runs += 1
                served_model_snapshot_observed_runs += int(bool(served_model_ids))
            maximum_semantic_attempts = max(maximum_semantic_attempts, semantic_attempts)
            counts["passed" if passed else "mismatched"] += 1
            counts[outcome.status] += 1
            if case.expected.status != "egress_blocked":
                strict_contract_total += 1
                strict_contract_passed += int(contract_valid)
            if outcome.status == "egress_blocked" and case.expected.status != "egress_blocked":
                unexpected_egress_blocks += 1
            if case.critical:
                critical_total += 1
                critical_passed += int(passed)
            if case.expected.status == "resolved":
                resolved_total += 1
                resolved_exact += int(passed)
            runs.append(
                {
                    "case_id": case.case_id,
                    "critical": case.critical,
                    "repetition": repetition,
                    "status": outcome.status,
                    "passed": passed,
                    "strict_contract_valid": contract_valid,
                    "mismatch_codes": list(mismatch_codes),
                    "semantic_attempt_count": semantic_attempts,
                    "served_model_ids": list(served_model_ids),
                    "evidence_manifest": (
                        None
                        if outcome.evidence_manifest is None
                        else str(outcome.evidence_manifest)
                    ),
                    "evidence_fingerprint": outcome.evidence_fingerprint,
                }
            )
    expected_runs = len(cases) * EVALUATION_REPETITIONS
    critical_metric = _threshold_metric(
        critical_passed,
        critical_total,
        minimum_rate=1.0,
    )
    contract_metric = _threshold_metric(
        strict_contract_passed,
        strict_contract_total,
        minimum_rate=0.98,
    )
    exact_metric = _threshold_metric(
        resolved_exact,
        resolved_total,
        minimum_rate=0.95,
    )
    coverage_complete = official_suite_complete
    provider_clean = counts["provider_failed"] == 0
    egress_policy_correct = unexpected_egress_blocks == 0
    semantic_bound_met = maximum_semantic_attempts <= 2
    served_model_snapshot_complete = (
        served_model_snapshot_observed_runs == served_model_snapshot_expected_runs
    )
    metrics_passed = (
        coverage_complete
        and critical_metric["passed"] is True
        and contract_metric["passed"] is True
        and exact_metric["passed"] is True
        and provider_clean
        and egress_policy_correct
        and semantic_bound_met
    )
    quality_target_met = (
        quality_evidence_eligible and metrics_passed and served_model_snapshot_complete
    )
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "upstream_family": upstream_family,
        "api_style": api_style,
        "configured_allowed_served_model_ids": list(allowed_served_model_ids),
        "observed_served_model_ids": sorted(observed_served_model_ids),
        "served_model_snapshot_expected_run_count": served_model_snapshot_expected_runs,
        "served_model_snapshot_observed_run_count": served_model_snapshot_observed_runs,
        "served_model_snapshot_complete": served_model_snapshot_complete,
        "case_count": len(cases),
        "expected_run_count": expected_runs,
        "completed_run_count": len(runs),
        "passed_run_count": counts["passed"],
        "mismatched_run_count": counts["mismatched"],
        "provider_failed_count": counts["provider_failed"],
        "egress_blocked_count": counts["egress_blocked"],
        "unexpected_egress_blocked_count": unexpected_egress_blocks,
        "status_counts": {
            key: counts[key] for key in ("resolved", "needs_clarification", "unsupported", "failed")
        },
        "all_runs_passed": counts["passed"] == expected_runs,
        "coverage_complete": coverage_complete,
        "critical_classification_accuracy": critical_metric,
        "strict_contract_pass_rate": contract_metric,
        "unambiguous_resolved_exact_match_rate": exact_metric,
        "provider_failed_zero": provider_clean,
        "egress_policy_correct": egress_policy_correct,
        "maximum_semantic_attempts_observed": maximum_semantic_attempts,
        "semantic_attempt_bound_met": semantic_bound_met,
        "quality_evidence_eligible": quality_evidence_eligible,
        "metrics_passed": metrics_passed,
        "quality_target_met": quality_target_met,
        "runs": runs,
    }


def _observed_served_model_ids(outcome: DomainIntentOutcome) -> tuple[str, ...]:
    observed: set[str] = set()
    for step in outcome.steps:
        invocation = getattr(step, "invocation", None)
        attempts = getattr(invocation, "attempts", ())
        if not isinstance(attempts, Sequence):
            continue
        for attempt in attempts:
            served_model = getattr(attempt, "served_model", None)
            if isinstance(served_model, str):
                observed.add(served_model)
    return tuple(sorted(observed))


def _reported_served_model_set(report: Mapping[str, object]) -> set[str]:
    value = report.get("observed_served_model_ids")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    return {item for item in value if isinstance(item, str)}


def _threshold_metric(
    numerator: int,
    denominator: int,
    *,
    minimum_rate: float,
) -> dict[str, object]:
    rate = None if denominator == 0 else numerator / denominator
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "minimum_rate": minimum_rate,
        "passed": rate is not None and rate >= minimum_rate,
    }


def _evaluation_mismatches(
    suite: NaturalLanguageEvaluationSuite,
    case: NaturalLanguageEvaluationCase,
    outcome: DomainIntentOutcome,
) -> tuple[str, ...]:
    if outcome.status == "provider_failed":
        return ("provider-failed",)
    if outcome.status == "egress_blocked":
        if case.expected.status != "egress_blocked":
            return ("egress-blocked",)
        actual_code = None if outcome.provider_error is None else outcome.provider_error.code
        return () if actual_code == case.expected.error_code else ("egress-error-code-mismatch",)
    if case.expected.status == "egress_blocked":
        return (f"status:{outcome.status}!=egress_blocked",)
    if outcome.communication_result is None:  # pragma: no cover - runtime invariant
        return ("communication-result-missing",)
    return suite.evaluate(case, outcome.communication_result)


__all__ = [
    "DEFAULT_EVALUATION_SUITE",
    "EVALUATION_REPETITIONS",
    "DmxApiError",
    "RuntimeFactory",
    "build_domain_intent_runtime",
    "continue_intent",
    "discover_models",
    "evaluate_models",
    "inspect_intent_session",
    "interpret_intent",
    "load_clarification_answers",
]
