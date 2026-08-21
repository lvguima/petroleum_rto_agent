"""Trusted formula registry for paired M2 evidence.

Catalogs select only pre-registered formula identifiers.  They never carry
executable expressions or dynamically imported code.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from ..capabilities.models import CapabilityCatalog, MetricCapability
from ..contracts.common import finite
from ..contracts.problem import OptimizationProblem
from ..contracts.simulation import SimulationRunBundle


@dataclass(frozen=True)
class PairedMetricValue:
    """Baseline and candidate values for one cataloged metric."""

    metric_id: str
    baseline_value: float
    candidate_value: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline_value",
            finite(self.baseline_value, context=f"{self.metric_id}.baseline_value"),
        )
        object.__setattr__(
            self,
            "candidate_value",
            finite(self.candidate_value, context=f"{self.metric_id}.candidate_value"),
        )


class MetricResolver(Protocol):
    def evaluate(self, metric_id: str) -> PairedMetricValue: ...


class FormulaHandler(Protocol):
    def __call__(
        self,
        metric: MetricCapability,
        baseline: SimulationRunBundle,
        candidate: SimulationRunBundle,
        resolver: MetricResolver,
    ) -> PairedMetricValue: ...


def _path_value(root: object, dotted_path: str) -> object:
    current = root
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def _finite_path(root: object, dotted_path: str) -> float:
    value = _path_value(root, dotted_path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{dotted_path} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{dotted_path} is not finite")
    return result


def _require_path_count(metric: MetricCapability, count: int) -> tuple[str, ...]:
    if len(metric.source_paths) != count:
        raise ValueError(f"formula {metric.formula_ref!r} requires exactly {count} source paths")
    return metric.source_paths


def _m2_evaluable(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del resolver
    if len(metric.source_paths) < 1:
        raise ValueError("m2 evaluability formula requires source paths")
    path = metric.source_paths[-1]
    return PairedMetricValue(
        metric.metric_id,
        _finite_path(baseline.as_dict(), path),
        _finite_path(candidate.as_dict(), path),
    )


def _specific_furnace_energy(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del resolver
    fuel_path, feed_path = _require_path_count(metric, 2)

    def value(bundle: SimulationRunBundle) -> float:
        feed = _finite_path(bundle.as_dict(), feed_path)
        if feed <= 0.0:
            raise ValueError("fresh feed must be positive")
        return _finite_path(bundle.as_dict(), fuel_path) / feed / 1000.0

    return PairedMetricValue(metric.metric_id, value(baseline), value(candidate))


def _valuable_distillate_yield(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del resolver
    if not metric.source_paths:
        raise ValueError("yield formula requires source paths")

    def value(bundle: SimulationRunBundle) -> float:
        payload = bundle.as_dict()
        return sum(_finite_path(payload, path) for path in metric.source_paths)

    return PairedMetricValue(metric.metric_id, value(baseline), value(candidate))


def _quality_proxy_change(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del resolver
    if not metric.source_paths:
        raise ValueError("quality-change formula requires source paths")
    baseline_payload = baseline.as_dict()
    candidate_payload = candidate.as_dict()
    change = max(
        abs(_finite_path(candidate_payload, path) - _finite_path(baseline_payload, path))
        / max(abs(_finite_path(baseline_payload, path)), 1e-12)
        for path in metric.source_paths
    )
    return PairedMetricValue(metric.metric_id, 0.0, change)


def _paired_dependency(metric: MetricCapability) -> str:
    (path,) = _require_path_count(metric, 1)
    prefix = "paired."
    if not path.startswith(prefix) or len(path) == len(prefix):
        raise ValueError("paired formula requires one paired.<metric_id> source path")
    return path[len(prefix) :]


def _candidate_minus_baseline(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del baseline, candidate
    dependency = resolver.evaluate(_paired_dependency(metric))
    return PairedMetricValue(
        metric.metric_id,
        0.0,
        dependency.candidate_value - dependency.baseline_value,
    )


def _baseline_minus_candidate_fraction(
    metric: MetricCapability,
    baseline: SimulationRunBundle,
    candidate: SimulationRunBundle,
    resolver: MetricResolver,
) -> PairedMetricValue:
    del baseline, candidate
    dependency = resolver.evaluate(_paired_dependency(metric))
    if abs(dependency.baseline_value) <= 1e-12:
        raise ValueError("relative paired formula has a zero baseline")
    return PairedMetricValue(
        metric.metric_id,
        0.0,
        (dependency.baseline_value - dependency.candidate_value) / dependency.baseline_value,
    )


_BUILTIN_FORMULAS: Final[Mapping[str, FormulaHandler]] = MappingProxyType(
    {
        "runtime_convergence_conservation_finite_nonnegative_v1": _m2_evaluable,
        "furnace_fuel_w_div_feed_kg_s_div_1000_v1": _specific_furnace_energy,
        "sum_gasoline_kerosene_light_and_heavy_diesel_yields_v1": (_valuable_distillate_yield),
        "max_abs_relative_change_with_small_denominator_guard_v1": (_quality_proxy_change),
        "paired_candidate_minus_baseline_v1": _candidate_minus_baseline,
        "paired_baseline_minus_candidate_over_baseline_v1": (_baseline_minus_candidate_fraction),
    }
)


class _FormulaSession:
    def __init__(
        self,
        catalog: CapabilityCatalog,
        baseline: SimulationRunBundle,
        candidate: SimulationRunBundle,
        handlers: Mapping[str, FormulaHandler],
    ) -> None:
        self._metrics = {item.metric_id: item for item in catalog.metrics}
        self._baseline = baseline
        self._candidate = candidate
        self._handlers = handlers
        self._cache: dict[str, PairedMetricValue] = {}
        self._active: set[str] = set()

    @property
    def values(self) -> Mapping[str, PairedMetricValue]:
        return MappingProxyType(dict(self._cache))

    def evaluate(self, metric_id: str) -> PairedMetricValue:
        cached = self._cache.get(metric_id)
        if cached is not None:
            return cached
        metric = self._metrics.get(metric_id)
        if metric is None:
            raise ValueError(f"metric {metric_id!r} is absent from the capability catalog")
        handler = self._handlers.get(metric.formula_ref)
        if handler is None:
            raise ValueError(f"formula {metric.formula_ref!r} is not registered")
        if metric_id in self._active:
            raise ValueError(f"metric formula dependency cycle detected at {metric_id!r}")
        self._active.add(metric_id)
        try:
            value = handler(metric, self._baseline, self._candidate, self)
        finally:
            self._active.remove(metric_id)
        if value.metric_id != metric_id:
            raise ValueError("formula returned a value for another metric")
        self._cache[metric_id] = value
        return value


class TrustedM2FormulaRegistry:
    """Evaluate only built-in, reviewed formula identifiers."""

    @property
    def formula_ids(self) -> tuple[str, ...]:
        return tuple(sorted(_BUILTIN_FORMULAS))

    def supports(self, formula_id: str) -> bool:
        return formula_id in _BUILTIN_FORMULAS

    def evaluate_required(
        self,
        problem: OptimizationProblem,
        catalog: CapabilityCatalog,
        baseline: SimulationRunBundle,
        candidate: SimulationRunBundle,
    ) -> Mapping[str, PairedMetricValue]:
        if not isinstance(problem, OptimizationProblem):
            raise TypeError("problem must be an OptimizationProblem")
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must be a CapabilityCatalog")
        if not isinstance(baseline, SimulationRunBundle) or not isinstance(
            candidate, SimulationRunBundle
        ):
            raise TypeError("formula evaluation requires paired SimulationRunBundle values")
        if problem.capability_catalog_ref != catalog.ref:
            raise ValueError("problem references another capability catalog")

        by_id = {item.metric_id: item for item in catalog.metrics}
        required: list[str] = []
        for objective in problem.objectives:
            metric = by_id.get(objective.metric_id)
            if metric is None:
                raise ValueError("problem objective references an unknown metric")
            if objective.evaluation_stage != "M2" or metric.stage != "M2":
                raise ValueError("M2 evaluator received a non-M2 objective")
            if (
                objective.formula_id != metric.formula_ref
                or objective.unit != metric.unit
                or objective.sense != metric.direction
            ):
                raise ValueError("problem objective differs from its metric capability")
            required.append(objective.metric_id)
        for rule in problem.hard_constraints:
            if rule.evaluation_stage != "M2":
                continue
            metric = by_id.get(rule.metric_id)
            if metric is None or metric.stage != "M2" or metric.unit != rule.unit:
                raise ValueError("M2 constraint differs from its metric capability")
            required.append(rule.metric_id)
        for rule in problem.publishability_constraints:
            metric = by_id.get(rule.metric_id)
            if (
                rule.source != "system"
                or rule.evaluation_stage != "post_selection"
                or metric is None
                or metric.stage != "post_selection"
                or metric.unit != rule.unit
            ):
                raise ValueError("publishability constraint differs from its metric capability")
            required.append(rule.metric_id)

        session = _FormulaSession(catalog, baseline, candidate, _BUILTIN_FORMULAS)
        for metric_id in dict.fromkeys(required):
            session.evaluate(metric_id)
        return session.values


__all__ = ["PairedMetricValue", "TrustedM2FormulaRegistry"]
