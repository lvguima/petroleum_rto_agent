"""Transient progress for synchronous RTO calls; never persisted as evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RtoProgress:
    """Counts evaluated search points (M2) or shortlisted candidates (M4).

    M2 has no known total while adaptive refinement is still running. Counts
    describe completed evaluations, including unsuccessful evaluations, rather
    than physical simulator executions or feasible/final recommendations.
    """

    stage: Literal["m2", "m4"]
    status: Literal["started", "progress", "completed", "reused", "no_feasible", "error"]
    completed: int | None = None
    total: int | None = None


RtoProgressCallback = Callable[[RtoProgress], None]
