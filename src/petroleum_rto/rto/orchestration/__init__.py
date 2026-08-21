"""Unified offline orchestration with explicit frozen legacy readers."""

from .models import (
    AnchorAttemptV1,
    AnchorValidationResultV1,
    OfflineRtoManifestV1,
    OfflineRtoRequestV1,
    OfflineRtoResultV1,
    WorkflowEventV1,
)
from .service import (
    OfflineRtoOrchestrator as LegacyOfflineRtoOrchestratorV1,
)
from .service import (
    OfflineRtoRunRecord as LegacyOfflineRtoRunRecordV1,
)
from .service import (
    read_offline_run as read_legacy_offline_run_v1,
)
from .unified_models import (
    AnchorAttempt,
    AnchorValidationResult,
    CapabilityBundleSnapshot,
    DynamicVerificationArtifact,
    FinalizationArtifact,
    OfflineRtoManifest,
    OfflineRtoRequest,
    OfflineRtoResult,
    SolverExecutionArtifact,
    WorkflowEvent,
)
from .unified_service import OfflineRtoOrchestrator, OfflineRtoRunRecord, read_offline_run

UnifiedOfflineRtoOrchestrator = OfflineRtoOrchestrator
UnifiedOfflineRtoRunRecord = OfflineRtoRunRecord
read_unified_offline_run = read_offline_run
from .v2_models import (
    AnchorAttemptV2,
    AnchorValidationResultV2,
    OfflineRtoManifestV2,
    OfflineRtoRequestV2,
    OfflineRtoResultV2,
    WorkflowEventV2,
)
from .v2_service import (
    OfflineRtoOrchestratorV2,
    OfflineRtoRunRecordV2,
    read_offline_run_v2,
)

__all__ = [
    "AnchorAttempt",
    "AnchorAttemptV1",
    "AnchorAttemptV2",
    "AnchorValidationResult",
    "AnchorValidationResultV1",
    "AnchorValidationResultV2",
    "CapabilityBundleSnapshot",
    "DynamicVerificationArtifact",
    "FinalizationArtifact",
    "LegacyOfflineRtoOrchestratorV1",
    "LegacyOfflineRtoRunRecordV1",
    "OfflineRtoManifest",
    "OfflineRtoManifestV1",
    "OfflineRtoManifestV2",
    "OfflineRtoOrchestrator",
    "OfflineRtoOrchestratorV2",
    "OfflineRtoRequest",
    "OfflineRtoRequestV1",
    "OfflineRtoRequestV2",
    "OfflineRtoResult",
    "OfflineRtoResultV1",
    "OfflineRtoResultV2",
    "OfflineRtoRunRecord",
    "OfflineRtoRunRecordV2",
    "SolverExecutionArtifact",
    "UnifiedOfflineRtoOrchestrator",
    "UnifiedOfflineRtoRunRecord",
    "WorkflowEvent",
    "WorkflowEventV1",
    "WorkflowEventV2",
    "read_legacy_offline_run_v1",
    "read_offline_run",
    "read_offline_run_v2",
    "read_unified_offline_run",
]
