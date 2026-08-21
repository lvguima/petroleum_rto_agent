"""Explicit in-process registry for solver plugins."""

from __future__ import annotations

from collections.abc import Iterable

from .models import SolverDescriptor
from .port import SolverPort


class SolverRegistry:
    """Register solver instances by immutable ID without dynamic imports or replacement."""

    def __init__(self, solvers: Iterable[SolverPort] = ()) -> None:
        self._solvers: dict[str, SolverPort] = {}
        for solver in solvers:
            self.register(solver)

    def register(self, solver: SolverPort) -> None:
        descriptor = solver.descriptor
        if not isinstance(descriptor, SolverDescriptor):
            raise TypeError("solver descriptor must be a SolverDescriptor")
        if descriptor.solver_id in self._solvers:
            raise ValueError(f"solver id is already registered: {descriptor.solver_id}")
        self._solvers[descriptor.solver_id] = solver

    def get(self, solver_id: str) -> SolverPort:
        try:
            return self._solvers[solver_id]
        except KeyError as exc:
            raise KeyError(f"solver is not registered: {solver_id}") from exc

    def find(self, solver_id: str) -> SolverPort | None:
        return self._solvers.get(solver_id)

    def descriptors(self) -> tuple[SolverDescriptor, ...]:
        return tuple(self._solvers[key].descriptor for key in sorted(self._solvers))

    def __len__(self) -> int:
        return len(self._solvers)
