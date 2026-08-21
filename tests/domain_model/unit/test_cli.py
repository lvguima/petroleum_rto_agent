from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from petroleum_rto.domain_model import api, cli
from petroleum_rto.domain_model.adapters import HttpRequest, HttpResponse
from petroleum_rto.domain_model.evaluation import (
    load_evaluation_suite,
    load_packaged_evaluation_suite,
)
from petroleum_rto.domain_model.evidence import EvidenceStore
from petroleum_rto.domain_model.loader import load_provider_catalog
from petroleum_rto.domain_model.models import DMX_CREDENTIAL_ENV
from petroleum_rto.domain_model.prompt import PromptCompiler
from petroleum_rto.domain_model.runtime import DomainIntentRuntime
from petroleum_rto.rto.communication import (
    COMMUNICATION_SCHEMA_VERSION,
    DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
    DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
    DomainModelInvocationResult,
    DomainModelRequest,
    ProviderAttempt,
    ProviderError,
    build_intent_communication_service,
)
from petroleum_rto.rto.unified_inputs import OptimizationIntent


def _intent(*, ambiguities: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_id": "optimization-intent",
        "schema_version": "1.0.0",
        "intent_id": "cli-energy-intent",
        "objectives": [
            {
                "metric_id": "specific_furnace_fuel_energy_mj_per_t",
                "sense": "minimize",
                "priority": 1,
            }
        ],
        "decision_variables": [
            "furnace_temperature_target_k",
            "tower_top_pressure_target_pa_a",
        ],
        "constraints": [],
        "preference": {
            "method": "single-objective",
            "objective_order": ["specific_furnace_fuel_energy_mj_per_t"],
        },
        "result_request": {
            "output_kind": "steady-setpoint-vector",
            "include_alternatives": False,
            "max_candidates": 1,
        },
        "ambiguities": ambiguities or [],
    }


def _chat_response(
    request: HttpRequest,
    *,
    ambiguities: list[str] | None = None,
) -> HttpResponse:
    assert request.body is not None
    wire = json.loads(request.body)
    compiled = json.loads(wire["messages"][1]["content"])
    binding = compiled["required_response_binding"]
    egress_request = compiled["egress_request"]
    response = {
        "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
        "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
        "response_id": (
            f"response-{egress_request['turn_index']}-{egress_request['model_attempt']}"
        ),
        "request_ref": binding["request_ref"],
        "capability_manifest_ref": binding["capability_manifest_ref"],
        "outcome": "intent",
        "intent": _intent(ambiguities=ambiguities),
    }
    payload = {
        "id": "provider-request-cli",
        "model": "deepseek-v4-flash-0731",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(response, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return HttpResponse(
        status_code=200,
        headers={"x-request-id": "provider-request-cli"},
        body=json.dumps(payload, ensure_ascii=False).encode(),
    )


class _CallbackTransport:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.callback(request)


class _SuccessfulPort:
    provider_id = "dmx-cn"
    provider_version = "1.0.0"

    def __init__(self, *, ambiguities: list[str] | None = None) -> None:
        self.ambiguities = ambiguities

    def invoke(self, request: DomainModelRequest) -> DomainModelInvocationResult:
        response = {
            "schema_id": DOMAIN_MODEL_RESPONSE_SCHEMA_ID,
            "schema_version": DOMAIN_MODEL_RESPONSE_SCHEMA_VERSION,
            "response_id": f"response-{request.turn_index}-{request.model_attempt}",
            "request_ref": request.ref.as_dict(),
            "capability_manifest_ref": request.capability_manifest_ref.as_dict(),
            "outcome": "intent",
            "intent": _intent(ambiguities=self.ambiguities),
        }
        return DomainModelInvocationResult(
            schema_id=DOMAIN_MODEL_INVOCATION_RESULT_SCHEMA_ID,
            schema_version=COMMUNICATION_SCHEMA_VERSION,
            invocation_id=f"fake-invocation-{request.turn_index}-{request.model_attempt}",
            request_ref=request.ref,
            status="succeeded",
            attempts=(
                ProviderAttempt(
                    attempt_index=1,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status="succeeded",
                    provider_request_id="provider-request-cli",
                    served_model="deepseek-v4-flash-0731",
                    finish_reason="stop",
                    duration_ms=1,
                    usage=None,
                    error=None,
                ),
            ),
            response=json.dumps(response, ensure_ascii=False),
            error=None,
        )


def test_public_interpret_uses_configured_model_and_safe_inspect(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DMX_CREDENTIAL_ENV, "unit-test-only-key")

    def respond(http_request: HttpRequest) -> HttpResponse:
        assert http_request.url == "https://www.dmxapi.cn/v1/chat/completions"
        assert http_request.headers["authorization"] == "unit-test-only-key"
        return _chat_response(http_request)

    transport = _CallbackTransport(respond)
    outcome = api.interpret_intent(
        "请降低单位进料炉燃料消耗。",
        provider_id="dmx-cn",
        model_id="deepseek-v4-flash-0731",
        repo_root=repo_root,
        project_root=tmp_path,
        transport=transport,
    )

    assert outcome.status == "resolved"
    assert outcome.provider_error is None
    assert len(transport.requests) == 1
    assert outcome.evidence_manifest is not None
    summary = api.inspect_intent_session(
        outcome.evidence_manifest,
        project_root=tmp_path,
    )
    assert summary["provider_id"] == "dmx-cn"
    assert summary["model_id"] == "deepseek-v4-flash-0731"
    assert summary["approved_egress_included"] is False
    assert summary["user_message_bodies_included"] is False
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "请降低单位进料炉燃料消耗" not in serialized
    assert "unit-test-only-key" not in serialized
    assert "approved_egress" not in summary

    secret_root = tmp_path / "blocked-secret"
    secret_outcome = api.interpret_intent(
        "请处理 unit-test-only-key 并降低能耗。",
        provider_id="dmx-cn",
        model_id="deepseek-v4-flash-0731",
        repo_root=repo_root,
        project_root=secret_root,
        transport=transport,
    )
    assert secret_outcome.status == "egress_blocked"
    assert secret_outcome.provider_error is not None
    assert secret_outcome.provider_error.code == "egress-suspected-credential"
    assert secret_outcome.evidence_manifest is None
    assert len(transport.requests) == 1
    assert not (secret_root / "runs" / "domain_model" / "sessions").exists()


def test_model_discovery_does_not_expand_configured_allow_list(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DMX_CREDENTIAL_ENV, "unit-test-only-key")

    def respond(request: HttpRequest) -> HttpResponse:
        assert request.method == "GET"
        return HttpResponse(
            status_code=200,
            headers={},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": "deepseek-v4-flash-0731", "owned_by": "deepseek"},
                        {"id": "unconfigured-model", "owned_by": "unknown"},
                    ],
                }
            ).encode(),
        )

    result = api.discover_models(
        repo_root=repo_root,
        project_root=tmp_path,
        transport=_CallbackTransport(respond),
    )

    assert result["status"] == "succeeded"
    assert len(result["attempts"]) == 1
    assert result["discovery_is_authoritative"] is False
    discovered = {item["model_id"]: item for item in result["discovered_models"]}
    assert discovered["deepseek-v4-flash-0731"]["configured"] is True
    assert discovered["unconfigured-model"]["configured"] is False
    assert "unconfigured-model" not in {item["model_id"] for item in result["configured_models"]}
    artifact = EvidenceStore(tmp_path).read_discovery_report(Path(str(result["evidence_manifest"])))
    assert artifact.report_fingerprint == result["report_fingerprint"]
    assert artifact.report["status"] == "succeeded"


def test_failed_model_discovery_is_persisted_before_error_return(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DMX_CREDENTIAL_ENV, "unit-test-only-key")
    with pytest.raises(api.DmxApiError) as caught:
        api.discover_models(
            repo_root=repo_root,
            project_root=tmp_path,
            transport=_CallbackTransport(lambda _request: HttpResponse(401, {}, b"{}")),
        )

    assert caught.value.evidence_manifest is not None
    artifact = EvidenceStore(tmp_path).read_discovery_report(Path(caught.value.evidence_manifest))
    assert artifact.report["status"] == "failed"
    assert artifact.report["attempts"][0]["error"]["category"] == "authentication"


def test_continue_loads_strict_answers_and_pins_manifest_provider_model(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    provider = load_provider_catalog(repo_root).provider("dmx-cn")
    model = provider.model("deepseek-v4-flash-0731")
    compiler = PromptCompiler()
    runtime = DomainIntentRuntime(
        provider_profile=provider,
        model_profile=model,
        port=_SuccessfulPort(ambiguities=["objective-selection-ambiguous"]),
        communication_service=build_intent_communication_service(repo_root=repo_root),
        prompt_compiler=compiler,
        evidence_store=EvidenceStore(tmp_path),
    )
    first = runtime.interpret("优化一下，但我还没有决定具体目标。")
    assert first.status == "needs_clarification"
    assert first.evidence_manifest is not None
    assert first.communication_result is not None
    assert first.communication_result.clarification is not None
    question = first.communication_result.clarification.questions[0]
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "message_id": "user-2",
                "user_text": "选择能耗目标。",
                "answers": [
                    {
                        "question_id": question.question_id,
                        "values": ["specific_furnace_fuel_energy_mj_per_t"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pinned: list[tuple[str, str]] = []

    class _ContinuationRuntime:
        def continue_session(self, record: object, **kwargs: object) -> Any:
            assert record is not None
            assert kwargs["message_id"] == "user-2"
            return first

    def runtime_factory(provider_id: str, model_id: str) -> Any:
        pinned.append((provider_id, model_id))
        return _ContinuationRuntime()

    continued = api.continue_intent(
        manifest_path=first.evidence_manifest,
        answers_path=answers_path,
        repo_root=repo_root,
        project_root=tmp_path,
        runtime_factory=runtime_factory,
    )

    assert continued is first
    assert pinned == [("dmx-cn", "deepseek-v4-flash-0731")]


def test_clarification_answers_are_strict_json(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    path.write_text(
        '{"message_id":"user-2","user_text":"x","answers":[],"model":"other"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown"):
        api.load_clarification_answers(path)

    path.write_text(
        '{"message_id":"user-2","message_id":"user-3","user_text":"x","answers":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        api.load_clarification_answers(path)


def _intent_from_template(template: Any) -> OptimizationIntent:
    return OptimizationIntent.from_mapping(
        {
            "schema_id": "optimization-intent",
            "schema_version": "1.0.0",
            "intent_id": "evaluation-intent",
            "objectives": [
                {"metric_id": metric, "sense": sense, "priority": priority}
                for metric, sense, priority in template.objectives
            ],
            "decision_variables": list(template.decision_variables),
            "constraints": list(template.constraints),
            "preference": {
                "method": template.preference_method,
                "objective_order": list(template.objective_order),
            },
            "result_request": {
                "output_kind": template.output_kind,
                "include_alternatives": template.include_alternatives,
                "max_candidates": template.max_candidates,
            },
            "ambiguities": [],
        }
    )


class _GoldRuntime:
    def __init__(self, suite: Any, *, provider_failed: bool = False) -> None:
        self.suite = suite
        self.provider_failed = provider_failed
        self.by_text = {item.user_text: item for item in suite.cases}

    def interpret(self, user_text: str) -> Any:
        case = self.by_text[user_text]
        expected = case.expected
        if self.provider_failed:
            return SimpleNamespace(
                status="provider_failed",
                communication_result=None,
                provider_error=SimpleNamespace(code="synthetic-provider-failure"),
                steps=(),
                evidence_manifest=None,
                evidence_fingerprint=None,
            )
        if expected.status == "egress_blocked":
            return SimpleNamespace(
                status="egress_blocked",
                communication_result=None,
                provider_error=SimpleNamespace(code=expected.error_code),
                steps=(),
                evidence_manifest=None,
                evidence_fingerprint=None,
            )
        if expected.status == "resolved":
            intent = _intent_from_template(self.suite.intent_templates[expected.template_id])
            result = SimpleNamespace(
                status="resolved",
                resolved_intent=intent,
                candidate_intent=intent,
            )
            status = "resolved"
        elif expected.status == "needs_clarification":
            result = SimpleNamespace(
                status="needs_clarification",
                resolved_intent=None,
                candidate_intent=SimpleNamespace(ambiguities=expected.ambiguity_codes),
            )
            status = "needs_clarification"
        else:
            result = SimpleNamespace(
                status="unsupported",
                resolved_intent=None,
                candidate_intent=None,
                issues=(SimpleNamespace(code=expected.reason_code),),
            )
            status = "unsupported"
        return SimpleNamespace(
            status=status,
            communication_result=result,
            provider_error=None,
            steps=(object(),),
            evidence_manifest=None,
            evidence_fingerprint=None,
        )


def test_eval_runs_all_50_cases_three_times_across_three_upstream_families(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    suite = load_packaged_evaluation_suite(repo_root)

    def factory(provider_id: str, model_id: str) -> Any:
        assert provider_id == "dmx-cn"
        assert model_id in {
            "deepseek-v4-flash-0731",
            "gpt-5.6-sol",
            "claude-opus-4-8",
        }
        return _GoldRuntime(suite)

    report = api.evaluate_models(
        ("deepseek-v4-flash-0731", "gpt-5.6-sol", "claude-opus-4-8"),
        repo_root=repo_root,
        project_root=tmp_path,
        runtime_factory=factory,
    )

    assert report["selected_case_count"] == 50
    assert report["repetitions_per_case"] == 3
    assert report["execution_mode"] == "synthetic_injected"
    assert report["quality_evidence_eligible"] is False
    assert report["all_models_meet_quality_target"] is False
    assert report["official_suite_byte_identical"] is True
    assert report["comparison_scope"] == {
        "selected_model_count": 3,
        "required_api_style": "openai_chat",
        "eligible_model_count": 1,
        "distinct_upstream_family_count": 1,
        "minimum_model_count": 3,
        "minimum_upstream_family_count": 3,
        "served_model_snapshots_complete": False,
        "served_model_sets_disjoint": True,
        "distinct_observed_served_model_count": 0,
        "three_family_chat_coverage_met": False,
    }
    assert report["cross_model_average"] is None
    assert isinstance(report["report_fingerprint"], str)
    artifact_manifest = Path(str(report["artifact_manifest"]))
    artifact = EvidenceStore(tmp_path).read_evaluation_report(artifact_manifest)
    assert artifact.report_fingerprint == report["report_fingerprint"]
    assert artifact.manifest_fingerprint == report["artifact_manifest_fingerprint"]
    assert artifact.report["all_models_meet_quality_target"] is False
    assert len(report["models"]) == 3
    for model in report["models"]:
        assert model["expected_run_count"] == 150
        assert model["critical_classification_accuracy"] == {
            "numerator": 60,
            "denominator": 60,
            "rate": 1.0,
            "minimum_rate": 1.0,
            "passed": True,
        }
        assert model["strict_contract_pass_rate"]["minimum_rate"] == 0.98
        assert model["strict_contract_pass_rate"]["numerator"] == 144
        assert model["strict_contract_pass_rate"]["denominator"] == 144
        assert model["egress_blocked_count"] == 6
        assert model["unexpected_egress_blocked_count"] == 0
        assert model["egress_policy_correct"] is True
        assert model["unambiguous_resolved_exact_match_rate"] == {
            "numerator": 90,
            "denominator": 90,
            "rate": 1.0,
            "minimum_rate": 0.95,
            "passed": True,
        }
        assert model["served_model_snapshot_complete"] is False
        assert model["observed_served_model_ids"] == []
        assert model["metrics_passed"] is True
        assert model["quality_evidence_eligible"] is False
        assert model["quality_target_met"] is False


def test_eval_counts_provider_failures_without_cross_model_masking(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    suite = load_packaged_evaluation_suite(repo_root)

    def factory(provider_id: str, model_id: str) -> Any:
        return _GoldRuntime(suite, provider_failed=model_id == "gpt-5.6-sol")

    report = api.evaluate_models(
        ("deepseek-v4-flash-0731", "gpt-5.6-sol"),
        repo_root=repo_root,
        project_root=tmp_path,
        case_ids=("nl-001",),
        runtime_factory=factory,
    )
    models = {item["model_id"]: item for item in report["models"]}

    assert models["deepseek-v4-flash-0731"]["provider_failed_count"] == 0
    assert models["gpt-5.6-sol"]["provider_failed_count"] == 3
    assert models["gpt-5.6-sol"]["passed_run_count"] == 0
    assert models["gpt-5.6-sol"]["strict_contract_pass_rate"]["numerator"] == 0
    assert report["all_models_meet_quality_target"] is False


def test_eval_parses_the_same_packaged_bytes_that_it_hashes(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_bytes = api.packaged_evaluation_suite_bytes()
    suite = load_packaged_evaluation_suite(repo_root)
    calls = 0

    def one_read() -> bytes:
        nonlocal calls
        calls += 1
        return suite_bytes

    monkeypatch.setattr(api, "packaged_evaluation_suite_bytes", one_read)
    report = api.evaluate_models(
        ("deepseek-v4-flash-0731",),
        repo_root=repo_root,
        project_root=tmp_path,
        case_ids=("nl-001",),
        runtime_factory=lambda _provider, _model: _GoldRuntime(suite),
    )

    assert calls == 1
    assert report["official_suite_byte_identical"] is True


def test_custom_evaluation_suite_can_never_issue_quality_claim(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    raw = json.loads(
        (repo_root / "data/domain_model/gold/natural_language_intent_v1.json").read_text(
            encoding="utf-8"
        )
    )
    raw["cases"][0]["user_text"] += "（自定义副本）"
    custom_path = tmp_path / "custom-suite.json"
    custom_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    custom_suite = load_evaluation_suite(custom_path)

    report = api.evaluate_models(
        ("deepseek-v4-flash-0731",),
        repo_root=repo_root,
        project_root=tmp_path,
        suite_path=custom_path,
        runtime_factory=lambda _provider, _model: _GoldRuntime(custom_suite),
    )

    assert report["official_suite_byte_identical"] is False
    assert report["models"][0]["coverage_complete"] is False
    assert report["models"][0]["quality_target_met"] is False
    assert report["all_models_meet_quality_target"] is False


def test_public_api_does_not_accept_credentials_as_arguments() -> None:
    for function in (
        api.build_domain_intent_runtime,
        api.discover_models,
        api.interpret_intent,
        api.continue_intent,
    ):
        names = set(inspect.signature(function).parameters)
        assert not names & {"api_key", "key", "sk", "credential", "environ"}


def test_cli_parse_errors_are_structured_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["interpret"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["schema_id"] == "domain-model-cli-error"
    assert payload["status"] == "error"
    assert payload["error"]["category"] == "validation"


def test_cli_interpret_reads_file_and_emits_business_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "request.txt"
    input_path.write_text("降低能耗。", encoding="utf-8")
    called: dict[str, object] = {}

    class _Outcome:
        status = "resolved"
        provider_error = None

        @staticmethod
        def as_dict() -> dict[str, object]:
            return {"status": "resolved", "model_id": "deepseek-v4-flash-0731"}

    def fake_interpret(user_text: str, **kwargs: object) -> Any:
        called.update(kwargs)
        called["user_text"] = user_text
        return _Outcome()

    monkeypatch.setattr(cli, "interpret_intent", fake_interpret)
    assert (
        cli.main(
            [
                "interpret",
                "--model",
                "deepseek-v4-flash-0731",
                "--input",
                str(input_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "resolved"
    assert called["user_text"] == "降低能耗。"
    assert "credential" not in called


def test_cli_provider_failure_and_secret_like_exception_use_safe_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "request.txt"
    input_path.write_text("降低能耗。", encoding="utf-8")
    error = ProviderError(
        category="authentication",
        code="credential-missing-or-invalid",
        message="domain-model provider credential is unavailable or invalid",
        retryable=False,
        http_status=None,
    )

    class _Outcome:
        status = "provider_failed"
        provider_error = error

        @staticmethod
        def as_dict() -> dict[str, object]:
            return {"status": "provider_failed"}

    monkeypatch.setattr(cli, "interpret_intent", lambda *args, **kwargs: _Outcome())
    assert (
        cli.main(
            [
                "interpret",
                "--model",
                "deepseek-v4-flash-0731",
                "--input",
                str(input_path),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["category"] == "authentication"

    monkeypatch.setattr(
        cli,
        "interpret_intent",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Bearer secretvalue123")),
    )
    assert (
        cli.main(
            [
                "interpret",
                "--model",
                "deepseek-v4-flash-0731",
                "--input",
                str(input_path),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "secretvalue123" not in captured.err
    assert "strict validation" in captured.err

    for secret_like in (
        "sk_secretvalue123",
        "token=abcd1234",
        "Authorization: Basic YWJjZA==",
        '{"q9W8e7R6t5Y4u3I2o1P0":1,"q9W8e7R6t5Y4u3I2o1P0":2}',
    ):
        monkeypatch.setattr(
            cli,
            "interpret_intent",
            lambda *args, _message=secret_like, **kwargs: (_ for _ in ()).throw(
                ValueError(_message)
            ),
        )
        assert (
            cli.main(
                [
                    "interpret",
                    "--model",
                    "deepseek-v4-flash-0731",
                    "--input",
                    str(input_path),
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        assert secret_like not in captured.err
        assert "strict validation" in captured.err


def test_cli_dispatches_models_continue_inspect_and_eval_without_execution_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    class _Outcome:
        status = "needs_clarification"
        provider_error = None

        @staticmethod
        def as_dict() -> dict[str, object]:
            return {"status": "needs_clarification"}

    monkeypatch.setattr(
        cli,
        "discover_models",
        lambda **kwargs: {"provider_id": kwargs["provider_id"]},
    )
    monkeypatch.setattr(cli, "continue_intent", lambda **kwargs: _Outcome())
    monkeypatch.setattr(
        cli,
        "inspect_intent_session",
        lambda *args, **kwargs: {"approved_egress_included": False},
    )
    monkeypatch.setattr(
        cli,
        "evaluate_models",
        lambda models, **kwargs: {
            "models": list(models),
            "repetitions_per_case": 3,
            "all_models_meet_quality_target": False,
        },
    )

    assert cli.main(["models", "--provider", "dmx-cn"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_id"] == "dmx-cn"
    assert cli.main(["continue", "--session", str(manifest), "--answers", str(answers)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "needs_clarification"
    assert cli.main(["inspect", "--session", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["approved_egress_included"] is False
    assert cli.main(["eval", "--models", "deepseek-v4-flash-0731,gpt-5.6-sol"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "models": ["deepseek-v4-flash-0731", "gpt-5.6-sol"],
        "repetitions_per_case": 3,
        "all_models_meet_quality_target": False,
    }

    imported = []
    for file_path in (Path(api.__file__), Path(cli.__file__)):
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        imported.extend(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
    assert not any(
        marker in module
        for module in imported
        for marker in ("solver", "simulation", "strategy", ".cdu")
    )
