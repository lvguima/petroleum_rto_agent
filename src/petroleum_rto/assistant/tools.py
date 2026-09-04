"""Closed tool gateway for the local engineering assistant."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from petroleum_rto.rto import load_operating_context
from petroleum_rto.rto.communication import OptimizationIntent
from petroleum_rto.rto.runtime import (
    build_chat_operating_status,
    capabilities,
    run_confirmed_optimization,
)

AGENT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "show_capabilities",
        "show_simulation_status",
        "run_offline",
        "inspect_result",
    }
)
_SIMULATION_CONTEXT_PATH = Path("configs/rto/contexts/case_20260604.json")
_RUN_ROOT = Path("runs/rto")
_WORKFLOW_ID_PATTERN = re.compile(r"offline-rto-[0-9a-f]{16}\Z")
_RESULT_SOURCE_PATTERN = re.compile(r"(offline-rto-[0-9a-f]{16})/result\.json\Z")


class AgentActionDenied(ValueError):
    """Raised when a caller requests an action outside the fixed set."""


def _result_path(source: str, *, run_root: Path) -> Path:
    workflow_id: str | None = None
    if _WORKFLOW_ID_PATTERN.fullmatch(source) is not None:
        workflow_id = source
    else:
        match = _RESULT_SOURCE_PATTERN.fullmatch(source)
        if match is not None:
            workflow_id = match.group(1)

    if workflow_id is not None:
        if run_root.is_symlink() or run_root.parent.is_symlink():
            raise ValueError("RTO run root must not be a symbolic link")
        controlled_root = run_root.resolve()
        run_dir = controlled_root / workflow_id
        if run_dir.is_symlink():
            raise ValueError("RTO workflow directory must not be a symbolic link")
        path = run_dir / "result.json"
    else:
        raw_path = Path(source).expanduser()
        if not raw_path.is_absolute() and (
            source.startswith("offline-rto-") or ".." in raw_path.parts
        ):
            raise ValueError("RTO result source is not a valid controlled workflow reference")
        path = raw_path.resolve()
    if path.is_dir():
        path = path / "result.json"
    if path.name != "result.json" or not path.is_file() or path.is_symlink():
        raise ValueError("RTO result source must be a run directory or result.json")
    return path


def _reject_constant(value: str) -> object:
    raise ValueError(f"result.json contains non-finite value {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"result.json contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_result(source: str, *, run_root: Path) -> Mapping[str, object]:
    path = _result_path(source, run_root=run_root)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("result.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise TypeError("result.json must contain one object")
    expected = {
        "status",
        "targets",
        "operating_context",
        "baseline_values",
        "recommended_adjustments",
        "predicted_effects",
        "alternative_candidates",
    }
    if set(value) != expected:
        raise ValueError("result.json fields differ from the compact result contract")
    return dict(value)


def _rows(value: object, *, context: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{context} must be a sequence")
    rows: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"{context}[{index}] must be a mapping")
        rows.append(item)
    return tuple(rows)


def _text(row: Mapping[str, object], field: str, *, context: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{context}.{field} must be a non-empty string")
    return value


def _positive_int(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{context} must be a positive integer")
    return value


def _capability_projection(raw: Mapping[str, object]) -> dict[str, object]:
    """Reduce the public RTO manifest to the facts needed by a chat answer."""

    if raw.get("solver_called") is not False:
        raise ValueError("capability discovery must not call a solver")
    claim_scope = raw.get("claim_scope")
    if claim_scope != "engineering_simulation_only":
        raise ValueError("unsupported capability claim scope")

    metric_units: dict[str, str] = {}
    for row in _rows(raw.get("metrics"), context="capabilities.metrics"):
        if row.get("availability") == "available":
            metric_units[_text(row, "metric_id", context="metric")] = _text(
                row, "unit", context="metric"
            )

    objectives: list[dict[str, str]] = []
    for row in _rows(raw.get("objectives"), context="capabilities.objectives"):
        if row.get("availability") != "available":
            continue
        metric_id = _text(row, "metric_id", context="objective")
        if metric_id not in metric_units:
            raise ValueError("available objective must reference an available metric")
        objectives.append(
            {
                "objective_id": _text(row, "objective_id", context="objective"),
                "business_name": _text(row, "business_name", context="objective"),
                "sense": _text(row, "sense", context="objective"),
                "unit": metric_units[metric_id],
            }
        )

    decisions: list[dict[str, str]] = []
    for row in _rows(raw.get("decisions"), context="capabilities.decisions"):
        if row.get("availability") != "available":
            continue
        decisions.append(
            {
                "decision_id": _text(row, "decision_id", context="decision"),
                "business_name": _text(row, "business_name", context="decision"),
                "display_unit": _text(row, "display_unit", context="decision"),
            }
        )

    routes = _rows(raw.get("execution_routes"), context="capabilities.execution_routes")
    minimum_objectives = min(
        _positive_int(row.get("minimum_objectives"), context="route.minimum_objectives")
        for row in routes
    )
    maximum_objectives = max(
        _positive_int(row.get("maximum_objectives"), context="route.maximum_objectives")
        for row in routes
    )
    if not objectives or not decisions:
        raise ValueError("capability projection must contain available objectives and decisions")
    if minimum_objectives > maximum_objectives or maximum_objectives > len(objectives):
        raise ValueError("objective-count capability is inconsistent with available objectives")

    return {
        "claim_scope": claim_scope,
        "objectives": objectives,
        "decision_variables": decisions,
        "supported_objective_count": {
            "minimum": minimum_objectives,
            "maximum": maximum_objectives,
        },
        "output_kind": "steady_setpoint_vector",
        "solver_called": False,
    }


@dataclass(frozen=True, slots=True)
class AgentTools:
    """Expose the fixed actions authorized through phase three."""

    workspace: Path

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Path):
            raise TypeError("workspace must be Path")
        object.__setattr__(self, "workspace", self.workspace.resolve())

    def invoke(
        self,
        action: str,
        *,
        source: str | None = None,
        intent: OptimizationIntent | None = None,
    ) -> Mapping[str, object]:
        if action not in AGENT_ACTIONS:
            raise AgentActionDenied(f"action is not available to the agent: {action}")
        if action == "show_capabilities":
            if source is not None or intent is not None:
                raise ValueError("show_capabilities does not accept arguments")
            return _capability_projection(capabilities(repo_root=self.workspace))
        if action == "show_simulation_status":
            if source is not None or intent is not None:
                raise ValueError("show_simulation_status does not accept arguments")
            context = load_operating_context(self.workspace / _SIMULATION_CONTEXT_PATH)
            summary = build_chat_operating_status(context)
            if not isinstance(summary, Mapping):
                raise TypeError("simulation status summary must be a mapping")
            return summary
        if action == "run_offline":
            if source is not None or not isinstance(intent, OptimizationIntent):
                raise ValueError("run_offline requires one resolved intent")
            return run_confirmed_optimization(
                repo_root=self.workspace,
                intent=intent,
                context_file=self.workspace / _SIMULATION_CONTEXT_PATH,
                run_root=self.workspace / _RUN_ROOT,
            )

        if source is None or not source.strip() or intent is not None:
            raise ValueError("inspect_result requires exactly one source")
        return _read_result(source.strip(), run_root=self.workspace / _RUN_ROOT)
