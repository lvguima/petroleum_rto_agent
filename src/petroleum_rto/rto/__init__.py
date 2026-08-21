"""Objective-count-neutral offline RTO public surface with explicit legacy readers."""

from .capabilities import (
    BundleCapabilityView,
    CapabilityCatalog,
    ContextSchema,
    SystemPolicy,
    UnifiedCapabilityBundle,
    build_public_capability_manifest,
    load_capability_bundle,
)
from .catalogs import RtoCatalogBundle as LegacyRtoCatalogBundleV1
from .catalogs import RtoCatalogBundleV2 as LegacyRtoCatalogBundleV2
from .catalogs import load_rto_v1_bundle, load_rto_v2_bundle
from .compilation import (
    CandidatePlanCompiler as LegacyCandidatePlanCompilerV1,
)
from .compilation import (
    MultiObjectiveCandidatePlanCompiler as LegacyCandidatePlanCompilerV2,
)
from .compilation import UnifiedCandidatePlanCompiler
from .compilation import UnifiedCandidatePlanCompiler as CandidatePlanCompiler
from .context import load_operating_context
from .contracts import (
    CandidateEvaluation,
    CandidateEvaluationV1,
    CandidateEvaluationV2,
    CandidateProposal,
    CandidateProposalV1,
    CandidateProposalV2,
    ContractRef,
    OperatingContext,
    OperatingContextV1,
    OptimizationProblem,
    OptimizationProblemV1,
    OptimizationProblemV2,
    OptimizationResultV1,
    OptimizationResultV2,
    ParetoSearchResultV2,
    PreferenceSelectionV2,
    SimulationEvaluationRequestV1,
    SimulationPreviewV1,
    SimulationRunBundleV1,
    SolverResult,
    StaticSearchResultV1,
)
from .evaluation import (
    UnifiedM2EvaluationService,
    UnifiedM2PairedEvaluator,
    UnifiedM4EvaluationService,
    UnifiedM4PairedEvaluator,
)
from .inputs import (
    BoundExternalOptimizationRequestV1,
    BoundExternalOptimizationRequestV2,
    DomainOptimizationIntentV2,
    ExternalOperatingContextInputV1,
    ExternalOptimizationIntentInputV1,
)
from .inputs import (
    ExternalOptimizationRequestV1 as LegacyExternalOptimizationRequestV1,
)
from .inputs import (
    ExternalOptimizationRequestV2 as LegacyExternalOptimizationRequestV2,
)
from .inputs import (
    bind_external_optimization_request as bind_legacy_external_optimization_request_v1,
)
from .inputs import (
    bind_external_optimization_request_v2 as bind_legacy_external_optimization_request_v2,
)
from .inputs import (
    capability_manifest_v2 as legacy_capability_manifest_v2,
)
from .inputs import (
    load_domain_optimization_intent_v2 as load_legacy_domain_optimization_intent_v2,
)
from .inputs import (
    load_external_optimization_request as load_legacy_external_optimization_request_v1,
)
from .inputs import (
    load_external_optimization_request_v2 as load_legacy_external_optimization_request_v2,
)
from .inputs import (
    validate_domain_intent_v2 as validate_legacy_domain_intent_v2,
)
from .orchestration import (
    AnchorAttempt,
    AnchorAttemptV1,
    AnchorAttemptV2,
    AnchorValidationResult,
    AnchorValidationResultV1,
    AnchorValidationResultV2,
    LegacyOfflineRtoOrchestratorV1,
    LegacyOfflineRtoRunRecordV1,
    OfflineRtoManifest,
    OfflineRtoManifestV1,
    OfflineRtoManifestV2,
    OfflineRtoOrchestrator,
    OfflineRtoRequest,
    OfflineRtoRequestV1,
    OfflineRtoRequestV2,
    OfflineRtoResult,
    OfflineRtoResultV1,
    OfflineRtoResultV2,
    OfflineRtoRunRecord,
    SolverExecutionArtifact,
    WorkflowEvent,
    WorkflowEventV1,
    WorkflowEventV2,
    read_legacy_offline_run_v1,
    read_offline_run,
)
from .orchestration import (
    OfflineRtoOrchestratorV2 as LegacyOfflineRtoOrchestratorV2,
)
from .orchestration import (
    OfflineRtoRunRecordV2 as LegacyOfflineRtoRunRecordV2,
)
from .orchestration import (
    read_offline_run_v2 as read_legacy_offline_run_v2,
)
from .ports.unified import UnifiedProviderRequestFactory, UnifiedSimulatorPort
from .problem import (
    MultiObjectiveProblemBuilder as LegacyProblemBuilderV2,
)
from .problem import ProblemBuilder as LegacyProblemBuilderV1
from .problem import ProblemFeatureAnalyzer
from .problem import UnifiedProblemBuilder as ProblemBuilder
from .selection import FinalizationArtifacts, PublishabilityAssessor, UnifiedFinalSelector
from .solvers import (
    CandidateEvaluatorPort,
    CoarseRefineGridSolver,
    FullGridParetoSolver,
    ProblemFeatures,
    SolverPort,
    SolverRegistry,
    SolverRouter,
    SolverRoutingDecision,
    SolverRoutingPolicy,
)
from .strategies import StrategyBuilder as LegacyStrategyBuilderV1
from .strategies import StrategyRepository as LegacyStrategyRepositoryV1
from .strategies.unified import (
    StrategyAnchor,
    StrategyBuilder,
    StrategyEntry,
    StrategyLifecycleEvent,
    StrategyQuery,
    StrategyRecord,
    StrategyReleaseManifest,
    StrategyRepository,
    anchor_from_finalization,
    anchor_from_verified_candidate,
)
from .unified_inputs import (
    IntentResolution,
    IntentResolver,
    OptimizationIntent,
    load_optimization_intent,
)

__all__ = [
    "AnchorAttempt",
    # Frozen legacy artifact contract types remain explicit by version.
    "AnchorAttemptV1",
    "AnchorAttemptV2",
    "AnchorValidationResult",
    "AnchorValidationResultV1",
    "AnchorValidationResultV2",
    "BoundExternalOptimizationRequestV1",
    "BoundExternalOptimizationRequestV2",
    "BundleCapabilityView",
    "CandidateEvaluation",
    "CandidateEvaluationV1",
    "CandidateEvaluationV2",
    "CandidateEvaluatorPort",
    "CandidatePlanCompiler",
    "CandidateProposal",
    "CandidateProposalV1",
    "CandidateProposalV2",
    "CapabilityCatalog",
    "CoarseRefineGridSolver",
    "ContextSchema",
    "ContractRef",
    "DomainOptimizationIntentV2",
    "ExternalOperatingContextInputV1",
    "ExternalOptimizationIntentInputV1",
    "FinalizationArtifacts",
    "FullGridParetoSolver",
    "IntentResolution",
    "IntentResolver",
    "LegacyCandidatePlanCompilerV1",
    "LegacyCandidatePlanCompilerV2",
    "LegacyExternalOptimizationRequestV1",
    "LegacyExternalOptimizationRequestV2",
    "LegacyOfflineRtoOrchestratorV1",
    "LegacyOfflineRtoOrchestratorV2",
    "LegacyOfflineRtoRunRecordV1",
    "LegacyOfflineRtoRunRecordV2",
    "LegacyProblemBuilderV1",
    "LegacyProblemBuilderV2",
    "LegacyRtoCatalogBundleV1",
    "LegacyRtoCatalogBundleV2",
    "LegacyStrategyBuilderV1",
    "LegacyStrategyRepositoryV1",
    "OfflineRtoManifest",
    "OfflineRtoManifestV1",
    "OfflineRtoManifestV2",
    "OfflineRtoOrchestrator",
    "OfflineRtoRequest",
    "OfflineRtoRequestV1",
    "OfflineRtoRequestV2",
    "OfflineRtoResult",
    "OfflineRtoResultV1",
    "OfflineRtoResultV2",
    "OfflineRtoRunRecord",
    "OperatingContext",
    "OperatingContextV1",
    "OptimizationIntent",
    "OptimizationProblem",
    "OptimizationProblemV1",
    "OptimizationProblemV2",
    "OptimizationResultV1",
    "OptimizationResultV2",
    "ParetoSearchResultV2",
    "PreferenceSelectionV2",
    "ProblemBuilder",
    "ProblemFeatureAnalyzer",
    "ProblemFeatures",
    "PublishabilityAssessor",
    "SimulationEvaluationRequestV1",
    "SimulationPreviewV1",
    "SimulationRunBundleV1",
    "SolverExecutionArtifact",
    "SolverPort",
    "SolverRegistry",
    "SolverResult",
    "SolverRouter",
    "SolverRoutingDecision",
    "SolverRoutingPolicy",
    "StaticSearchResultV1",
    "StrategyAnchor",
    "StrategyBuilder",
    "StrategyEntry",
    "StrategyLifecycleEvent",
    "StrategyQuery",
    "StrategyRecord",
    "StrategyReleaseManifest",
    "StrategyRepository",
    "SystemPolicy",
    "UnifiedCandidatePlanCompiler",
    "UnifiedCapabilityBundle",
    "UnifiedFinalSelector",
    "UnifiedM2EvaluationService",
    "UnifiedM2PairedEvaluator",
    "UnifiedM4EvaluationService",
    "UnifiedM4PairedEvaluator",
    "UnifiedProviderRequestFactory",
    "UnifiedSimulatorPort",
    "WorkflowEvent",
    "WorkflowEventV1",
    "WorkflowEventV2",
    "anchor_from_finalization",
    "anchor_from_verified_candidate",
    "bind_legacy_external_optimization_request_v1",
    "bind_legacy_external_optimization_request_v2",
    "build_public_capability_manifest",
    "legacy_capability_manifest_v2",
    "load_capability_bundle",
    "load_legacy_domain_optimization_intent_v2",
    "load_legacy_external_optimization_request_v1",
    "load_legacy_external_optimization_request_v2",
    "load_operating_context",
    "load_optimization_intent",
    "load_rto_v1_bundle",
    "load_rto_v2_bundle",
    "read_legacy_offline_run_v1",
    "read_legacy_offline_run_v2",
    "read_offline_run",
    "validate_legacy_domain_intent_v2",
]
