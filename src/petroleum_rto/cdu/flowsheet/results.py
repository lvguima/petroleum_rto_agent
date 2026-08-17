"""Result contracts for steady open-loop and recycle flowsheets."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..core.types import BalanceReport, MaterialStream, SimulationResult, UnitResult

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset({"success", "failed", "not_converged", "rejected"})


@dataclass(frozen=True)
class SteadyFlowsheetResult:
    """Traceable steady-state flowsheet result with unit-level evidence."""

    status: str
    streams: Mapping[str, MaterialStream]
    products: Mapping[str, MaterialStream]
    unit_results: Mapping[str, UnitResult]
    qualities: Mapping[str, Mapping[str, float]]
    balance: BalanceReport
    diagnostics: Mapping[str, float]
    versions: Mapping[str, str]
    input_fingerprint: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported flowsheet status: {self.status!r}")
        for context, values, expected_type in (
            ("streams", self.streams, MaterialStream),
            ("products", self.products, MaterialStream),
            ("unit_results", self.unit_results, UnitResult),
        ):
            if any(
                not isinstance(key, str) or not isinstance(value, expected_type)
                for key, value in values.items()
            ):
                raise TypeError(f"{context} contains an invalid key or value")
        frozen_qualities: dict[str, Mapping[str, float]] = {}
        for product, indicators in self.qualities.items():
            if not isinstance(product, str):
                raise TypeError("quality product names must be strings")
            copied = dict(indicators)
            if any(
                not isinstance(name, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for name, value in copied.items()
            ):
                raise TypeError("quality indicators must map names to finite numbers")
            frozen_qualities[product] = MappingProxyType(copied)
        if any(
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for name, value in self.diagnostics.items()
        ):
            raise TypeError("flowsheet diagnostics must map names to finite numbers")
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in self.versions.items()
        ):
            raise TypeError("flowsheet versions must map string names to string values")
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise TypeError("flowsheet warnings must be strings")
        if not _SHA256_PATTERN.fullmatch(self.input_fingerprint):
            raise ValueError("input_fingerprint must be a lowercase SHA-256 digest")
        object.__setattr__(self, "streams", MappingProxyType(dict(self.streams)))
        object.__setattr__(self, "products", MappingProxyType(dict(self.products)))
        object.__setattr__(self, "unit_results", MappingProxyType(dict(self.unit_results)))
        object.__setattr__(self, "qualities", MappingProxyType(frozen_qualities))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def as_simulation_result(self) -> SimulationResult:
        return SimulationResult(
            status=self.status,
            streams=self.streams,
            balance=self.balance,
            metrics=self.diagnostics,
            versions=self.versions,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "streams": {name: stream.as_dict() for name, stream in self.streams.items()},
            "products": {name: stream.as_dict() for name, stream in self.products.items()},
            "unit_results": {
                name: result.as_dict() for name, result in self.unit_results.items()
            },
            "qualities": {
                product: dict(indicators) for product, indicators in self.qualities.items()
            },
            "balance": self.balance.as_dict(),
            "diagnostics": dict(self.diagnostics),
            "versions": dict(self.versions),
            "input_fingerprint": self.input_fingerprint,
            "warnings": list(self.warnings),
        }
