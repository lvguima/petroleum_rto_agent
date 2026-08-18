from petroleum_rto.cdu import calibration


def test_m5_public_api_exports_stable_entry_points() -> None:
    expected = {
        "AlignmentConfig",
        "CalibrationConfig",
        "CalibrationResult",
        "FlowEstimateInput",
        "M5ArtifactManifest",
        "M5PipelineError",
        "M5PipelineResult",
        "Observation",
        "ReconciliationResult",
        "load_alignment_config",
        "load_calibration_config",
        "load_observation_catalog",
        "reconcile_boundary_flows",
        "run_calibration",
        "run_m5_pipeline",
        "write_m5_artifacts",
    }

    assert expected <= set(calibration.__all__)
    for name in expected:
        assert getattr(calibration, name) is not None
