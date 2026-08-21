"""External structured request contracts and deterministic binding."""

from .adapter import BoundExternalOptimizationRequestV1, bind_external_optimization_request
from .loader import (
    load_domain_optimization_intent_v2,
    load_external_optimization_request,
    load_external_optimization_request_v2,
)
from .models import (
    ExternalOperatingContextInputV1,
    ExternalOptimizationIntentInputV1,
    ExternalOptimizationRequestV1,
)
from .v2_adapter import (
    BoundExternalOptimizationRequestV2,
    bind_external_optimization_request_v2,
    capability_manifest_v2,
    resolve_domain_intent_v2,
    validate_domain_intent_v2,
)
from .v2_models import (
    DomainIntentSourceV2,
    DomainObjectiveRequestV2,
    DomainOptimizationIntentV2,
    DomainSelectionRequestV2,
    ExternalOptimizationRequestV2,
    IntentValidationIssueV2,
    IntentValidationResultV2,
    RtoCapabilityManifestV2,
)

__all__ = [
    "BoundExternalOptimizationRequestV1",
    "BoundExternalOptimizationRequestV2",
    "DomainIntentSourceV2",
    "DomainObjectiveRequestV2",
    "DomainOptimizationIntentV2",
    "DomainSelectionRequestV2",
    "ExternalOperatingContextInputV1",
    "ExternalOptimizationIntentInputV1",
    "ExternalOptimizationRequestV1",
    "ExternalOptimizationRequestV2",
    "IntentValidationIssueV2",
    "IntentValidationResultV2",
    "RtoCapabilityManifestV2",
    "bind_external_optimization_request",
    "bind_external_optimization_request_v2",
    "capability_manifest_v2",
    "load_domain_optimization_intent_v2",
    "load_external_optimization_request",
    "load_external_optimization_request_v2",
    "resolve_domain_intent_v2",
    "validate_domain_intent_v2",
]
