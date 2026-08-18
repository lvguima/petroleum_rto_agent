"""Deterministic one-boundary mass-flow reconciliation for the M5 base case.

The first M5 reconciliation deliberately contains one equality only.  Fresh
feed and wash water are the two net inlets; the eight named products are the
net outlets.  Reflux and pump-around measurements may be retained for
traceability, but they are explicit internal flows and never enter the
boundary equation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast

from ..core.config import canonical_fingerprint

FlowDirection = Literal["inlet", "outlet", "internal"]
BoundaryDirection = Literal["inlet", "outlet"]
EstimateBasis = Literal["measurement", "latent_prior"]

INLET_STREAM_IDS: Final[tuple[str, ...]] = ("fresh_feed", "wash_water")
OUTLET_STREAM_IDS: Final[tuple[str, ...]] = (
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
    "offgas",
    "aqueous",
    "brine",
)
BOUNDARY_STREAM_IDS: Final[tuple[str, ...]] = INLET_STREAM_IDS + OUTLET_STREAM_IDS
INTERNAL_STREAM_IDS: Final[tuple[str, ...]] = (
    "reflux",
    "top_circulation",
    "pump_around_1",
    "pump_around_2",
    "pump_around_3",
)
REQUIRED_VERSION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model_version",
        "parameter_set_version",
        "case_version",
        "observation_catalog_version",
        "reconciliation_config_version",
    }
)
RECONCILIATION_ALGORITHM_VERSION: Final[str] = "m5-equality-wls-v0.1.0"
MASS_BALANCE_EQUATION: Final[str] = (
    "fresh_feed + wash_water = gasoline + kerosene + light_diesel + "
    "heavy_diesel + residue + offgas + aqueous + brine"
)
DATA_ORIGIN: Final[str] = "M5_reconciled_field_observations"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BOUNDARY_DIRECTIONS: Final[Mapping[str, BoundaryDirection]] = MappingProxyType(
    {
        **{stream_id: "inlet" for stream_id in INLET_STREAM_IDS},
        **{stream_id: "outlet" for stream_id in OUTLET_STREAM_IDS},
    }
)


def _finite_number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} must be a non-boolean number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def _nonnegative_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number < 0.0:
        raise ValueError(f"{context} must be non-negative")
    return number


def _positive_number(value: object, *, context: str) -> float:
    number = _finite_number(value, context=context)
    if number <= 0.0:
        raise ValueError(f"{context} must be positive")
    return number


def _stream_id(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("stream_id must be a non-empty identifier")
    return value


def _source_refs(value: object, *, context: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{context} must be a sequence of strings")
    refs = tuple(value)
    if not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError(f"{context} must contain non-empty strings")
    if len(set(refs)) != len(refs):
        raise ValueError(f"{context} must not contain duplicates")
    return cast(tuple[str, ...], refs)


def _validate_sha256(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _validated_versions(versions: Mapping[str, str]) -> Mapping[str, str]:
    copied = dict(versions)
    if set(copied) != set(REQUIRED_VERSION_KEYS):
        missing = sorted(REQUIRED_VERSION_KEYS - set(copied))
        unknown = sorted(set(copied) - REQUIRED_VERSION_KEYS)
        raise ValueError(
            "reconciliation versions differ from the fixed contract; "
            f"missing={missing}, unknown={unknown}"
        )
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        for key, value in copied.items()
    ):
        raise ValueError("reconciliation versions must map fixed names to identifiers")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class FlowEstimateInput:
    """One measured or latent flow retained in the reconciliation input record."""

    stream_id: str
    direction: FlowDirection
    source_refs: tuple[str, ...]
    z_kg_s: float | None = None
    sigma_kg_s: float | None = None
    prior_kg_s: float | None = None
    tau_kg_s: float | None = None
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        stream_id = _stream_id(self.stream_id)
        if self.direction not in ("inlet", "outlet", "internal"):
            raise ValueError("direction must be inlet, outlet, or internal")

        measurement_fields_present = (
            self.z_kg_s is not None,
            self.sigma_kg_s is not None,
        )
        prior_fields_present = (
            self.prior_kg_s is not None,
            self.tau_kg_s is not None,
        )
        if measurement_fields_present[0] != measurement_fields_present[1]:
            raise ValueError("z_kg_s and sigma_kg_s must be supplied together")
        if prior_fields_present[0] != prior_fields_present[1]:
            raise ValueError("prior_kg_s and tau_kg_s must be supplied together")
        has_measurement = all(measurement_fields_present)
        has_prior = all(prior_fields_present)
        if has_measurement == has_prior:
            raise ValueError("supply exactly one of z/sigma or prior/tau")

        z = None
        sigma = None
        prior = None
        tau = None
        if has_measurement:
            z = _nonnegative_number(self.z_kg_s, context=f"{stream_id}.z_kg_s")
            sigma = _positive_number(
                self.sigma_kg_s,
                context=f"{stream_id}.sigma_kg_s",
            )
        else:
            prior = _nonnegative_number(
                self.prior_kg_s,
                context=f"{stream_id}.prior_kg_s",
            )
            tau = _positive_number(self.tau_kg_s, context=f"{stream_id}.tau_kg_s")

        if self.direction == "internal":
            if stream_id not in INTERNAL_STREAM_IDS:
                raise ValueError(f"unsupported internal stream {stream_id!r}")
            if not isinstance(self.exclusion_reason, str) or not self.exclusion_reason.strip():
                raise ValueError("an internal flow requires a non-empty exclusion_reason")
        else:
            expected_direction = _BOUNDARY_DIRECTIONS.get(stream_id)
            if expected_direction is None:
                raise ValueError(f"unsupported boundary stream {stream_id!r}")
            if self.direction != expected_direction:
                raise ValueError(
                    f"{stream_id} must have boundary direction {expected_direction!r}"
                )
            if self.exclusion_reason is not None:
                raise ValueError("a boundary flow cannot have an exclusion_reason")

        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(
            self,
            "source_refs",
            _source_refs(self.source_refs, context=f"{stream_id}.source_refs"),
        )
        object.__setattr__(self, "z_kg_s", z)
        object.__setattr__(self, "sigma_kg_s", sigma)
        object.__setattr__(self, "prior_kg_s", prior)
        object.__setattr__(self, "tau_kg_s", tau)

    @property
    def basis(self) -> EstimateBasis:
        return "measurement" if self.z_kg_s is not None else "latent_prior"

    @property
    def estimate_kg_s(self) -> float:
        value = self.z_kg_s if self.z_kg_s is not None else self.prior_kg_s
        if value is None:  # pragma: no cover - guarded by the strict constructor
            raise RuntimeError("flow estimate has no central value")
        return value

    @property
    def uncertainty_kg_s(self) -> float:
        value = self.sigma_kg_s if self.sigma_kg_s is not None else self.tau_kg_s
        if value is None:  # pragma: no cover - guarded by the strict constructor
            raise RuntimeError("flow estimate has no uncertainty")
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "direction": self.direction,
            "z_kg_s": self.z_kg_s,
            "sigma_kg_s": self.sigma_kg_s,
            "prior_kg_s": self.prior_kg_s,
            "tau_kg_s": self.tau_kg_s,
            "source_refs": list(self.source_refs),
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class ReconciledFlow:
    """One reconciled net-boundary flow and its auditable adjustment."""

    stream_id: str
    direction: BoundaryDirection
    basis: EstimateBasis
    raw_kg_s: float | None
    sigma_kg_s: float | None
    prior_kg_s: float | None
    tau_kg_s: float | None
    reconciled_kg_s: float
    adjustment_kg_s: float
    pull: float
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        stream_id = _stream_id(self.stream_id)
        expected_direction = _BOUNDARY_DIRECTIONS.get(stream_id)
        if expected_direction is None or self.direction != expected_direction:
            raise ValueError("reconciled flow has an invalid boundary direction")
        if self.basis not in ("measurement", "latent_prior"):
            raise ValueError("reconciled flow has an invalid basis")

        if self.basis == "measurement":
            raw = _nonnegative_number(self.raw_kg_s, context=f"{stream_id}.raw_kg_s")
            sigma = _positive_number(
                self.sigma_kg_s,
                context=f"{stream_id}.sigma_kg_s",
            )
            if self.prior_kg_s is not None or self.tau_kg_s is not None:
                raise ValueError("a measured result cannot carry a latent prior")
            prior = None
            tau = None
            reference = raw
            uncertainty = sigma
        else:
            prior = _nonnegative_number(
                self.prior_kg_s,
                context=f"{stream_id}.prior_kg_s",
            )
            tau = _positive_number(self.tau_kg_s, context=f"{stream_id}.tau_kg_s")
            if self.raw_kg_s is not None or self.sigma_kg_s is not None:
                raise ValueError("a latent result cannot carry a raw measurement")
            raw = None
            sigma = None
            reference = prior
            uncertainty = tau

        reconciled = _nonnegative_number(
            self.reconciled_kg_s,
            context=f"{stream_id}.reconciled_kg_s",
        )
        adjustment = _finite_number(
            self.adjustment_kg_s,
            context=f"{stream_id}.adjustment_kg_s",
        )
        pull = _finite_number(self.pull, context=f"{stream_id}.pull")
        tolerance = 1e-12 * max(1.0, reference, reconciled, abs(adjustment))
        if not math.isclose(
            adjustment,
            reconciled - reference,
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("reconciled-flow adjustment does not match its values")
        if not math.isclose(
            pull,
            adjustment / uncertainty,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("reconciled-flow pull does not match its uncertainty")

        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "raw_kg_s", raw)
        object.__setattr__(self, "sigma_kg_s", sigma)
        object.__setattr__(self, "prior_kg_s", prior)
        object.__setattr__(self, "tau_kg_s", tau)
        object.__setattr__(self, "reconciled_kg_s", reconciled)
        object.__setattr__(self, "adjustment_kg_s", adjustment)
        object.__setattr__(self, "pull", pull)
        object.__setattr__(
            self,
            "source_refs",
            _source_refs(self.source_refs, context=f"{stream_id}.source_refs"),
        )

    @property
    def reference_kg_s(self) -> float:
        value = self.raw_kg_s if self.raw_kg_s is not None else self.prior_kg_s
        if value is None:  # pragma: no cover - guarded by the strict constructor
            raise RuntimeError("reconciled flow has no reference")
        return value

    @property
    def normalized_adjustment(self) -> float:
        """Adjustment divided by its measurement or prior engineering scale."""

        return self.pull

    def as_input_dict(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "direction": self.direction,
            "z_kg_s": self.raw_kg_s,
            "sigma_kg_s": self.sigma_kg_s,
            "prior_kg_s": self.prior_kg_s,
            "tau_kg_s": self.tau_kg_s,
            "source_refs": list(self.source_refs),
            "exclusion_reason": None,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.as_input_dict(),
            "basis": self.basis,
            "raw_kg_s": self.raw_kg_s,
            "reconciled_kg_s": self.reconciled_kg_s,
            "adjustment_kg_s": self.adjustment_kg_s,
            "normalized_adjustment": self.normalized_adjustment,
            "pull": self.pull,
        }


class NonNegativeConstraintError(ValueError):
    """Raised when the unconstrained equality solution contains a negative flow."""

    def __init__(
        self,
        violating_candidates_kg_s: Mapping[str, float],
        *,
        pre_reconciliation_residual_kg_s: float,
    ) -> None:
        copied = dict(violating_candidates_kg_s)
        self.violating_candidates_kg_s: Mapping[str, float] = MappingProxyType(copied)
        self.pre_reconciliation_residual_kg_s = pre_reconciliation_residual_kg_s
        details = ", ".join(
            f"{stream_id}={value:.12g}" for stream_id, value in sorted(copied.items())
        )
        super().__init__(
            "equality-only reconciliation violates the non-negative-flow constraint; "
            f"candidates: {details}"
        )


def _boundary_sign(stream_id: str) -> float:
    return 1.0 if stream_id in INLET_STREAM_IDS else -1.0


def _boundary_residual_from_entries(entries: Mapping[str, ReconciledFlow]) -> float:
    return math.fsum(
        _boundary_sign(stream_id) * entries[stream_id].reconciled_kg_s
        for stream_id in BOUNDARY_STREAM_IDS
    )


def _input_payload(
    entries: Mapping[str, ReconciledFlow],
    excluded_internal: Mapping[str, FlowEstimateInput],
    versions: Mapping[str, str],
    observation_fingerprint: str,
) -> dict[str, object]:
    return {
        "algorithm_version": RECONCILIATION_ALGORITHM_VERSION,
        "mass_balance_equation": MASS_BALANCE_EQUATION,
        "inputs": [entries[stream_id].as_input_dict() for stream_id in BOUNDARY_STREAM_IDS]
        + [excluded_internal[stream_id].as_dict() for stream_id in sorted(excluded_internal)],
        "versions": dict(versions),
        "observation_fingerprint": observation_fingerprint,
    }


def _result_payload(
    *,
    entries: Mapping[str, ReconciledFlow],
    excluded_internal: Mapping[str, FlowEstimateInput],
    versions: Mapping[str, str],
    source_refs: tuple[str, ...],
    observation_fingerprint: str,
    input_fingerprint: str,
    pre_reconciliation_residual_kg_s: float,
    post_reconciliation_residual_kg_s: float,
    objective: float,
) -> dict[str, object]:
    return {
        "status": "success",
        "algorithm_version": RECONCILIATION_ALGORITHM_VERSION,
        "mass_balance_equation": MASS_BALANCE_EQUATION,
        "entries": {
            stream_id: entries[stream_id].as_dict()
            for stream_id in BOUNDARY_STREAM_IDS
        },
        "excluded_internal": {
            stream_id: {
                **excluded_internal[stream_id].as_dict(),
                "excluded_from_balance": True,
            }
            for stream_id in sorted(excluded_internal)
        },
        "pre_reconciliation_residual_kg_s": pre_reconciliation_residual_kg_s,
        "post_reconciliation_residual_kg_s": post_reconciliation_residual_kg_s,
        "objective": objective,
        "versions": dict(versions),
        "source_refs": list(source_refs),
        "observation_fingerprint": observation_fingerprint,
        "input_fingerprint": input_fingerprint,
        "synthetic": False,
        "data_origin": DATA_ORIGIN,
    }


@dataclass(frozen=True)
class ReconciliationResult:
    """Successful, self-checking M5 reconciliation result."""

    status: str
    entries: Mapping[str, ReconciledFlow]
    excluded_internal: Mapping[str, FlowEstimateInput]
    pre_reconciliation_residual_kg_s: float
    post_reconciliation_residual_kg_s: float
    objective: float
    versions: Mapping[str, str]
    source_refs: tuple[str, ...]
    observation_fingerprint: str
    input_fingerprint: str
    result_fingerprint: str
    algorithm_version: str = RECONCILIATION_ALGORITHM_VERSION
    synthetic: bool = False
    data_origin: str = DATA_ORIGIN

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("reconciliation result status must be success")
        if self.algorithm_version != RECONCILIATION_ALGORITHM_VERSION:
            raise ValueError("unsupported reconciliation algorithm version")
        if self.synthetic is not False:
            raise ValueError("reconciled field observations cannot be marked synthetic")
        if self.data_origin != DATA_ORIGIN:
            raise ValueError("reconciliation result has an invalid data origin")

        entries = dict(self.entries)
        if set(entries) != set(BOUNDARY_STREAM_IDS) or any(
            not isinstance(value, ReconciledFlow) or key != value.stream_id
            for key, value in entries.items()
        ):
            raise ValueError("reconciliation entries must cover exactly the net boundary")
        excluded = dict(self.excluded_internal)
        if any(
            key not in INTERNAL_STREAM_IDS
            or not isinstance(value, FlowEstimateInput)
            or value.direction != "internal"
            or key != value.stream_id
            for key, value in excluded.items()
        ):
            raise ValueError("excluded_internal contains an invalid internal flow")

        pre_residual = _finite_number(
            self.pre_reconciliation_residual_kg_s,
            context="pre_reconciliation_residual_kg_s",
        )
        post_residual = _finite_number(
            self.post_reconciliation_residual_kg_s,
            context="post_reconciliation_residual_kg_s",
        )
        objective = _nonnegative_number(self.objective, context="objective")
        expected_pre = math.fsum(
            _boundary_sign(stream_id) * entries[stream_id].reference_kg_s
            for stream_id in BOUNDARY_STREAM_IDS
        )
        expected_post = _boundary_residual_from_entries(entries)
        expected_objective = math.fsum(entry.pull * entry.pull for entry in entries.values())
        uncertainty_denominator = math.fsum(
            (
                entry.sigma_kg_s
                if entry.sigma_kg_s is not None
                else cast(float, entry.tau_kg_s)
            )
            ** 2
            for entry in entries.values()
        )
        residual_scale = max(
            1.0,
            *(entry.reference_kg_s for entry in entries.values()),
            *(entry.reconciled_kg_s for entry in entries.values()),
        )
        if not math.isclose(pre_residual, expected_pre, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("pre-reconciliation residual is inconsistent with the inputs")
        if not math.isclose(
            post_residual,
            expected_post,
            rel_tol=1e-12,
            abs_tol=1e-12 * residual_scale,
        ):
            raise ValueError("post-reconciliation residual is inconsistent with the solution")
        if abs(expected_post) > 1e-12 * residual_scale:
            raise ValueError("a successful reconciliation must close the boundary equality")
        if not math.isclose(
            objective,
            expected_objective,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("reconciliation objective is inconsistent with the pulls")
        for stream_id, entry in entries.items():
            uncertainty = (
                entry.sigma_kg_s
                if entry.sigma_kg_s is not None
                else cast(float, entry.tau_kg_s)
            )
            expected_adjustment = (
                -_boundary_sign(stream_id)
                * uncertainty**2
                * pre_residual
                / uncertainty_denominator
            )
            if not math.isclose(
                entry.adjustment_kg_s,
                expected_adjustment,
                rel_tol=1e-12,
                abs_tol=1e-12 * residual_scale,
            ):
                raise ValueError(
                    "reconciliation entries do not satisfy the analytic WLS optimum"
                )

        versions = _validated_versions(self.versions)
        observation_fingerprint = _validate_sha256(
            self.observation_fingerprint,
            context="observation_fingerprint",
        )
        input_fingerprint = _validate_sha256(
            self.input_fingerprint,
            context="input_fingerprint",
        )
        result_fingerprint = _validate_sha256(
            self.result_fingerprint,
            context="result_fingerprint",
        )
        expected_refs = tuple(
            sorted(
                {ref for item in entries.values() for ref in item.source_refs}
                | {ref for item in excluded.values() for ref in item.source_refs}
            )
        )
        if tuple(self.source_refs) != expected_refs:
            raise ValueError("result source_refs must be the sorted union of input references")

        expected_input_fingerprint = canonical_fingerprint(
            _input_payload(entries, excluded, versions, observation_fingerprint)
        )
        if input_fingerprint != expected_input_fingerprint:
            raise ValueError("input_fingerprint does not match reconciliation inputs")
        expected_result_fingerprint = canonical_fingerprint(
            _result_payload(
                entries=entries,
                excluded_internal=excluded,
                versions=versions,
                source_refs=expected_refs,
                observation_fingerprint=observation_fingerprint,
                input_fingerprint=input_fingerprint,
                pre_reconciliation_residual_kg_s=pre_residual,
                post_reconciliation_residual_kg_s=post_residual,
                objective=objective,
            )
        )
        if result_fingerprint != expected_result_fingerprint:
            raise ValueError("result_fingerprint does not match reconciliation result")

        object.__setattr__(self, "entries", MappingProxyType(entries))
        object.__setattr__(self, "excluded_internal", MappingProxyType(excluded))
        object.__setattr__(self, "versions", versions)
        object.__setattr__(self, "source_refs", expected_refs)
        object.__setattr__(self, "observation_fingerprint", observation_fingerprint)
        object.__setattr__(self, "input_fingerprint", input_fingerprint)
        object.__setattr__(self, "result_fingerprint", result_fingerprint)
        object.__setattr__(
            self,
            "pre_reconciliation_residual_kg_s",
            pre_residual,
        )
        object.__setattr__(
            self,
            "post_reconciliation_residual_kg_s",
            post_residual,
        )
        object.__setattr__(self, "objective", objective)

    @property
    def reconciled_values_kg_s(self) -> Mapping[str, float]:
        """Return the ten reconciled boundary values keyed by model stream name."""

        return MappingProxyType(
            {
                stream_id: self.entries[stream_id].reconciled_kg_s
                for stream_id in BOUNDARY_STREAM_IDS
            }
        )

    def as_dict(self) -> dict[str, object]:
        payload = _result_payload(
            entries=self.entries,
            excluded_internal=self.excluded_internal,
            versions=self.versions,
            source_refs=self.source_refs,
            observation_fingerprint=self.observation_fingerprint,
            input_fingerprint=self.input_fingerprint,
            pre_reconciliation_residual_kg_s=self.pre_reconciliation_residual_kg_s,
            post_reconciliation_residual_kg_s=self.post_reconciliation_residual_kg_s,
            objective=self.objective,
        )
        payload["result_fingerprint"] = self.result_fingerprint
        return payload


def reconcile_boundary_flows(
    inputs: Sequence[FlowEstimateInput],
    *,
    versions: Mapping[str, str],
    observation_fingerprint: str,
) -> ReconciliationResult:
    """Solve the first M5 one-equality weighted least-squares problem.

    The objective is ``sum(((x_i - estimate_i) / uncertainty_i) ** 2)``.
    The equality sign convention is inlet minus outlet, so both reported
    residuals are positive for apparent excess inlet flow.
    """

    if isinstance(inputs, (str, bytes, bytearray)) or not isinstance(inputs, Sequence):
        raise TypeError("reconciliation inputs must be a sequence")
    items = tuple(inputs)
    if any(not isinstance(item, FlowEstimateInput) for item in items):
        raise TypeError("reconciliation inputs contain an invalid item")
    if len({item.stream_id for item in items}) != len(items):
        raise ValueError("reconciliation stream_id values must be unique")

    by_id = {item.stream_id: item for item in items}
    if not set(BOUNDARY_STREAM_IDS).issubset(by_id):
        missing = sorted(set(BOUNDARY_STREAM_IDS) - set(by_id))
        raise ValueError(f"reconciliation boundary is incomplete; missing={missing}")
    boundary_inputs = {stream_id: by_id[stream_id] for stream_id in BOUNDARY_STREAM_IDS}
    excluded_internal = {
        stream_id: item for stream_id, item in by_id.items() if item.direction == "internal"
    }
    if len(boundary_inputs) + len(excluded_internal) != len(items):
        raise ValueError("reconciliation inputs contain an unsupported flow")

    validated_versions = _validated_versions(versions)
    validated_observation_fingerprint = _validate_sha256(
        observation_fingerprint,
        context="observation_fingerprint",
    )
    pre_residual = math.fsum(
        _boundary_sign(stream_id) * boundary_inputs[stream_id].estimate_kg_s
        for stream_id in BOUNDARY_STREAM_IDS
    )
    denominator = math.fsum(
        boundary_inputs[stream_id].uncertainty_kg_s**2
        for stream_id in BOUNDARY_STREAM_IDS
    )
    if not math.isfinite(denominator) or denominator <= 0.0:
        raise ValueError("reconciliation uncertainty denominator must be finite and positive")

    entries: dict[str, ReconciledFlow] = {}
    violating_candidates: dict[str, float] = {}
    for stream_id in BOUNDARY_STREAM_IDS:
        item = boundary_inputs[stream_id]
        sign = _boundary_sign(stream_id)
        adjustment = (
            -sign * item.uncertainty_kg_s**2 * pre_residual / denominator
        )
        reconciled = item.estimate_kg_s + adjustment
        if not math.isfinite(reconciled) or not math.isfinite(adjustment):
            raise ValueError("reconciliation produced a non-finite candidate")
        if reconciled < 0.0:
            violating_candidates[stream_id] = reconciled
            continue
        entries[stream_id] = ReconciledFlow(
            stream_id=stream_id,
            direction=cast(BoundaryDirection, item.direction),
            basis=item.basis,
            raw_kg_s=item.z_kg_s,
            sigma_kg_s=item.sigma_kg_s,
            prior_kg_s=item.prior_kg_s,
            tau_kg_s=item.tau_kg_s,
            reconciled_kg_s=reconciled,
            adjustment_kg_s=adjustment,
            pull=adjustment / item.uncertainty_kg_s,
            source_refs=item.source_refs,
        )
    if violating_candidates:
        raise NonNegativeConstraintError(
            violating_candidates,
            pre_reconciliation_residual_kg_s=pre_residual,
        )

    post_residual = _boundary_residual_from_entries(entries)
    objective = math.fsum(entry.pull * entry.pull for entry in entries.values())
    source_refs = tuple(
        sorted({ref for item in items for ref in item.source_refs})
    )
    input_fingerprint = canonical_fingerprint(
        _input_payload(
            entries,
            excluded_internal,
            validated_versions,
            validated_observation_fingerprint,
        )
    )
    result_fingerprint = canonical_fingerprint(
        _result_payload(
            entries=entries,
            excluded_internal=excluded_internal,
            versions=validated_versions,
            source_refs=source_refs,
            observation_fingerprint=validated_observation_fingerprint,
            input_fingerprint=input_fingerprint,
            pre_reconciliation_residual_kg_s=pre_residual,
            post_reconciliation_residual_kg_s=post_residual,
            objective=objective,
        )
    )
    return ReconciliationResult(
        status="success",
        entries=entries,
        excluded_internal=excluded_internal,
        pre_reconciliation_residual_kg_s=pre_residual,
        post_reconciliation_residual_kg_s=post_residual,
        objective=objective,
        versions=validated_versions,
        source_refs=source_refs,
        observation_fingerprint=validated_observation_fingerprint,
        input_fingerprint=input_fingerprint,
        result_fingerprint=result_fingerprint,
    )
