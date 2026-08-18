"""Deterministic and transactional writers for the formal M6 evidence suite."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from ..core.config import canonical_fingerprint
from .basis import M6Basis, load_m6_basis
from .config import M6ValidationConfig, load_m6_validation_config
from .domain import assess_applicability
from .protection import run_protection
from .results import M6_RESULT_METADATA, M6ValidationResult

M6_ARTIFACT_PATHS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "validation_evidence": "data/gold/m6_validation_evidence_v0.1.0.json",
        "report_json": "reports/modeling/m6_validation_report_v0.1.0.json",
        "report_markdown": "reports/modeling/M6_VALIDATION_REPORT.md",
        "artifact_manifest": (
            "reports/modeling/m6_validation_manifest_v0.1.0.json"
        ),
    }
)

_PUBLISH_ORDER: Final[tuple[str, ...]] = tuple(M6_ARTIFACT_PATHS)
_CONTENT_ARTIFACTS: Final[tuple[str, ...]] = _PUBLISH_ORDER[:-1]
_BACKUP_ORDER: Final[tuple[str, ...]] = (
    "artifact_manifest",
    *_CONTENT_ARTIFACTS,
)
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_VERSION: Final[str] = "m6-validation-artifacts-v0.1.0"
_REPORT_VERSION: Final[str] = "m6-validation-report-v0.1.0"
_VALIDATION_CONFIG_PATH: Final[str] = (
    "configs/validation/m6_validation_v0.1.0.json"
)


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_object_fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(encoded)


def _fingerprinted(
    payload: Mapping[str, object],
    *,
    field: str,
) -> dict[str, object]:
    result = dict(payload)
    result[field] = canonical_fingerprint(payload)
    return result


def _json_object(path: Path, *, context: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {context}: {exc}") from exc
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) for key in decoded
    ):
        raise ValueError(f"{context} must be a JSON object")
    return cast(dict[str, object], decoded)


def _object_field(
    value: Mapping[str, object],
    name: str,
    *,
    context: str,
) -> dict[str, object]:
    selected = value.get(name)
    if not isinstance(selected, dict) or any(
        not isinstance(key, str) for key in selected
    ):
        raise TypeError(f"{context}.{name} must be a JSON object")
    return cast(dict[str, object], selected)


def _string_list_field(
    value: Mapping[str, object],
    name: str,
    *,
    context: str,
) -> tuple[str, ...]:
    selected = value.get(name)
    if not isinstance(selected, list) or any(
        not isinstance(item, str) for item in selected
    ):
        raise TypeError(f"{context}.{name} must be a string list")
    return tuple(cast(list[str], selected))


def _validate_fingerprint(
    payload: Mapping[str, object],
    *,
    field: str,
    context: str,
) -> str:
    fingerprint = _digest(payload.get(field), context=f"{context}.{field}")
    unsigned = dict(payload)
    del unsigned[field]
    if canonical_fingerprint(unsigned) != fingerprint:
        raise ValueError(f"{context} self fingerprint mismatch")
    return fingerprint


def _safe_output(repo_root: Path, artifact_name: str) -> Path:
    try:
        relative_path = M6_ARTIFACT_PATHS[artifact_name]
    except KeyError as exc:  # pragma: no cover - internal fixed map
        raise ValueError(f"unknown M6 artifact class: {artifact_name}") from exc
    root = repo_root.resolve()
    if not root.is_dir():
        raise ValueError("repo_root must be an existing directory")
    parsed = PurePosixPath(relative_path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or "." in parsed.parts
        or ".." in parsed.parts
        or "\\" in relative_path
    ):  # pragma: no cover - fixed constants are covered by construction tests
        raise ValueError("M6 artifact path is not repository-relative and canonical")
    candidate = root.joinpath(*parsed.parts)
    cursor = root
    for part in parsed.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"M6 artifact path cannot traverse a symlink: {relative_path}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:  # pragma: no cover - symlink check is the primary guard
        raise ValueError(f"M6 artifact path escapes repository: {relative_path}") from exc
    return candidate


def _load_frozen_sources(repo_root: Path) -> tuple[M6ValidationConfig, M6Basis]:
    root = repo_root.resolve()
    if not root.is_dir():
        raise ValueError("repo_root must be an existing directory")
    config = load_m6_validation_config(root / _VALIDATION_CONFIG_PATH)
    basis = load_m6_basis(root)
    return config, basis


def _expected_plan_sources(config: M6ValidationConfig) -> dict[str, tuple[str, ...]]:
    return {
        config.steady_uncertainty.plan_id: (
            "M2_steady_model_prediction",
            "M6_synthetic_validation",
        ),
        config.dynamic_uncertainty.plan_id: (
            "M3_open_loop_simulation",
            "M6_synthetic_validation",
        ),
    }


def _result_execution_layer(config_layer: str) -> str:
    return "M6_supervisory" if config_layer == "M6_supervision" else config_layer


def _validate_result_source_closure(
    result: M6ValidationResult,
    config: M6ValidationConfig,
    basis: M6Basis,
) -> None:
    if result.validation_config_version != config.validation_version:
        raise ValueError("M6 result validation version differs from frozen config")
    if result.validation_config_fingerprint != config.input_fingerprint:
        raise ValueError("M6 result validation fingerprint differs from frozen config")
    if result.control_version != config.control_version:
        raise ValueError("M6 result control version differs from frozen config")
    if result.basis.as_dict() != basis.as_dict():
        raise ValueError("M6 result basis/M5 chain differs from source-verified basis")

    scenario_specs = {item.scenario_id: item for item in config.scenarios}
    expected_scenario_ids = tuple(scenario_specs)
    if result.required_scenario_ids != expected_scenario_ids:
        raise ValueError("M6 result scenarios differ from frozen config")
    for scenario in result.scenarios:
        spec = scenario_specs[scenario.scenario_id]
        expected_domain = assess_applicability(
            config.domain_dimensions,
            spec.inputs,
            abnormal_verification=spec.abnormal_verification,
        )
        if (
            scenario.scenario_version != spec.scenario_version
            or scenario.claim_ids != spec.claim_ids
            or scenario.purpose != spec.purpose
            or scenario.execution_layer != _result_execution_layer(
                spec.execution_layer
            )
            or scenario.expected_status != spec.expected_status
            or scenario.domain.as_dict() != expected_domain.as_dict()
        ):
            raise ValueError(
                f"M6 scenario {scenario.scenario_id} metadata differs from frozen config"
            )

    plans = (config.steady_uncertainty, config.dynamic_uncertainty)
    expected_plan_ids = tuple(item.plan_id for item in plans)
    if result.required_plan_ids != expected_plan_ids:
        raise ValueError("M6 result plans differ from frozen config")
    expected_unquantified = {
        item.plan_id: item.unquantified_sources for item in plans
    }
    if dict(result.plan_unquantified_sources) != expected_unquantified:
        raise ValueError("M6 plan unquantified sources differ from frozen config")
    if dict(result.plan_source_origins) != _expected_plan_sources(config):
        raise ValueError("M6 plan source origins differ from frozen execution layers")
    for plan in plans:
        analysis = result.sensitivity_analyses[plan.plan_id]
        propagation = result.uncertainty_results[plan.plan_id]
        if analysis.input_specs != plan.inputs or analysis.output_specs != plan.outputs:
            raise ValueError(
                f"M6 sensitivity plan {plan.plan_id} differs from frozen config"
            )
        if propagation.input_intervals != plan.intervals:
            raise ValueError(
                f"M6 uncertainty plan {plan.plan_id} differs from frozen config"
            )

    expected_rules = {item.rule_id: item for item in config.protection_rules}
    expected_rule_ids = tuple(expected_rules)
    if result.required_protection_rule_ids != expected_rule_ids:
        raise ValueError("M6 protection rules differ from frozen config")
    for rule_id in expected_rule_ids:
        trace = result.protection_traces[rule_id]
        if trace.rules[0] != expected_rules[rule_id]:
            raise ValueError(
                f"M6 protection trace {rule_id} differs from frozen rule"
            )
        replayed = run_protection(
            trace.rules,
            trace.frames,
            start_time_s=trace.start_time_s,
        )
        if replayed.as_dict() != trace.as_dict():
            raise ValueError(
                f"M6 protection trace {rule_id} events differ from frame replay"
            )


def _validate_serialized_source_closure(
    evidence: Mapping[str, object],
    config: M6ValidationConfig,
    basis: M6Basis,
) -> None:
    versions = _object_field(evidence, "versions", context="M6 evidence")
    fingerprints = _object_field(
        evidence,
        "source_fingerprints",
        context="M6 evidence",
    )
    if versions.get("validation_config_version") != config.validation_version:
        raise ValueError("M6 evidence validation version differs from frozen config")
    if fingerprints.get("validation_config") != config.input_fingerprint:
        raise ValueError("M6 evidence validation fingerprint differs from frozen config")
    if versions.get("control_version") != config.control_version:
        raise ValueError("M6 evidence control version differs from frozen config")
    if evidence.get("basis") != basis.as_dict():
        raise ValueError("M6 evidence basis/M5 chain differs from source-verified basis")
    if fingerprints.get("analysis_basis") != basis.analysis_basis_fingerprint:
        raise ValueError("M6 evidence analysis basis fingerprint differs")
    if fingerprints.get("m5_pipeline") != basis.m5_pipeline_fingerprint:
        raise ValueError("M6 evidence M5 pipeline fingerprint differs")
    if fingerprints.get("m5_manifest_sha256") != basis.m5_manifest_sha256:
        raise ValueError("M6 evidence M5 manifest SHA-256 differs")

    expected_scenarios = {item.scenario_id: item for item in config.scenarios}
    required_scenarios = _string_list_field(
        evidence,
        "required_scenario_ids",
        context="M6 evidence",
    )
    if required_scenarios != tuple(expected_scenarios):
        raise ValueError("M6 evidence scenario ids differ from frozen config")
    raw_scenarios = evidence.get("scenarios")
    if not isinstance(raw_scenarios, list) or any(
        not isinstance(item, dict) for item in raw_scenarios
    ):
        raise TypeError("M6 evidence.scenarios must be a JSON object list")
    for raw in cast(list[dict[str, object]], raw_scenarios):
        scenario_id = raw.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id not in expected_scenarios:
            raise ValueError("M6 evidence contains an unknown scenario")
        spec = expected_scenarios[scenario_id]
        expected_domain = assess_applicability(
            config.domain_dimensions,
            spec.inputs,
            abnormal_verification=spec.abnormal_verification,
        )
        if (
            raw.get("scenario_version") != spec.scenario_version
            or raw.get("claim_ids") != list(spec.claim_ids)
            or raw.get("purpose") != spec.purpose
            or raw.get("execution_layer") != _result_execution_layer(
                spec.execution_layer
            )
            or raw.get("expected_status") != spec.expected_status
            or raw.get("domain") != expected_domain.as_dict()
        ):
            raise ValueError(
                f"M6 evidence scenario {scenario_id} differs from frozen config"
            )

    plans = (config.steady_uncertainty, config.dynamic_uncertainty)
    expected_plan_ids = tuple(item.plan_id for item in plans)
    if _string_list_field(
        evidence,
        "required_plan_ids",
        context="M6 evidence",
    ) != expected_plan_ids:
        raise ValueError("M6 evidence plan ids differ from frozen config")
    expected_unquantified = {
        item.plan_id: list(item.unquantified_sources) for item in plans
    }
    if _object_field(
        evidence,
        "plan_unquantified_sources",
        context="M6 evidence",
    ) != expected_unquantified:
        raise ValueError("M6 evidence unquantified sources differ from frozen config")
    expected_origins = {
        plan_id: list(origins)
        for plan_id, origins in _expected_plan_sources(config).items()
    }
    if _object_field(
        evidence,
        "plan_source_origins",
        context="M6 evidence",
    ) != expected_origins:
        raise ValueError("M6 evidence plan sources differ from frozen execution layers")
    sensitivity = _object_field(
        evidence,
        "sensitivity_analyses",
        context="M6 evidence",
    )
    uncertainty = _object_field(
        evidence,
        "uncertainty_results",
        context="M6 evidence",
    )
    for plan in plans:
        raw_analysis = sensitivity.get(plan.plan_id)
        raw_uncertainty = uncertainty.get(plan.plan_id)
        if not isinstance(raw_analysis, dict) or not isinstance(raw_uncertainty, dict):
            raise TypeError(f"M6 evidence plan {plan.plan_id} must be an object")
        if raw_analysis.get("input_specs") != [
            item.as_dict() for item in plan.inputs
        ] or raw_analysis.get("output_specs") != [
            item.as_dict() for item in plan.outputs
        ]:
            raise ValueError(
                f"M6 evidence sensitivity plan {plan.plan_id} differs from config"
            )
        if raw_uncertainty.get("input_intervals") != [
            item.as_dict() for item in plan.intervals
        ]:
            raise ValueError(
                f"M6 evidence uncertainty plan {plan.plan_id} differs from config"
            )

    expected_rules = {item.rule_id: item for item in config.protection_rules}
    required_rules = _string_list_field(
        evidence,
        "required_protection_rule_ids",
        context="M6 evidence",
    )
    if required_rules != tuple(expected_rules):
        raise ValueError("M6 evidence rule ids differ from frozen config")
    traces = _object_field(evidence, "protection_traces", context="M6 evidence")
    if set(traces) != set(expected_rules):
        raise ValueError("M6 evidence protection traces do not cover frozen rules")
    expected_tracking: dict[str, str] = {}
    for rule_id, rule in expected_rules.items():
        raw_trace = traces[rule_id]
        if not isinstance(raw_trace, dict):
            raise TypeError(f"M6 evidence protection trace {rule_id} must be an object")
        rules = raw_trace.get("rules")
        if rules != [rule.as_dict()]:
            raise ValueError(
                f"M6 evidence protection trace {rule_id} differs from frozen rule"
            )
        events = raw_trace.get("events")
        if not isinstance(events, list) or not any(
            isinstance(event, dict)
            and event.get("rule_id") == rule_id
            and event.get("event_kind") == "triggered"
            for event in events
        ):
            raise ValueError(
                f"M6 evidence protection trace {rule_id} lacks a triggered event"
            )
        expected_tracking.update(
            {
                f"{rule_id}.{loop_id}": loop_id
                for loop_id in rule.action.manual_tracking_loop_ids
            }
        )
    tracking = _object_field(evidence, "controller_tracking", context="M6 evidence")
    if set(tracking) != set(expected_tracking):
        raise ValueError("M6 evidence controller tracking coverage is incomplete")
    for evidence_id, raw_tracking in tracking.items():
        if (
            not isinstance(raw_tracking, dict)
            or raw_tracking.get("passed") is not True
            or raw_tracking.get("loop_id") != expected_tracking[evidence_id]
        ):
            raise ValueError(f"M6 controller tracking {evidence_id} did not pass")
        tolerance = raw_tracking.get("tolerance")
        final_error = raw_tracking.get("final_tracking_error")
        return_jump = raw_tracking.get("automatic_return_jump")
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or isinstance(final_error, bool)
            or not isinstance(final_error, (int, float))
            or isinstance(return_jump, bool)
            or not isinstance(return_jump, (int, float))
            or float(tolerance) <= 0.0
            or float(final_error) < 0.0
            or float(return_jump) < 0.0
            or float(final_error) > float(tolerance)
            or float(return_jump) > float(tolerance)
        ):
            raise ValueError(
                f"M6 controller tracking {evidence_id} errors exceed tolerance"
            )
        tracking_fingerprint = _digest(
            raw_tracking.get("evidence_fingerprint"),
            context=f"M6 controller tracking {evidence_id}.evidence_fingerprint",
        )
        unsigned_tracking = dict(raw_tracking)
        del unsigned_tracking["evidence_fingerprint"]
        if _plain_object_fingerprint(unsigned_tracking) != tracking_fingerprint:
            raise ValueError(
                f"M6 controller tracking {evidence_id} self fingerprint mismatch"
            )
    expected_protection_fingerprint = canonical_fingerprint(
        {
            "required_rule_ids": list(required_rules),
            "traces": {
                rule_id: traces[rule_id] for rule_id in required_rules
            },
            "controller_tracking": tracking,
        }
    )
    if (
        fingerprints.get("protection_tracking_evidence")
        != expected_protection_fingerprint
    ):
        raise ValueError("M6 protection/tracking source fingerprint differs")


def _limitation_payloads(result: M6ValidationResult) -> list[dict[str, object]]:
    limited_ids = [
        scenario.scenario_id
        for scenario in result.scenarios
        if scenario.scenario_status == "limited"
    ]
    rejected_ids = [
        scenario.scenario_id
        for scenario in result.scenarios
        if scenario.scenario_status == "rejected"
    ]
    limitations: list[dict[str, object]] = [
        {
            "limitation_id": "engineering_validation_only",
            "statement": (
                "全部模型数值均为合成工程验证证据，不构成现场动态精度或跨原油验证。"
            ),
        },
        {
            "limitation_id": "local_first_order_envelope",
            "statement": (
                "灵敏度和不确定度仅为固定参考点的一阶工程包络，不是概率置信区间。"
            ),
        },
        {
            "limitation_id": "single_case_m5_basis",
            "statement": (
                "有效基准继承M5单案例、弱时间对齐和六伪组分工程初值限制。"
            ),
        },
        {
            "limitation_id": "synthetic_protection_not_sis",
            "statement": (
                "保护状态机只验证模型行为，不代表现场SIS设定、硬件或安全认证。"
            ),
        },
        {
            "limitation_id": "mixed_source_boundary",
            "statement": (
                "现场观测目录仅作来源证据；M2、M3、M4和M6结果仍为模型预测或合成仿真。"
            ),
        },
    ]
    if limited_ids:
        limitations.append(
            {
                "limitation_id": "limited_scenarios",
                "statement": "以下场景只形成受限工程验证结论。",
                "scenario_ids": limited_ids,
            }
        )
    if rejected_ids:
        limitations.append(
            {
                "limitation_id": "structurally_rejected_scenarios",
                "statement": "以下请求在求解前按模型结构明确拒绝。",
                "scenario_ids": rejected_ids,
            }
        )
    return limitations


def _report_payload(
    result: M6ValidationResult,
    *,
    evidence_sha256: str,
    evidence_bytes: int,
) -> dict[str, object]:
    status_counts = {
        status: sum(
            scenario.scenario_status == status for scenario in result.scenarios
        )
        for status in ("passed", "limited", "rejected", "failed")
    }
    verification_counts = {
        outcome: sum(
            scenario.verification_outcome == outcome for scenario in result.scenarios
        )
        for outcome in ("passed", "failed")
    }
    plans = {
        plan_id: {
            "sensitivity": result.sensitivity_analyses[plan_id].as_dict(),
            "uncertainty": result.uncertainty_results[plan_id].as_dict(),
            "unquantified_sources": list(
                result.plan_unquantified_sources[plan_id]
            ),
            "source_origins": list(result.plan_source_origins[plan_id]),
        }
        for plan_id in result.required_plan_ids
    }
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M6_validation_review",
        "report_version": _REPORT_VERSION,
        "status": result.status,
        "completion_passed": result.completion_passed,
        "evidence": {
            "path": M6_ARTIFACT_PATHS["validation_evidence"],
            "sha256": evidence_sha256,
            "bytes": evidence_bytes,
            "result_fingerprint": result.result_fingerprint,
        },
        "versions": dict(result.versions),
        "source_fingerprints": dict(result.source_fingerprints),
        "required_scenario_ids": list(result.required_scenario_ids),
        "scenario_summary": {
            "total": len(result.scenarios),
            "status_counts": status_counts,
            "verification_counts": verification_counts,
        },
        "scenarios": [scenario.as_dict() for scenario in result.scenarios],
        "required_plan_ids": list(result.required_plan_ids),
        "plans": plans,
        "required_protection_rule_ids": list(
            result.required_protection_rule_ids
        ),
        "protection_traces": {
            rule_id: result.protection_traces[rule_id].as_dict()
            for rule_id in result.required_protection_rule_ids
        },
        "controller_tracking": {
            evidence_id: evidence.as_dict()
            for evidence_id, evidence in result.controller_tracking.items()
        },
        "completion_checks": dict(result.completion_checks),
        "source_composition": dict(result.source_composition),
        "metadata": dict(result.metadata),
        "limitations": _limitation_payloads(result),
    }
    return _fingerprinted(payload, field="report_fingerprint")


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _report_markdown(
    result: M6ValidationResult,
    *,
    evidence_sha256: str,
    report_json_sha256: str,
) -> str:
    lines = [
        "# M6 工程验证审计报告",
        "",
        "> 本报告只声明合成工程验证通过，不构成现场动态精度、跨原油能力或SIS验证。",
        "",
        "## 发布结论",
        "",
        f"- 验证状态：`{result.status}`",
        f"- 完成门禁：`{str(result.completion_passed).lower()}`",
        f"- 结果指纹：`{result.result_fingerprint}`",
        f"- 声明范围：`{result.metadata['claim_scope']}`",
        f"- 数据来源：`{result.metadata['data_origin']}`",
        "",
        "## 完成门禁",
        "",
        "| 门禁 | 结果 |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| `{_markdown_cell(name)}` | {'通过' if passed else '失败'} |"
        for name, passed in result.completion_checks.items()
    )
    lines.extend(
        [
            "",
            "## 场景矩阵",
            "",
            "| 场景 | 执行层 | 实际/预期 | 验证结论 | 求解器 | 适用域 | 保护事件 |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for scenario in result.scenarios:
        summary = scenario.protection_trace_summary
        event_count = 0 if summary is None else summary["event_count"]
        lines.append(
            "| "
            f"`{_markdown_cell(scenario.scenario_id)}` | "
            f"`{_markdown_cell(scenario.execution_layer)}` | "
            f"`{scenario.scenario_status}` / `{scenario.expected_status}` | "
            f"`{scenario.verification_outcome}` | "
            f"{'已调用' if scenario.solver_called else '未调用'} | "
            f"`{scenario.domain.status}` | {event_count} |"
        )
    lines.extend(
        [
            "",
            "## 灵敏度与不确定度",
            "",
            "| Plan | 输入 | 输出 | 灵敏度 | 区间语义 | 来源 | 未量化来源 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for plan_id in result.required_plan_ids:
        analysis = result.sensitivity_analyses[plan_id]
        uncertainty = result.uncertainty_results[plan_id]
        input_ids = ", ".join(item.input_id for item in analysis.input_specs)
        output_ids = ", ".join(item.output_id for item in analysis.output_specs)
        sources = ", ".join(result.plan_source_origins[plan_id])
        unquantified = ", ".join(result.plan_unquantified_sources[plan_id])
        lines.append(
            f"| `{_markdown_cell(plan_id)}` | `{_markdown_cell(input_ids)}` | "
            f"`{_markdown_cell(output_ids)}` | `{analysis.status}` | "
            f"`{uncertainty.interval_semantics}` | "
            f"`{_markdown_cell(sources)}` | `{_markdown_cell(unquantified)}` |"
        )
    lines.extend(
        [
            "",
            "## 保护事件与控制器跟踪",
            "",
            "| 规则 | 帧数 | 事件数 | 触发时刻 | 跟踪证据 |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    for rule_id in result.required_protection_rule_ids:
        trace = result.protection_traces[rule_id]
        trigger_times = ", ".join(
            str(event.time_s)
            for event in trace.events
            if event.event_kind == "triggered"
        )
        tracking_ids = ", ".join(
            evidence_id
            for evidence_id in result.controller_tracking
            if evidence_id.startswith(f"{rule_id}.")
        )
        lines.append(
            f"| `{_markdown_cell(rule_id)}` | {len(trace.frames)} | "
            f"{len(trace.events)} | `{_markdown_cell(trigger_times)}` | "
            f"`{_markdown_cell(tracking_ids)}` |"
        )
    lines.extend(
        [
            "",
            "## 来源边界",
            "",
            "| 来源 | 分类 |",
            "| --- | --- |",
        ]
    )
    lines.extend(
        f"| `{_markdown_cell(source)}` | `{_markdown_cell(classification)}` |"
        for source, classification in result.source_composition.items()
    )
    lines.extend(["", "## 限制与禁止声明", ""])
    for limitation in _limitation_payloads(result):
        statement = limitation["statement"]
        scenario_ids = limitation.get("scenario_ids")
        suffix = ""
        if isinstance(scenario_ids, list):
            suffix = "（" + "、".join(f"`{item}`" for item in scenario_ids) + "）"
        lines.append(
            f"- `{limitation['limitation_id']}`：{statement}{suffix}"
        )
    lines.extend(
        [
            "",
            "## 文件交叉引用",
            "",
            f"- 完整证据 SHA-256：`{evidence_sha256}`",
            f"- 机器报告 SHA-256：`{report_json_sha256}`",
            "- 四文件套件由manifest最后发布；manifest本身不记录当前时间。",
            "",
        ]
    )
    return "\n".join(lines)


def _manifest_payload(
    result: M6ValidationResult,
    *,
    content_bytes: Mapping[str, bytes],
) -> dict[str, object]:
    artifacts = {
        name: {
            "path": M6_ARTIFACT_PATHS[name],
            "sha256": _sha256(content_bytes[name]),
            "bytes": len(content_bytes[name]),
        }
        for name in _CONTENT_ARTIFACTS
    }
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M6_validation_artifact_suite_manifest",
        "manifest_version": _ARTIFACT_VERSION,
        "manifest_path": M6_ARTIFACT_PATHS["artifact_manifest"],
        "status": "valid",
        "validation_status": result.status,
        "completion_passed": result.completion_passed,
        "result_fingerprint": result.result_fingerprint,
        "claim_scope": result.metadata["claim_scope"],
        "artifacts": artifacts,
        "metadata": dict(result.metadata),
    }
    return _fingerprinted(payload, field="manifest_fingerprint")


def _artifact_bytes(result: M6ValidationResult) -> dict[str, bytes]:
    evidence = _json_bytes(result.as_dict())
    evidence_sha = _sha256(evidence)
    report_payload = _report_payload(
        result,
        evidence_sha256=evidence_sha,
        evidence_bytes=len(evidence),
    )
    report_json = _json_bytes(report_payload)
    report_markdown = _report_markdown(
        result,
        evidence_sha256=evidence_sha,
        report_json_sha256=_sha256(report_json),
    ).encode("utf-8")
    content = {
        "validation_evidence": evidence,
        "report_json": report_json,
        "report_markdown": report_markdown,
    }
    content["artifact_manifest"] = _json_bytes(
        _manifest_payload(result, content_bytes=content)
    )
    return content


def _validate_bundle(paths: Mapping[str, Path]) -> tuple[str, str]:
    if set(paths) != set(_PUBLISH_ORDER):
        raise ValueError("M6 artifact bundle does not contain the fixed four files")
    for name in _PUBLISH_ORDER:
        if not paths[name].is_file():
            raise ValueError(f"M6 artifact bundle omitted {name}")

    evidence = _json_object(paths["validation_evidence"], context="M6 evidence")
    report = _json_object(paths["report_json"], context="M6 JSON report")
    manifest = _json_object(paths["artifact_manifest"], context="M6 manifest")
    evidence_fingerprint = _validate_fingerprint(
        evidence,
        field="result_fingerprint",
        context="M6 evidence",
    )
    report_fingerprint = _validate_fingerprint(
        report,
        field="report_fingerprint",
        context="M6 JSON report",
    )
    manifest_fingerprint = _validate_fingerprint(
        manifest,
        field="manifest_fingerprint",
        context="M6 manifest",
    )
    del report_fingerprint  # validated content identity is retained by its file hash

    if (
        evidence.get("status") != "success"
        or evidence.get("completion_passed") is not True
        or evidence.get("metadata") != dict(M6_RESULT_METADATA)
    ):
        raise ValueError("M6 evidence is not a complete successful validation result")
    if (
        report.get("artifact_type") != "M6_validation_review"
        or report.get("status") != "success"
        or report.get("completion_passed") is not True
        or report.get("metadata") != dict(M6_RESULT_METADATA)
    ):
        raise ValueError("M6 JSON report differs from the successful claim contract")
    if (
        manifest.get("artifact_type")
        != "M6_validation_artifact_suite_manifest"
        or manifest.get("status") != "valid"
        or manifest.get("validation_status") != "success"
        or manifest.get("completion_passed") is not True
        or manifest.get("manifest_path")
        != M6_ARTIFACT_PATHS["artifact_manifest"]
        or manifest.get("metadata") != dict(M6_RESULT_METADATA)
    ):
        raise ValueError("M6 manifest differs from the valid suite contract")
    if manifest.get("result_fingerprint") != evidence_fingerprint:
        raise ValueError("M6 manifest and evidence result fingerprints differ")

    report_evidence = _object_field(report, "evidence", context="M6 JSON report")
    evidence_data = paths["validation_evidence"].read_bytes()
    if (
        report_evidence.get("path")
        != M6_ARTIFACT_PATHS["validation_evidence"]
        or report_evidence.get("result_fingerprint") != evidence_fingerprint
        or report_evidence.get("sha256") != _sha256(evidence_data)
        or report_evidence.get("bytes") != len(evidence_data)
    ):
        raise ValueError("M6 JSON report evidence reference mismatch")

    required_scenarios = _string_list_field(
        evidence,
        "required_scenario_ids",
        context="M6 evidence",
    )
    required_plans = _string_list_field(
        evidence,
        "required_plan_ids",
        context="M6 evidence",
    )
    if (
        not required_scenarios
        or len(set(required_scenarios)) != len(required_scenarios)
        or len(required_plans) != 2
        or len(set(required_plans)) != 2
    ):
        raise ValueError("M6 evidence scenario/plan coverage is incomplete")
    evidence_scenarios = evidence.get("scenarios")
    if not isinstance(evidence_scenarios, list) or any(
        not isinstance(item, dict) for item in evidence_scenarios
    ):
        raise TypeError("M6 evidence.scenarios must be a JSON object list")
    scenario_ids = tuple(
        item.get("scenario_id")
        for item in cast(list[dict[str, object]], evidence_scenarios)
    )
    if scenario_ids != required_scenarios:
        raise ValueError("M6 evidence scenarios differ from required coverage")
    evidence_sensitivity = _object_field(
        evidence,
        "sensitivity_analyses",
        context="M6 evidence",
    )
    evidence_uncertainty = _object_field(
        evidence,
        "uncertainty_results",
        context="M6 evidence",
    )
    evidence_unquantified = _object_field(
        evidence,
        "plan_unquantified_sources",
        context="M6 evidence",
    )
    evidence_plan_sources = _object_field(
        evidence,
        "plan_source_origins",
        context="M6 evidence",
    )
    if set(evidence_sensitivity) != set(required_plans) or set(
        evidence_uncertainty
    ) != set(required_plans) or set(evidence_unquantified) != set(
        required_plans
    ) or set(evidence_plan_sources) != set(required_plans):
        raise ValueError("M6 evidence does not contain both required plans")
    if _string_list_field(
        report,
        "required_scenario_ids",
        context="M6 JSON report",
    ) != required_scenarios or _string_list_field(
        report,
        "required_plan_ids",
        context="M6 JSON report",
    ) != required_plans:
        raise ValueError("M6 JSON report coverage differs from the evidence")
    report_scenarios = report.get("scenarios")
    if not isinstance(report_scenarios, list) or tuple(
        item.get("scenario_id") if isinstance(item, dict) else None
        for item in report_scenarios
    ) != required_scenarios:
        raise ValueError("M6 JSON report scenario records differ from the evidence")
    report_plans = _object_field(report, "plans", context="M6 JSON report")
    if set(report_plans) != set(required_plans):
        raise ValueError("M6 JSON report does not contain both required plans")
    for plan_id in required_plans:
        plan = _object_field(
            report_plans,
            plan_id,
            context="M6 JSON report.plans",
        )
        if (
            plan.get("sensitivity") != evidence_sensitivity[plan_id]
            or plan.get("uncertainty") != evidence_uncertainty[plan_id]
            or plan.get("unquantified_sources") != evidence_unquantified[plan_id]
            or plan.get("source_origins") != evidence_plan_sources[plan_id]
        ):
            raise ValueError(f"M6 JSON report plan {plan_id} differs from evidence")

    required_rules = _string_list_field(
        evidence,
        "required_protection_rule_ids",
        context="M6 evidence",
    )
    if not required_rules or len(set(required_rules)) != len(required_rules):
        raise ValueError("M6 evidence protection rule coverage is incomplete")
    evidence_traces = _object_field(
        evidence,
        "protection_traces",
        context="M6 evidence",
    )
    evidence_tracking = _object_field(
        evidence,
        "controller_tracking",
        context="M6 evidence",
    )
    if set(evidence_traces) != set(required_rules):
        raise ValueError("M6 evidence protection traces differ from required rules")
    if _string_list_field(
        report,
        "required_protection_rule_ids",
        context="M6 JSON report",
    ) != required_rules or report.get("protection_traces") != evidence_traces:
        raise ValueError("M6 JSON report protection traces differ from evidence")
    if report.get("controller_tracking") != evidence_tracking:
        raise ValueError("M6 JSON report controller tracking differs from evidence")
    limitations = report.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
        not isinstance(item, dict)
        or not isinstance(item.get("limitation_id"), str)
        or not isinstance(item.get("statement"), str)
        for item in limitations
    ):
        raise ValueError("M6 JSON report omitted structured limitations")
    if report.get("completion_checks") != evidence.get("completion_checks"):
        raise ValueError("M6 JSON report completion gates differ from the evidence")

    manifest_artifacts = _object_field(manifest, "artifacts", context="M6 manifest")
    if set(manifest_artifacts) != set(_CONTENT_ARTIFACTS):
        raise ValueError("M6 manifest artifact set differs from the fixed suite")
    for name in _CONTENT_ARTIFACTS:
        entry = _object_field(
            manifest_artifacts,
            name,
            context="M6 manifest.artifacts",
        )
        data = paths[name].read_bytes()
        if (
            entry.get("path") != M6_ARTIFACT_PATHS[name]
            or entry.get("sha256") != _sha256(data)
            or entry.get("bytes") != len(data)
        ):
            raise ValueError(f"M6 manifest hash/bytes mismatch for {name}")

    try:
        markdown = paths["report_markdown"].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read M6 Markdown report: {exc}") from exc
    if evidence_fingerprint not in markdown or "不构成现场动态精度" not in markdown:
        raise ValueError("M6 Markdown report omitted result identity or claim limitation")
    if any(f"`{scenario_id}`" not in markdown for scenario_id in required_scenarios):
        raise ValueError("M6 Markdown report omitted a required scenario")
    if any(f"`{plan_id}`" not in markdown for plan_id in required_plans):
        raise ValueError("M6 Markdown report omitted a required plan")
    if any(f"`{rule_id}`" not in markdown for rule_id in required_rules):
        raise ValueError("M6 Markdown report omitted a required protection rule")
    return evidence_fingerprint, manifest_fingerprint


def _stage_bundle(
    targets: Mapping[str, Path],
    content: Mapping[str, bytes],
    *,
    token: str,
) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    try:
        for name in _PUBLISH_ORDER:
            target = targets[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name(f".{target.name}.{token}.stage")
            staged[name] = stage
            stage.write_bytes(content[name])
            reread = stage.read_bytes()
            if len(reread) != len(content[name]) or _sha256(reread) != _sha256(
                content[name]
            ):
                raise OSError(f"staged M6 artifact verification failed for {name}")
    except Exception:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    return staged


def _validate_staged_bundle(staged: Mapping[str, Path]) -> None:
    _validate_bundle(staged)


def _publish_staged_bundle(
    targets: Mapping[str, Path],
    staged: Mapping[str, Path],
    *,
    token: str,
) -> None:
    backups: dict[str, Path] = {}
    published: list[str] = []
    publication_succeeded = False
    try:
        # Remove the old manifest first, so no valid marker can point at a
        # transient mixture.  The new manifest remains the final publication.
        for name in _BACKUP_ORDER:
            target = targets[name]
            if target.exists():
                if not target.is_file():
                    raise ValueError(f"M6 artifact target is not a file: {target}")
                backup = target.with_name(f".{target.name}.{token}.backup")
                target.replace(backup)
                backups[name] = backup
        for name in _PUBLISH_ORDER:
            staged[name].replace(targets[name])
            published.append(name)
        _validate_bundle(targets)
        publication_succeeded = True
    except Exception as publication_error:
        rollback_errors: list[str] = []
        for name in reversed(published):
            try:
                targets[name].unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem failure path
                rollback_errors.append(f"remove {name}: {exc}")
        for name in reversed(_BACKUP_ORDER):
            prior_backup = backups.get(name)
            if prior_backup is not None and prior_backup.exists():
                try:
                    prior_backup.replace(targets[name])
                except OSError as exc:  # pragma: no cover - filesystem failure path
                    rollback_errors.append(f"restore {name}: {exc}")
        if rollback_errors:
            raise RuntimeError(
                "M6 artifact rollback was incomplete; backup files were preserved: "
                + "; ".join(rollback_errors)
            ) from publication_error
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        if publication_succeeded:
            for path in backups.values():
                path.unlink(missing_ok=True)


@dataclass(frozen=True)
class M6ArtifactManifest:
    """Immutable paths, sizes and hashes for one valid published M6 suite."""

    paths: Mapping[str, str]
    sha256: Mapping[str, str]
    size_bytes: Mapping[str, int]
    result_fingerprint: str
    manifest_fingerprint: str

    def __post_init__(self) -> None:
        paths = dict(self.paths)
        if paths != dict(M6_ARTIFACT_PATHS):
            raise ValueError("M6 artifact paths differ from the fixed formal suite")
        hashes = dict(self.sha256)
        sizes = dict(self.size_bytes)
        if set(hashes) != set(_PUBLISH_ORDER) or set(sizes) != set(_PUBLISH_ORDER):
            raise ValueError("M6 artifact hashes/sizes must cover the fixed suite")
        for name in _PUBLISH_ORDER:
            _digest(hashes[name], context=f"M6 artifact {name} SHA-256")
            size = sizes[name]
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ValueError(f"M6 artifact {name} byte size must be positive")
        _digest(self.result_fingerprint, context="M6 artifact result_fingerprint")
        _digest(self.manifest_fingerprint, context="M6 manifest_fingerprint")
        object.__setattr__(
            self,
            "paths",
            MappingProxyType({name: paths[name] for name in _PUBLISH_ORDER}),
        )
        object.__setattr__(
            self,
            "sha256",
            MappingProxyType({name: hashes[name] for name in _PUBLISH_ORDER}),
        )
        object.__setattr__(
            self,
            "size_bytes",
            MappingProxyType({name: sizes[name] for name in _PUBLISH_ORDER}),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "paths": dict(self.paths),
            "sha256": dict(self.sha256),
            "size_bytes": dict(self.size_bytes),
            "result_fingerprint": self.result_fingerprint,
            "manifest_fingerprint": self.manifest_fingerprint,
        }


def verify_m6_artifacts(
    repo_root: Path,
    *,
    expected_result_fingerprint: str | None = None,
) -> M6ArtifactManifest:
    """Verify and return the fixed formal M6 artifact suite without modifying it."""

    if expected_result_fingerprint is not None:
        _digest(
            expected_result_fingerprint,
            context="expected_result_fingerprint",
        )
    targets = {
        name: _safe_output(repo_root, name)
        for name in _PUBLISH_ORDER
    }
    result_fingerprint, manifest_fingerprint = _validate_bundle(targets)
    config, basis = _load_frozen_sources(repo_root)
    evidence = _json_object(targets["validation_evidence"], context="M6 evidence")
    _validate_serialized_source_closure(evidence, config, basis)
    if (
        expected_result_fingerprint is not None
        and result_fingerprint != expected_result_fingerprint
    ):
        raise ValueError("published M6 result fingerprint differs from expected")
    return M6ArtifactManifest(
        paths=M6_ARTIFACT_PATHS,
        sha256={name: _sha256(targets[name].read_bytes()) for name in _PUBLISH_ORDER},
        size_bytes={name: targets[name].stat().st_size for name in _PUBLISH_ORDER},
        result_fingerprint=result_fingerprint,
        manifest_fingerprint=manifest_fingerprint,
    )


def write_m6_artifacts(
    result: M6ValidationResult,
    repo_root: Path,
) -> M6ArtifactManifest:
    """Stage, validate and transactionally publish a successful M6 suite."""

    if not isinstance(result, M6ValidationResult):
        raise TypeError("result must be an M6ValidationResult")
    if result.status != "success" or not result.completion_passed:
        raise ValueError("only a complete successful M6 result may be published")
    config, basis = _load_frozen_sources(repo_root)
    _validate_result_source_closure(result, config, basis)
    targets = {
        name: _safe_output(repo_root, name)
        for name in _PUBLISH_ORDER
    }
    if len(set(targets.values())) != len(targets):  # pragma: no cover - fixed paths
        raise ValueError("M6 artifact output paths must be distinct")
    content = _artifact_bytes(result)
    token = uuid.uuid4().hex
    staged = _stage_bundle(targets, content, token=token)
    try:
        _validate_staged_bundle(staged)
        _publish_staged_bundle(targets, staged, token=token)
    except Exception:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    return verify_m6_artifacts(
        repo_root,
        expected_result_fingerprint=result.result_fingerprint,
    )


def m6_failure_payload(result: M6ValidationResult) -> dict[str, object]:
    """Return an in-memory failure summary that cannot masquerade as a suite."""

    if not isinstance(result, M6ValidationResult):
        raise TypeError("result must be an M6ValidationResult")
    if result.status != "failed" or result.completion_passed:
        raise ValueError("failure payload requires an incomplete failed M6 result")
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_type": "M6_validation_failure",
        "status": result.status,
        "completion_passed": result.completion_passed,
        "valid_artifact_suite": False,
        "result_fingerprint": result.result_fingerprint,
        "versions": dict(result.versions),
        "source_fingerprints": dict(result.source_fingerprints),
        "required_scenario_ids": list(result.required_scenario_ids),
        "scenario_outcomes": {
            scenario.scenario_id: {
                "scenario_status": scenario.scenario_status,
                "verification_outcome": scenario.verification_outcome,
                "failure_stage": scenario.failure_stage,
                "failure_reason": scenario.failure_reason,
            }
            for scenario in result.scenarios
        },
        "last_valid_scenario_ids": list(result.last_valid_scenario_ids),
        "last_valid_evidence": {
            scenario.scenario_id: scenario.as_dict()
            for scenario in result.scenarios
            if scenario.scenario_id in result.last_valid_scenario_ids
        },
        "completion_checks": dict(result.completion_checks),
        "failure_stage": result.failure_stage,
        "failure_reason": result.failure_reason,
        "failure_time_s": result.failure_time_s,
        "metadata": dict(result.metadata),
    }
    return _fingerprinted(payload, field="failure_fingerprint")
