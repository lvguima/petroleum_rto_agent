"""Neutral, immutable contracts for solver discovery and routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from ..contracts.common import (
    as_mapping,
    as_sequence,
    canonical_fingerprint,
    digest,
    identifier,
    integer,
    strict_keys,
    text,
)
from ..contracts.reference import ContractRef

SOLVER_ROUTING_SCHEMA_VERSION: Final[str] = "2.0.0"

RoutingStatus = Literal["selected", "unsupported"]


def _schema(value: str) -> None:
    if value != SOLVER_ROUTING_SCHEMA_VERSION:
        raise ValueError("schema_version differs from the solver routing contract")


def _boolean(value: object, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be boolean")
    return value


def _identifiers(
    values: tuple[str, ...],
    *,
    context: str,
    ordered: bool,
) -> tuple[str, ...]:
    result = tuple(identifier(value, context=f"{context} item") for value in values)
    if not result:
        raise ValueError(f"{context} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{context} must contain unique identifiers")
    if not ordered and result != tuple(sorted(result)):
        raise ValueError(f"{context} must be sorted")
    return result


@dataclass(frozen=True)
class ProblemFeatures:
    """Solver-relevant facts derived from one immutable optimization problem."""

    objective_count: int
    decision_count: int
    bounded: bool
    grid_cardinality: int | None
    result_mode: str
    deterministic: bool
    maximum_evaluations: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "objective_count",
            integer(self.objective_count, context="objective_count", minimum=1),
        )
        object.__setattr__(
            self,
            "decision_count",
            integer(self.decision_count, context="decision_count", minimum=1),
        )
        object.__setattr__(self, "bounded", _boolean(self.bounded, context="bounded"))
        if self.grid_cardinality is not None:
            object.__setattr__(
                self,
                "grid_cardinality",
                integer(self.grid_cardinality, context="grid_cardinality", minimum=1),
            )
        object.__setattr__(
            self,
            "result_mode",
            identifier(self.result_mode, context="result_mode"),
        )
        object.__setattr__(
            self,
            "deterministic",
            _boolean(self.deterministic, context="deterministic"),
        )
        object.__setattr__(
            self,
            "maximum_evaluations",
            integer(self.maximum_evaluations, context="maximum_evaluations", minimum=1),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_count": self.objective_count,
            "decision_count": self.decision_count,
            "bounded": self.bounded,
            "grid_cardinality": self.grid_cardinality,
            "result_mode": self.result_mode,
            "deterministic": self.deterministic,
            "maximum_evaluations": self.maximum_evaluations,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class SolverDescriptor:
    """Stable identity and advertised result modes for one solver plugin."""

    solver_id: str
    solver_version: str
    deterministic: bool
    supported_result_modes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "solver_id", identifier(self.solver_id, context="solver_id"))
        object.__setattr__(
            self,
            "solver_version",
            identifier(self.solver_version, context="solver_version"),
        )
        object.__setattr__(
            self,
            "deterministic",
            _boolean(self.deterministic, context="deterministic"),
        )
        object.__setattr__(
            self,
            "supported_result_modes",
            _identifiers(
                self.supported_result_modes,
                context="supported_result_modes",
                ordered=False,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "solver_version": self.solver_version,
            "deterministic": self.deterministic,
            "supported_result_modes": list(self.supported_result_modes),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class SolverSupport:
    """Structured answer from a plugin without running the optimization."""

    supported: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported",
            _boolean(self.supported, context="supported"),
        )
        reasons = tuple(identifier(value, context="reason_code") for value in self.reason_codes)
        if len(reasons) != len(set(reasons)) or reasons != tuple(sorted(reasons)):
            raise ValueError("reason_codes must be unique and sorted")
        if self.supported == bool(reasons):
            raise ValueError(
                "supported solvers require no reasons; unsupported solvers require reasons"
            )
        object.__setattr__(self, "reason_codes", reasons)

    @classmethod
    def yes(cls) -> SolverSupport:
        return cls(supported=True)

    @classmethod
    def no(cls, *reason_codes: str) -> SolverSupport:
        return cls(supported=False, reason_codes=tuple(sorted(reason_codes)))

    def as_dict(self) -> dict[str, object]:
        return {"supported": self.supported, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class SolverRoutingDecision:
    """Serializable result of checking one problem-bound execution route."""

    schema_version: str
    routing_version: str
    status: RoutingStatus
    problem_ref: ContractRef
    features_fingerprint: str
    execution_route_ref: ContractRef
    algorithm_id: str
    algorithm_version: str
    selected_solver_id: str | None
    selected_solver_version: str | None
    selected_solver_fingerprint: str | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(
            self,
            "routing_version",
            identifier(self.routing_version, context="routing_version"),
        )
        if self.status not in {"selected", "unsupported"}:
            raise ValueError("unsupported solver routing status")
        for name in ("problem_ref", "execution_route_ref"):
            if not isinstance(getattr(self, name), ContractRef):
                raise TypeError(f"{name} must be ContractRef")
        object.__setattr__(
            self,
            "features_fingerprint",
            digest(self.features_fingerprint, context="features_fingerprint"),
        )
        object.__setattr__(
            self, "algorithm_id", identifier(self.algorithm_id, context="algorithm_id")
        )
        object.__setattr__(
            self,
            "algorithm_version",
            identifier(self.algorithm_version, context="algorithm_version"),
        )
        selected = (
            self.selected_solver_id,
            self.selected_solver_version,
            self.selected_solver_fingerprint,
        )
        if any(value is not None for value in selected):
            if any(value is None for value in selected):
                raise ValueError("selected solver identity must be complete")
            object.__setattr__(
                self,
                "selected_solver_id",
                identifier(self.selected_solver_id, context="selected_solver_id"),
            )
            object.__setattr__(
                self,
                "selected_solver_version",
                identifier(self.selected_solver_version, context="selected_solver_version"),
            )
            object.__setattr__(
                self,
                "selected_solver_fingerprint",
                digest(
                    self.selected_solver_fingerprint,
                    context="selected_solver_fingerprint",
                ),
            )
        reasons = tuple(identifier(item, context="reason_code") for item in self.reason_codes)
        if len(reasons) != len(set(reasons)) or reasons != tuple(sorted(reasons)):
            raise ValueError("routing reason_codes must be unique and sorted")
        object.__setattr__(self, "reason_codes", reasons)
        if self.status == "selected":
            if (
                self.selected_solver_id != self.algorithm_id
                or self.selected_solver_version != self.algorithm_version
                or reasons
            ):
                raise ValueError("selected solver must exactly match its execution route")
        elif any(value is not None for value in selected) or not reasons:
            raise ValueError("unsupported route requires reasons and no selected solver")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "routing_version": self.routing_version,
            "status": self.status,
            "problem_ref": self.problem_ref.as_dict(),
            "features_fingerprint": self.features_fingerprint,
            "execution_route_ref": self.execution_route_ref.as_dict(),
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "selected_solver_id": self.selected_solver_id,
            "selected_solver_version": self.selected_solver_version,
            "selected_solver_fingerprint": self.selected_solver_fingerprint,
            "reason_codes": list(self.reason_codes),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolverRoutingDecision:
        strict_keys(
            value,
            required={
                "schema_version",
                "routing_version",
                "status",
                "problem_ref",
                "features_fingerprint",
                "execution_route_ref",
                "algorithm_id",
                "algorithm_version",
                "selected_solver_id",
                "selected_solver_version",
                "selected_solver_fingerprint",
                "reason_codes",
            },
            context="solver routing decision",
        )
        status = value["status"]
        if status not in {"selected", "unsupported"}:
            raise ValueError("unsupported solver routing status")
        selected_id = value["selected_solver_id"]
        selected_version = value["selected_solver_version"]
        selected_fingerprint = value["selected_solver_fingerprint"]
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            routing_version=identifier(value["routing_version"], context="routing_version"),
            status=status,
            problem_ref=ContractRef.from_mapping(
                as_mapping(value["problem_ref"], context="problem_ref")
            ),
            features_fingerprint=digest(
                value["features_fingerprint"], context="features_fingerprint"
            ),
            execution_route_ref=ContractRef.from_mapping(
                as_mapping(value["execution_route_ref"], context="execution_route_ref")
            ),
            algorithm_id=identifier(value["algorithm_id"], context="algorithm_id"),
            algorithm_version=identifier(value["algorithm_version"], context="algorithm_version"),
            selected_solver_id=(
                None
                if selected_id is None
                else identifier(selected_id, context="selected_solver_id")
            ),
            selected_solver_version=(
                None
                if selected_version is None
                else identifier(selected_version, context="selected_solver_version")
            ),
            selected_solver_fingerprint=(
                None
                if selected_fingerprint is None
                else digest(selected_fingerprint, context="selected_solver_fingerprint")
            ),
            reason_codes=tuple(
                identifier(item, context="reason_code")
                for item in as_sequence(value["reason_codes"], context="reason_codes")
            ),
        )
