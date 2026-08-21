from __future__ import annotations

from petroleum_rto.cdu import validation


def test_m6_public_api_exports_stable_execution_and_evidence_contracts() -> None:
    required = {
        "M6ValidationConfig",
        "M6ValidationResult",
        "ScenarioValidationResult",
        "run_m6_validation",
        "write_m6_artifacts",
        "verify_m6_artifacts",
        "assess_applicability",
        "run_local_sensitivity",
        "run_protection",
        "verify_controller_tracking",
    }

    assert required <= set(validation.__all__)
    assert all(hasattr(validation, name) for name in validation.__all__)
    assert len(validation.__all__) == len(set(validation.__all__))
