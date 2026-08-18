from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from petroleum_rto.cdu.control.config import (
    REQUIRED_CONTROL_LOOP_IDS,
    ControlConfig,
    load_control_config,
    validate_control_compatibility,
)
from petroleum_rto.cdu.core.config import (
    ConfigurationError,
    load_case_config,
    load_model_config,
)

CONTROL_CONFIG_PATH = Path("configs/controllers/cdu_pi_v0.1.0.json")
EXPECTED_CONTROL_FINGERPRINT = (
    "ab3f2d0d3787bc5e2a19e5760d3e54812e2ef3e762a32943fe5edddfa2b1294d"
)


def raw_control_config(repo_root: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((repo_root / CONTROL_CONFIG_PATH).read_text(encoding="utf-8")),
    )


def test_control_config_freezes_pairings_acceptance_and_fingerprint(repo_root: Path) -> None:
    control = load_control_config(repo_root / CONTROL_CONFIG_PATH)

    assert control.control_version == "cdu-pi-control-0.1.0"
    assert tuple(control.loops) == REQUIRED_CONTROL_LOOP_IDS
    assert control.loop("feed_flow").controlled_variable.source == "actuator"
    assert control.loop("furnace_temperature").feedforward == "furnace_feed_flow"
    assert control.loop("furnace_temperature").output_max_ratio == 1.15
    assert control.acceptance.band_fraction("feed_flow") == 0.002
    assert control.acceptance.band_fraction("flash_inventory") == 0.005
    assert control.acceptance.recovery_time_s["bottom_inventory"] == 3600.0
    assert control.input_fingerprint == EXPECTED_CONTROL_FINGERPRINT
    assert ControlConfig.from_mapping(control.as_dict()).input_fingerprint == (
        EXPECTED_CONTROL_FINGERPRINT
    )
    assert control.loop("top_pressure").controller_spec().signed_proportional_gain == -8.0

    with pytest.raises(TypeError):
        control.loops["feed_flow"] = control.loop("feed_flow")  # type: ignore[index]
    with pytest.raises(TypeError):
        control.acceptance.recovery_time_s["feed_flow"] = 1.0  # type: ignore[index]
    with pytest.raises(KeyError, match="unknown control loop"):
        control.loop("unknown")


def test_control_config_is_compatible_with_the_frozen_m3_basis(repo_root: Path) -> None:
    control = load_control_config(repo_root / CONTROL_CONFIG_PATH)
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")

    validate_control_compatibility(control, model, case)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda raw: raw.__setitem__("unknown", 1), "fields differ"),
        (lambda raw: raw["loops"].pop("feed_flow"), "loop ids differ"),
        (
            lambda raw: raw["loops"]["top_pressure"].__setitem__(
                "manipulated_variable",
                "gasoline_draw_kg_s",
            ),
            "pairing whitelist",
        ),
        (
            lambda raw: raw["loops"]["feed_flow"].__setitem__(
                "output_max_ratio",
                0.9,
            ),
            "strictly bracket nominal",
        ),
        (
            lambda raw: raw["loops"]["feed_flow"].__setitem__(
                "output_min_ratio",
                1.0,
            ),
            "strictly bracket nominal",
        ),
        (
            lambda raw: raw["loops"]["feed_flow"].__setitem__(
                "output_max_ratio",
                1.0,
            ),
            "strictly bracket nominal",
        ),
        (
            lambda raw: raw["acceptance"]["recovery_time_s"].pop(
                "top_temperature"
            ),
            "seven control loops",
        ),
        (
            lambda raw: raw["metadata"].__setitem__("synthetic", "false"),
            "synthetic",
        ),
    ],
)
def test_control_config_strictly_rejects_invalid_mutations(
    repo_root: Path,
    mutation: Callable[[dict[str, Any]], object],
    match: str,
) -> None:
    raw = copy.deepcopy(raw_control_config(repo_root))
    mutation(raw)
    with pytest.raises(ConfigurationError, match=match):
        ControlConfig.from_mapping(raw)


def test_control_compatibility_rejects_wrong_tuning_case(repo_root: Path) -> None:
    raw = raw_control_config(repo_root)
    raw["tuning_basis_case_version"] = "case-other-v0.1.0"
    control = ControlConfig.from_mapping(raw)
    model = load_model_config(repo_root / "configs/models/cdu_mini_v0.1.0.json")
    case = load_case_config(repo_root / "configs/cases/case_20260604.json")

    with pytest.raises(ConfigurationError, match="tuning_basis_case_version"):
        validate_control_compatibility(control, model, case)
