"""Objective-count-neutral names for the existing simulation evidence boundary.

The serialized simulation contract never encoded single- versus multi-objective
semantics. These aliases let the unified RTO core use that physical boundary
without carrying the historical optimization-mode suffix in its public code.
"""

from .models import EvaluationStage as SimulationStage
from .models import SimulationEvaluationRequestV1 as SimulationEvaluationRequest
from .models import SimulationPreviewV1 as SimulationPreview
from .models import SimulationRunBundleV1 as SimulationRunBundle

__all__ = [
    "SimulationEvaluationRequest",
    "SimulationPreview",
    "SimulationRunBundle",
    "SimulationStage",
]
