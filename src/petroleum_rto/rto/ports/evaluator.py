"""Evaluator callback consumed by deterministic RTO search and final selection."""

from __future__ import annotations

from typing import Protocol

from ..contracts import CandidateEvaluationV1, CandidateProposalV1


class CandidateEvaluatorPort(Protocol):
    def evaluate(self, proposal: CandidateProposalV1) -> CandidateEvaluationV1: ...
