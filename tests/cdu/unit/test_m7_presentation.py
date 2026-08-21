from __future__ import annotations

from pathlib import Path

from petroleum_rto.cdu.runtime.api import run
from petroleum_rto.cdu.runtime.presentation import (
    build_result_summary,
    render_result_summary,
)
from petroleum_rto.cdu.runtime.presets import load_preset


def test_steady_result_summary_contains_useful_process_results(tmp_path: Path) -> None:
    record = run(load_preset("steady-baseline"), output_root=tmp_path)

    summary = build_result_summary(record)
    key = summary["key_results"]
    assert isinstance(key, dict)
    products = key["products"]
    assert isinstance(products, dict)
    assert set(products) == {
        "gasoline",
        "kerosene",
        "light_diesel",
        "heavy_diesel",
        "residue",
    }
    assert products["gasoline"]["mass_flow_t_h"] > 0.0
    assert key["energy"]["furnace_fuel_mw"] > 0.0

    rendered = render_result_summary(record)
    assert "产品 (t/h | 收率%)" in rendered
    assert "能量:" in rendered
    assert "结果指纹" not in rendered
    assert "现场" not in rendered

    verbose = render_result_summary(record, verbose=True)
    assert "有效输入指纹" in verbose
