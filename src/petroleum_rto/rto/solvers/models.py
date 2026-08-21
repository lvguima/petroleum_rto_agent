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

SOLVER_ROUTING_SCHEMA_VERSION: Final[str] = "1.0.0"

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
    gradient_availability: str
    evaluator_kind: str
    dynamic_verification_required: bool

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
        object.__setattr__(
            self,
            "gradient_availability",
            identifier(self.gradient_availability, context="gradient_availability"),
        )
        object.__setattr__(
            self,
            "evaluator_kind",
            identifier(self.evaluator_kind, context="evaluator_kind"),
        )
        object.__setattr__(
            self,
            "dynamic_verification_required",
            _boolean(
                self.dynamic_verification_required,
                context="dynamic_verification_required",
            ),
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
            "gradient_availability": self.gradient_availability,
            "evaluator_kind": self.evaluator_kind,
            "dynamic_verification_required": self.dynamic_verification_required,
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
class SolverRoutingPolicy:
    """Versioned and explicitly ordered solver preference policy."""

    schema_version: str
    policy_version: str
    policy_id: str
    solver_order: tuple[str, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(
            self,
            "policy_version",
            identifier(self.policy_version, context="policy_version"),
        )
        object.__setattr__(self, "policy_id", identifier(self.policy_id, context="policy_id"))
        object.__setattr__(
            self,
            "solver_order",
            _identifiers(self.solver_order, context="solver_order", ordered=True),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "policy_id": self.policy_id,
            "solver_order": list(self.solver_order),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_dict())


@dataclass(frozen=True)
class SolverConsideration:
    """One solver's compatibility decision recorded during routing."""

    solver_id: str
    solver_version: str | None
    solver_fingerprint: str | None
    supported: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "solver_id", identifier(self.solver_id, context="solver_id"))
        if self.solver_version is not None:
            object.__setattr__(
                self,
                "solver_version",
                identifier(self.solver_version, context="solver_version"),
            )
        if self.solver_fingerprint is not None:
            object.__setattr__(
                self,
                "solver_fingerprint",
                digest(self.solver_fingerprint, context="solver_fingerprint"),
            )
        support = SolverSupport(self.supported, self.reason_codes)
        object.__setattr__(self, "supported", support.supported)
        object.__setattr__(self, "reason_codes", support.reason_codes)
        if (self.solver_version is None) != (self.solver_fingerprint is None):
            raise ValueError(
                "solver version and fingerprint must either both exist or both be absent"
            )
        if self.solver_version is None and self.supported:
            raise ValueError("an unregistered solver cannot be supported")

    def as_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "solver_version": self.solver_version,
            "solver_fingerprint": self.solver_fingerprint,
            "supported": self.supported,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SolverConsideration:
        strict_keys(
            value,
            required={
                "solver_id",
                "solver_version",
                "solver_fingerprint",
                "supported",
                "reason_codes",
            },
            context="solver consideration",
        )
        version = value["solver_version"]
        fingerprint = value["solver_fingerprint"]
        return cls(
            solver_id=identifier(value["solver_id"], context="solver_id"),
            solver_version=(
                None if version is None else identifier(version, context="solver_version")
            ),
            solver_fingerprint=(
                None if fingerprint is None else digest(fingerprint, context="solver_fingerprint")
            ),
            supported=_boolean(value["supported"], context="supported"),
            reason_codes=tuple(
                identifier(item, context="reason_code")
                for item in as_sequence(value["reason_codes"], context="reason_codes")
            ),
        )


@dataclass(frozen=True)
class SolverRoutingDecision:
    """Serializable selected or unsupported route; it never contains a plugin object."""

    schema_version: str
    routing_version: str
    status: RoutingStatus
    features_fingerprint: str
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    trusted_override: str | None
    selected_solver_id: str | None
    selected_solver_version: str | None
    selected_solver_fingerprint: str | None
    considerations: tuple[SolverConsideration, ...]
    reason_code: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(
            self,
            "routing_version",
            identifier(self.routing_version, context="routing_version"),
        )
        if self.status not in {"selected", "unsupported"}:
            raise ValueError("unsupported solver routing status")
        object.__setattr__(
            self,
            "features_fingerprint",
            digest(self.features_fingerprint, context="features_fingerprint"),
        )
        object.__setattr__(self, "policy_id", identifier(self.policy_id, context="policy_id"))
        object.__setattr__(
            self,
            "policy_version",
            identifier(self.policy_version, context="policy_version"),
        )
        object.__setattr__(
            self,
            "policy_fingerprint",
            digest(self.policy_fingerprint, context="policy_fingerprint"),
        )
        if self.trusted_override is not None:
            object.__setattr__(
                self,
                "trusted_override",
                identifier(self.trusted_override, context="trusted_override"),
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
        considerations = tuple(self.considerations)
        if not considerations:
            raise ValueError("routing decision must record at least one consideration")
        if any(not isinstance(value, SolverConsideration) for value in considerations):
            raise TypeError("considerations must contain SolverConsideration values")
        ids = tuple(value.solver_id for value in considerations)
        if len(ids) != len(set(ids)):
            raise ValueError("routing considerations must contain unique solver ids")
        object.__setattr__(self, "considerations", considerations)
        object.__setattr__(
            self,
            "reason_code",
            identifier(self.reason_code, context="reason_code"),
        )
        if self.status == "selected":
            if self.selected_solver_id is None:
                raise ValueError("selected route requires a solver")
            if not any(
                item.solver_id == self.selected_solver_id and item.supported
                for item in considerations
            ):
                raise ValueError("selected solver must have a supported consideration")
        elif any(value is not None for value in selected):
            raise ValueError("unsupported route cannot contain a selected solver")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "routing_version": self.routing_version,
            "status": self.status,
            "features_fingerprint": self.features_fingerprint,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "trusted_override": self.trusted_override,
            "selected_solver_id": self.selected_solver_id,
            "selected_solver_version": self.selected_solver_version,
            "selected_solver_fingerprint": self.selected_solver_fingerprint,
            "considerations": [item.as_dict() for item in self.considerations],
            "reason_code": self.reason_code,
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
                "features_fingerprint",
                "policy_id",
                "policy_version",
                "policy_fingerprint",
                "trusted_override",
                "selected_solver_id",
                "selected_solver_version",
                "selected_solver_fingerprint",
                "considerations",
                "reason_code",
            },
            context="solver routing decision",
        )
        status = value["status"]
        if status not in {"selected", "unsupported"}:
            raise ValueError("unsupported solver routing status")
        trusted_override = value["trusted_override"]
        selected_id = value["selected_solver_id"]
        selected_version = value["selected_solver_version"]
        selected_fingerprint = value["selected_solver_fingerprint"]
        return cls(
            schema_version=text(value["schema_version"], context="schema_version"),
            routing_version=identifier(value["routing_version"], context="routing_version"),
            status=status,
            features_fingerprint=digest(
                value["features_fingerprint"], context="features_fingerprint"
            ),
            policy_id=identifier(value["policy_id"], context="policy_id"),
            policy_version=identifier(value["policy_version"], context="policy_version"),
            policy_fingerprint=digest(value["policy_fingerprint"], context="policy_fingerprint"),
            trusted_override=(
                None
                if trusted_override is None
                else identifier(trusted_override, context="trusted_override")
            ),
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
            considerations=tuple(
                SolverConsideration.from_mapping(as_mapping(item, context="solver consideration"))
                for item in as_sequence(value["considerations"], context="considerations")
            ),
            reason_code=identifier(value["reason_code"], context="reason_code"),
        )
