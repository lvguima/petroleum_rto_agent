"""Immutable result contracts for M6 engineering validation.

The contract keeps model execution status separate from verification outcome.
An expected limited or pre-solver rejected scenario can therefore verify its
intended behaviour without being relabelled as a successful model prediction.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import canonical_fingerprint
from .basis import M6Basis
from .domain import ApplicabilityAssessment
from .protection import ProtectionTrace
from .tracking import ControllerTrackingEvidence
from .uncertainty import LocalSensitivityAnalysis, UncertaintyPropagationResult

type ScenarioStatus = Literal["passed", "limited", "rejected", "failed"]
type ExpectedScenarioStatus = Literal["passed", "limited", "rejected"]
type VerificationOutcome = Literal["passed", "failed"]
type ExecutionLayer = Literal[
    "M2_steady",
    "M3_open_loop",
    "M4_closed_loop",
    "M6_supervisory",
    "structural_rejection",
]
type M6ResultStatus = Literal["success", "failed"]

M6_RESULT_SCHEMA_VERSION: Final[str] = "1.0.0"
M6_COMPLETION_CHECK_IDS: Final[tuple[str, ...]] = (
    "scenario_matrix",
    "applicability_domain",
    "uncertainty_propagation",
    "protection_logic",
    "conservation",
    "deterministic_reproduction",
)
M6_SOURCE_ORIGINS: Final[tuple[str, ...]] = (
    "source_traced_field_observation_catalog",
    "M2_steady_model_prediction",
    "M3_open_loop_simulation",
    "M4_closed_loop_simulation",
    "M6_synthetic_validation",
)
M6_SOURCE_COMPOSITION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "source_traced_field_observation_catalog": "source_evidence",
        "M2_steady_model_prediction": "synthetic_prediction",
        "M3_open_loop_simulation": "synthetic_simulation",
        "M4_closed_loop_simulation": "synthetic_simulation",
        "M6_synthetic_validation": "synthetic_validation",
    }
)
M6_RESULT_METADATA: Final[Mapping[str, str]] = MappingProxyType(
    {
        "synthetic": "true",
        "data_origin": "M6_synthetic_validation",
        "claim_scope": "engineering_validation_only",
        "source_basis": "mixed_sources",
    }
)

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SCENARIO_STATUSES: Final[frozenset[str]] = frozenset(
    {"passed", "limited", "rejected", "failed"}
)
_EXPECTED_STATUSES: Final[frozenset[str]] = frozenset(
    {"passed", "limited", "rejected"}
)
_VERIFICATION_OUTCOMES: Final[frozenset[str]] = frozenset({"passed", "failed"})
_EXECUTION_LAYERS: Final[frozenset[str]] = frozenset(
    {
        "M2_steady",
        "M3_open_loop",
        "M4_closed_loop",
        "M6_supervisory",
        "structural_rejection",
    }
)
_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"success", "failed"})
_LAYER_SOURCE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "M2_steady": "M2_steady_model_prediction",
        "M3_open_loop": "M3_open_loop_simulation",
        "M4_closed_loop": "M4_closed_loop_simulation",
        "M6_supervisory": "M6_synthetic_validation",
        "structural_rejection": "M6_synthetic_validation",
    }
)


def _identifier(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{context} must be a non-empty identifier")
    return value


def _text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be non-empty text")
    return value


def _digest(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _identifiers(values: Sequence[str], *, context: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence of identifiers")
    copied = tuple(
        _identifier(value, context=f"{context}[{index}]")
        for index, value in enumerate(values)
    )
    if not copied:
        raise ValueError(f"{context} cannot be empty")
    if len(set(copied)) != len(copied):
        raise ValueError(f"{context} cannot contain duplicates")
    return copied


def _finite_mapping(
    values: Mapping[str, float],
    *,
    context: str,
) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for raw_name, raw_value in values.items():
        name = _identifier(raw_name, context=f"{context} key")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TypeError(f"{context}.{name} must be numeric")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"{context}.{name} must be finite")
        copied[name] = value
    return MappingProxyType({name: copied[name] for name in sorted(copied)})


def _boolean_mapping(
    values: Mapping[str, bool],
    *,
    context: str,
) -> Mapping[str, bool]:
    copied: dict[str, bool] = {}
    for raw_name, value in values.items():
        name = _identifier(raw_name, context=f"{context} key")
        if not isinstance(value, bool):
            raise TypeError(f"{context}.{name} must be boolean")
        copied[name] = value
    return MappingProxyType({name: copied[name] for name in sorted(copied)})


def _source_origins(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("source_origins must be a sequence")
    copied = tuple(values)
    if not copied:
        raise ValueError("source_origins cannot be empty")
    if len(set(copied)) != len(copied):
        raise ValueError("source_origins cannot contain duplicates")
    unknown = set(copied) - set(M6_SOURCE_ORIGINS)
    if unknown:
        raise ValueError(f"unsupported source origins: {sorted(unknown)}")
    return tuple(origin for origin in M6_SOURCE_ORIGINS if origin in copied)


def _trace_summary(trace: ProtectionTrace) -> dict[str, object]:
    event_kind_counts: dict[str, int] = {}
    for event in trace.events:
        event_kind_counts[event.event_kind] = (
            event_kind_counts.get(event.event_kind, 0) + 1
        )
    return {
        "rule_ids": [rule.rule_id for rule in trace.rules],
        "frame_count": len(trace.frames),
        "event_count": len(trace.events),
        "last_time_s": trace.last_time_s,
        "active_rule_ids": list(trace.active_actions),
        "triggered_rule_ids": sorted(
            {
                event.rule_id
                for event in trace.events
                if event.event_kind == "triggered"
            }
        ),
        "final_phases": {
            rule.rule_id: trace.states[rule.rule_id].phase for rule in trace.rules
        },
        "event_kind_counts": {
            name: event_kind_counts[name] for name in sorted(event_kind_counts)
        },
        "trace_fingerprint": canonical_fingerprint(trace.as_dict()),
    }


@dataclass(frozen=True)
class ScenarioValidationResult:
    """One scenario decision with independent execution and verification states."""

    scenario_id: str
    scenario_version: str
    claim_ids: tuple[str, ...]
    purpose: str
    execution_layer: ExecutionLayer
    scenario_status: ScenarioStatus
    expected_status: ExpectedScenarioStatus
    verification_outcome: VerificationOutcome
    solver_called: bool
    domain: ApplicabilityAssessment
    metrics: Mapping[str, float]
    direction_checks: Mapping[str, bool]
    conservation_checks: Mapping[str, bool]
    protection_trace: ProtectionTrace | None
    source_origins: tuple[str, ...]
    engine_status: str | None
    input_fingerprint: str
    failure_stage: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.scenario_id, context="scenario_id")
        _identifier(self.scenario_version, context="scenario_version")
        claim_ids = _identifiers(self.claim_ids, context="claim_ids")
        _text(self.purpose, context="purpose")
        if self.execution_layer not in _EXECUTION_LAYERS:
            raise ValueError("unsupported scenario execution_layer")
        if self.scenario_status not in _SCENARIO_STATUSES:
            raise ValueError("unsupported scenario_status")
        if self.expected_status not in _EXPECTED_STATUSES:
            raise ValueError("unsupported expected_status")
        if self.verification_outcome not in _VERIFICATION_OUTCOMES:
            raise ValueError("unsupported verification_outcome")
        if not isinstance(self.solver_called, bool):
            raise TypeError("solver_called must be boolean")
        if not isinstance(self.domain, ApplicabilityAssessment):
            raise TypeError("domain must be an ApplicabilityAssessment")
        if self.protection_trace is not None and not isinstance(
            self.protection_trace, ProtectionTrace
        ):
            raise TypeError("protection_trace must be a ProtectionTrace or None")

        metrics = _finite_mapping(self.metrics, context="scenario metrics")
        direction_checks = _boolean_mapping(
            self.direction_checks,
            context="scenario direction_checks",
        )
        conservation_checks = _boolean_mapping(
            self.conservation_checks,
            context="scenario conservation_checks",
        )
        origins = _source_origins(self.source_origins)
        if "M6_synthetic_validation" not in origins:
            raise ValueError("every scenario must identify M6_synthetic_validation")
        required_layer_source = _LAYER_SOURCE[self.execution_layer]
        if required_layer_source not in origins:
            raise ValueError("scenario sources do not identify the execution layer")
        _digest(self.input_fingerprint, context="scenario input_fingerprint")

        if self.solver_called:
            if self.engine_status is None:
                raise ValueError("a solver-called scenario requires engine_status")
            _text(self.engine_status, context="engine_status")
        elif self.engine_status is not None:
            raise ValueError("a scenario without a solver call cannot have engine_status")

        expected_domain_status = {
            "passed": "passed",
            "limited": "limited",
            "rejected": "rejected",
        }
        if self.scenario_status != "failed" and (
            self.domain.status != expected_domain_status[self.scenario_status]
        ):
            raise ValueError("scenario_status differs from its applicability decision")
        if self.scenario_status == "failed" and self.domain.status == "rejected":
            raise ValueError("a rejected applicability decision is not a solver failure")

        if self.scenario_status == "rejected":
            if self.execution_layer != "structural_rejection":
                raise ValueError("a rejected scenario must use structural_rejection")
            if self.solver_called:
                raise ValueError("a structurally rejected scenario cannot call a solver")
            if metrics or direction_checks or conservation_checks:
                raise ValueError(
                    "a structurally rejected scenario cannot expose numerical results"
                )
            if self.protection_trace is not None:
                raise ValueError(
                    "a structurally rejected scenario cannot expose a protection trace"
                )
        elif self.execution_layer == "structural_rejection":
            raise ValueError("structural_rejection is reserved for rejected scenarios")

        if self.scenario_status == "passed":
            if not self.solver_called or not metrics or not conservation_checks:
                raise ValueError(
                    "a passed scenario requires solver, metrics and conservation evidence"
                )
            if self.engine_status != "success":
                raise ValueError("a passed scenario requires engine_status=success")
        elif self.scenario_status == "limited":
            if self.solver_called:
                if not metrics or not conservation_checks:
                    raise ValueError(
                        "a solver-called limited scenario requires numerical evidence"
                    )
                if self.engine_status != "success":
                    raise ValueError(
                        "a non-failed limited scenario requires engine_status=success"
                    )
            elif self.protection_trace is None:
                raise ValueError(
                    "a limited scenario without a solver requires protection evidence"
                )

        if self.scenario_status == "failed":
            if self.failure_stage is None or self.failure_reason is None:
                raise ValueError("a failed scenario requires failure stage and reason")
            _text(self.failure_stage, context="scenario failure_stage")
            _text(self.failure_reason, context="scenario failure_reason")
        elif self.failure_stage is not None or self.failure_reason is not None:
            raise ValueError("a non-failed scenario cannot have failure information")

        checks_passed = all(direction_checks.values()) and all(
            conservation_checks.values()
        )
        expected_verification = (
            "passed"
            if self.scenario_status == self.expected_status
            and checks_passed
            else "failed"
        )
        if self.verification_outcome != expected_verification:
            raise ValueError(
                "verification_outcome differs from status and check evidence"
            )

        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "direction_checks", direction_checks)
        object.__setattr__(self, "conservation_checks", conservation_checks)
        object.__setattr__(self, "source_origins", origins)
        object.__setattr__(self, "claim_ids", claim_ids)

    @property
    def protection_trace_summary(self) -> dict[str, object] | None:
        """Return compact serializable protection evidence, if present."""

        if self.protection_trace is None:
            return None
        return _trace_summary(self.protection_trace)

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "claim_ids": list(self.claim_ids),
            "purpose": self.purpose,
            "execution_layer": self.execution_layer,
            "scenario_status": self.scenario_status,
            "expected_status": self.expected_status,
            "verification_outcome": self.verification_outcome,
            "solver_called": self.solver_called,
            "domain": self.domain.as_dict(),
            "metrics": dict(self.metrics),
            "direction_checks": dict(self.direction_checks),
            "conservation_checks": dict(self.conservation_checks),
            "protection_trace": (
                None
                if self.protection_trace is None
                else self.protection_trace.as_dict()
            ),
            "protection_trace_summary": self.protection_trace_summary,
            "source_origins": list(self.source_origins),
            "engine_status": self.engine_status,
            "input_fingerprint": self.input_fingerprint,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
        }

    @property
    def result_fingerprint(self) -> str:
        return canonical_fingerprint(self._fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            **self._fingerprint_payload(),
            "result_fingerprint": self.result_fingerprint,
        }


@dataclass(frozen=True)
class M6ValidationResult:
    """Source-closed aggregate M6 evidence and release-gate decision."""

    schema_version: str
    status: M6ResultStatus
    basis: M6Basis
    validation_config_version: str
    validation_config_fingerprint: str
    control_version: str
    scenario_set_version: str
    required_scenario_ids: tuple[str, ...]
    scenarios: tuple[ScenarioValidationResult, ...]
    required_plan_ids: tuple[str, ...]
    sensitivity_analyses: Mapping[str, LocalSensitivityAnalysis]
    uncertainty_results: Mapping[str, UncertaintyPropagationResult]
    plan_unquantified_sources: Mapping[str, tuple[str, ...]]
    plan_source_origins: Mapping[str, tuple[str, ...]]
    required_protection_rule_ids: tuple[str, ...]
    protection_traces: Mapping[str, ProtectionTrace]
    controller_tracking: Mapping[str, ControllerTrackingEvidence]
    completion_checks: Mapping[str, bool]
    source_composition: Mapping[str, str]
    metadata: Mapping[str, str]
    last_valid_scenario_ids: tuple[str, ...]
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_time_s: float | None = None

    def __post_init__(self) -> None:
        if self.schema_version != M6_RESULT_SCHEMA_VERSION:
            raise ValueError("schema_version differs from the M6 result contract")
        if self.status not in _RESULT_STATUSES:
            raise ValueError("M6 result status must be success or failed")
        if not isinstance(self.basis, M6Basis):
            raise TypeError("basis must be an M6Basis")
        _identifier(
            self.validation_config_version,
            context="validation_config_version",
        )
        _digest(
            self.validation_config_fingerprint,
            context="validation_config_fingerprint",
        )
        _identifier(self.control_version, context="control_version")
        _identifier(self.scenario_set_version, context="scenario_set_version")

        required_scenario_ids = _identifiers(
            self.required_scenario_ids,
            context="required_scenario_ids",
        )
        scenarios = tuple(self.scenarios)
        if any(not isinstance(item, ScenarioValidationResult) for item in scenarios):
            raise TypeError("scenarios must contain ScenarioValidationResult values")
        if tuple(item.scenario_id for item in scenarios) != required_scenario_ids:
            raise ValueError(
                "scenarios must exactly cover required_scenario_ids in config order"
            )

        required_plan_ids = _identifiers(
            self.required_plan_ids,
            context="required_plan_ids",
        )
        if len(required_plan_ids) != 2:
            raise ValueError("required_plan_ids must contain steady and dynamic plans")
        raw_analyses = dict(self.sensitivity_analyses)
        raw_uncertainty = dict(self.uncertainty_results)
        if set(raw_analyses) != set(required_plan_ids):
            raise ValueError("sensitivity_analyses must exactly cover required_plan_ids")
        if set(raw_uncertainty) != set(required_plan_ids):
            raise ValueError("uncertainty_results must exactly cover required_plan_ids")
        analyses = MappingProxyType(
            {plan_id: raw_analyses[plan_id] for plan_id in required_plan_ids}
        )
        uncertainty = MappingProxyType(
            {plan_id: raw_uncertainty[plan_id] for plan_id in required_plan_ids}
        )
        for plan_id in required_plan_ids:
            analysis = analyses[plan_id]
            propagation = uncertainty[plan_id]
            if not isinstance(analysis, LocalSensitivityAnalysis):
                raise TypeError(
                    f"sensitivity_analyses.{plan_id} must be a local analysis"
                )
            if not isinstance(propagation, UncertaintyPropagationResult):
                raise TypeError(
                    f"uncertainty_results.{plan_id} must be a propagation result"
                )
            if not analysis.complete:
                raise ValueError(f"sensitivity_analyses.{plan_id} is incomplete")
            if analysis.basis_fingerprint != self.basis.analysis_basis_fingerprint:
                raise ValueError(f"sensitivity_analyses.{plan_id} uses another basis")
            if propagation.basis_fingerprint != analysis.basis_fingerprint:
                raise ValueError(f"uncertainty_results.{plan_id} uses another basis")
            if propagation.sensitivity_fingerprint != analysis.analysis_fingerprint:
                raise ValueError(
                    f"uncertainty_results.{plan_id} uses another sensitivity analysis"
                )

        raw_unquantified = dict(self.plan_unquantified_sources)
        raw_plan_origins = dict(self.plan_source_origins)
        if set(raw_unquantified) != set(required_plan_ids):
            raise ValueError(
                "plan_unquantified_sources must exactly cover required_plan_ids"
            )
        if set(raw_plan_origins) != set(required_plan_ids):
            raise ValueError("plan_source_origins must exactly cover required_plan_ids")
        plan_unquantified: dict[str, tuple[str, ...]] = {}
        plan_origins: dict[str, tuple[str, ...]] = {}
        expected_plan_origins = {
            required_plan_ids[0]: (
                "M2_steady_model_prediction",
                "M6_synthetic_validation",
            ),
            required_plan_ids[1]: (
                "M3_open_loop_simulation",
                "M6_synthetic_validation",
            ),
        }
        for plan_id in required_plan_ids:
            sources = _identifiers(
                raw_unquantified[plan_id],
                context=f"plan_unquantified_sources.{plan_id}",
            )
            origins = _source_origins(raw_plan_origins[plan_id])
            if origins != expected_plan_origins[plan_id]:
                raise ValueError(
                    f"plan_source_origins.{plan_id} differs from its fixed model layer"
                )
            plan_unquantified[plan_id] = sources
            plan_origins[plan_id] = origins

        required_rule_ids = _identifiers(
            self.required_protection_rule_ids,
            context="required_protection_rule_ids",
        )
        raw_traces = dict(self.protection_traces)
        if set(raw_traces) != set(required_rule_ids):
            raise ValueError(
                "protection_traces must exactly cover required_protection_rule_ids"
            )
        traces: dict[str, ProtectionTrace] = {}
        expected_tracking: dict[str, str] = {}
        for rule_id in required_rule_ids:
            trace = raw_traces[rule_id]
            if not isinstance(trace, ProtectionTrace):
                raise TypeError(f"protection_traces.{rule_id} must be a ProtectionTrace")
            if tuple(rule.rule_id for rule in trace.rules) != (rule_id,):
                raise ValueError(
                    f"protection_traces.{rule_id} must contain exactly its named rule"
                )
            rule = trace.rules[0]
            for loop_id in rule.action.manual_tracking_loop_ids:
                expected_tracking[f"{rule_id}.{loop_id}"] = loop_id
            traces[rule_id] = trace

        raw_tracking = dict(self.controller_tracking)
        if set(raw_tracking) != set(expected_tracking):
            raise ValueError(
                "controller_tracking must exactly cover every rule manual-tracking loop"
            )
        tracking: dict[str, ControllerTrackingEvidence] = {}
        for evidence_id in sorted(expected_tracking):
            _identifier(evidence_id, context="controller_tracking evidence_id")
            evidence = raw_tracking[evidence_id]
            if not isinstance(evidence, ControllerTrackingEvidence):
                raise TypeError(
                    f"controller_tracking.{evidence_id} must be tracking evidence"
                )
            if evidence.loop_id != expected_tracking[evidence_id]:
                raise ValueError(
                    f"controller_tracking.{evidence_id} loop_id differs from its rule"
                )
            expected_pass = (
                evidence.final_tracking_error <= evidence.tolerance
                and evidence.automatic_return_jump <= evidence.tolerance
            )
            if evidence.passed != expected_pass:
                raise ValueError(
                    f"controller_tracking.{evidence_id} pass flag differs from errors"
                )
            tracking[evidence_id] = evidence

        checks = dict(self.completion_checks)
        if set(checks) != set(M6_COMPLETION_CHECK_IDS):
            raise ValueError("completion_checks differ from the fixed M6 gate set")
        if any(not isinstance(value, bool) for value in checks.values()):
            raise TypeError("completion_checks values must be boolean")
        ordered_checks = MappingProxyType(
            {name: checks[name] for name in M6_COMPLETION_CHECK_IDS}
        )

        source_composition = dict(self.source_composition)
        if source_composition != dict(M6_SOURCE_COMPOSITION):
            raise ValueError("source_composition differs from the mixed-source contract")
        metadata = dict(self.metadata)
        if metadata != dict(M6_RESULT_METADATA):
            raise ValueError("metadata differs from the M6 synthetic claim contract")

        object.__setattr__(self, "required_scenario_ids", required_scenario_ids)
        object.__setattr__(self, "scenarios", scenarios)
        object.__setattr__(self, "required_plan_ids", required_plan_ids)
        object.__setattr__(self, "sensitivity_analyses", analyses)
        object.__setattr__(self, "uncertainty_results", uncertainty)
        object.__setattr__(
            self,
            "plan_unquantified_sources",
            MappingProxyType(
                {plan_id: plan_unquantified[plan_id] for plan_id in required_plan_ids}
            ),
        )
        object.__setattr__(
            self,
            "plan_source_origins",
            MappingProxyType(
                {plan_id: plan_origins[plan_id] for plan_id in required_plan_ids}
            ),
        )
        object.__setattr__(self, "required_protection_rule_ids", required_rule_ids)
        object.__setattr__(
            self,
            "protection_traces",
            MappingProxyType({rule_id: traces[rule_id] for rule_id in required_rule_ids}),
        )
        object.__setattr__(
            self,
            "controller_tracking",
            MappingProxyType(tracking),
        )
        object.__setattr__(self, "completion_checks", ordered_checks)
        object.__setattr__(
            self,
            "source_composition",
            MappingProxyType(source_composition),
        )
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

        valid_scenario_ids = tuple(
            scenario.scenario_id
            for scenario in scenarios
            if scenario.scenario_status != "failed"
            and scenario.verification_outcome == "passed"
        )
        last_valid_ids = tuple(self.last_valid_scenario_ids)
        if last_valid_ids != valid_scenario_ids:
            raise ValueError(
                "last_valid_scenario_ids must exactly identify valid scenario evidence"
            )
        object.__setattr__(self, "last_valid_scenario_ids", last_valid_ids)

        if self.status == "success":
            if not self.completion_passed:
                raise ValueError("a successful M6 result must pass every evidence gate")
            if (
                self.failure_stage is not None
                or self.failure_reason is not None
                or self.failure_time_s is not None
            ):
                raise ValueError("a successful M6 result cannot have failure information")
            if last_valid_ids != required_scenario_ids:
                raise ValueError(
                    "a successful M6 result must retain every scenario as valid evidence"
                )
        else:
            if self.completion_passed:
                raise ValueError("a complete M6 result cannot be labelled failed")
            if self.failure_stage is None or self.failure_reason is None:
                raise ValueError("a failed M6 result requires failure stage and reason")
            _text(self.failure_stage, context="M6 failure_stage")
            _text(self.failure_reason, context="M6 failure_reason")
            if self.failure_time_s is None:
                raise ValueError("a failed M6 result requires failure_time_s")
            if (
                isinstance(self.failure_time_s, bool)
                or not isinstance(self.failure_time_s, (int, float))
                or not math.isfinite(self.failure_time_s)
                or self.failure_time_s < 0.0
            ):
                raise ValueError("M6 failure_time_s must be finite and non-negative")
            retained_trace_time = max(
                (
                    scenario.protection_trace.last_time_s
                    for scenario in scenarios
                    if scenario.scenario_id in last_valid_ids
                    and scenario.protection_trace is not None
                ),
                default=0.0,
            )
            if float(self.failure_time_s) < retained_trace_time:
                raise ValueError(
                    "M6 failure_time_s cannot precede retained scenario evidence"
                )
            object.__setattr__(self, "failure_time_s", float(self.failure_time_s))

    @property
    def completion_passed(self) -> bool:
        scenario_gate = all(
            scenario.verification_outcome == "passed"
            and scenario.scenario_status != "failed"
            for scenario in self.scenarios
        )
        solver_scenarios = tuple(
            scenario for scenario in self.scenarios if scenario.solver_called
        )
        conservation_gate = bool(solver_scenarios) and all(
            bool(scenario.conservation_checks)
            and all(scenario.conservation_checks.values())
            for scenario in solver_scenarios
        )
        protection_gate = bool(self.protection_traces) and all(
            any(event.event_kind == "triggered" for event in trace.events)
            for trace in self.protection_traces.values()
        ) and all(
            evidence.passed for evidence in self.controller_tracking.values()
        )
        return (
            scenario_gate
            and conservation_gate
            and protection_gate
            and all(self.completion_checks.values())
        )

    @property
    def versions(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "software_version": SOFTWARE_VERSION,
                "model_version": self.basis.model.model_version,
                "model_config_version": self.basis.model.config_version,
                "base_parameter_set_version": self.basis.base_parameter_set_version,
                "derived_parameter_set_version": (
                    self.basis.derived_parameter_set_version
                ),
                "base_case_version": self.basis.base_case_version,
                "derived_case_version": self.basis.derived_case_version,
                "control_version": self.control_version,
                "scenario_set_version": self.scenario_set_version,
                "validation_config_version": self.validation_config_version,
                "basis_analysis_version": self.basis.analysis_version,
                "simulation_stage": "M6_engineering_validation",
            }
        )

    @property
    def source_fingerprints(self) -> Mapping[str, str]:
        scenario_fingerprint = canonical_fingerprint(
            {
                "scenario_set_version": self.scenario_set_version,
                "required_scenario_ids": list(self.required_scenario_ids),
                "scenario_results": [
                    scenario.result_fingerprint for scenario in self.scenarios
                ],
            }
        )
        plan_fingerprint = canonical_fingerprint(
            {
                "required_plan_ids": list(self.required_plan_ids),
                "sensitivity_analyses": {
                    plan_id: self.sensitivity_analyses[plan_id].analysis_fingerprint
                    for plan_id in self.required_plan_ids
                },
                "uncertainty_results": {
                    plan_id: self.uncertainty_results[plan_id].result_fingerprint
                    for plan_id in self.required_plan_ids
                },
                "unquantified_sources": {
                    plan_id: list(self.plan_unquantified_sources[plan_id])
                    for plan_id in self.required_plan_ids
                },
                "source_origins": {
                    plan_id: list(self.plan_source_origins[plan_id])
                    for plan_id in self.required_plan_ids
                },
            }
        )
        protection_fingerprint = canonical_fingerprint(
            {
                "required_rule_ids": list(self.required_protection_rule_ids),
                "traces": {
                    rule_id: self.protection_traces[rule_id].as_dict()
                    for rule_id in self.required_protection_rule_ids
                },
                "controller_tracking": {
                    evidence_id: evidence.as_dict()
                    for evidence_id, evidence in self.controller_tracking.items()
                },
            }
        )
        return MappingProxyType(
            {
                "analysis_basis": self.basis.analysis_basis_fingerprint,
                "validation_config": self.validation_config_fingerprint,
                "m5_pipeline": self.basis.m5_pipeline_fingerprint,
                "m5_manifest_sha256": self.basis.m5_manifest_sha256,
                "scenario_results": scenario_fingerprint,
                "sensitivity_uncertainty_plans": plan_fingerprint,
                "protection_tracking_evidence": protection_fingerprint,
            }
        )

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "versions": dict(self.versions),
            "source_fingerprints": dict(self.source_fingerprints),
            "basis": self.basis.as_dict(),
            "required_scenario_ids": list(self.required_scenario_ids),
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "required_plan_ids": list(self.required_plan_ids),
            "sensitivity_analyses": {
                plan_id: self.sensitivity_analyses[plan_id].as_dict()
                for plan_id in self.required_plan_ids
            },
            "uncertainty_results": {
                plan_id: self.uncertainty_results[plan_id].as_dict()
                for plan_id in self.required_plan_ids
            },
            "plan_unquantified_sources": {
                plan_id: list(self.plan_unquantified_sources[plan_id])
                for plan_id in self.required_plan_ids
            },
            "plan_source_origins": {
                plan_id: list(self.plan_source_origins[plan_id])
                for plan_id in self.required_plan_ids
            },
            "required_protection_rule_ids": list(
                self.required_protection_rule_ids
            ),
            "protection_traces": {
                rule_id: self.protection_traces[rule_id].as_dict()
                for rule_id in self.required_protection_rule_ids
            },
            "controller_tracking": {
                evidence_id: evidence.as_dict()
                for evidence_id, evidence in self.controller_tracking.items()
            },
            "completion_checks": dict(self.completion_checks),
            "completion_passed": self.completion_passed,
            "source_composition": dict(self.source_composition),
            "metadata": dict(self.metadata),
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "failure_time_s": self.failure_time_s,
            "last_valid_scenario_ids": list(self.last_valid_scenario_ids),
        }

    @property
    def result_fingerprint(self) -> str:
        return canonical_fingerprint(self._fingerprint_payload())

    def as_dict(self) -> dict[str, object]:
        return {
            **self._fingerprint_payload(),
            "result_fingerprint": self.result_fingerprint,
        }
