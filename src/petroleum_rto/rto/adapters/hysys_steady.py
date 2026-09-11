"""HYSYS-specific IO for serial MV scenario comparisons."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from petroleum_rto.simulation.baseline import (
    _file_bytes,
    _json,
    _plain_directory,
    _sha256,
    _stable,
    capture_baseline,
    read_baseline,
)
from petroleum_rto.simulation.boundary import DEFAULT_BOUNDARY
from petroleum_rto.simulation.hysys import DEFAULT_CATALOG, catalog_from_dict, read_current_snapshot
from petroleum_rto.simulation.models import OperatingSnapshot
from petroleum_rto.simulation.mv import parse_changes
from petroleum_rto.simulation.point import run_mv_point, run_t39_point
from petroleum_rto.simulation.point_evidence import (
    SPECIFICATION,
    _bindings_match,
    read_point_result,
)


def read_json(path: Path) -> Any:
    return _json(_file_bytes(path))


def file_hash(path: Path) -> str:
    return _sha256(_file_bytes(path))


def plain_directory(path: Path) -> None:
    _plain_directory(path)


def definition_hash() -> str:
    return file_hash(DEFAULT_BOUNDARY)


def validate_context(value: Any) -> OperatingSnapshot:
    snapshot = OperatingSnapshot.from_dict(value)
    if snapshot.case_id != "mjh_atm" or not _stable(snapshot) or not snapshot.solver.can_solve:
        raise ValueError("A stable mjh_atm observation is required")
    return snapshot


def read_catalog() -> dict[str, Any]:
    value = read_json(DEFAULT_CATALOG)
    catalog_from_dict(value)
    return dict(value)


def validate_changes(context: Any, changes: Any, catalog: Any) -> list[dict[str, Any]]:
    snapshot = validate_context(context)
    bindings = catalog_from_dict(catalog)
    if not _bindings_match(bindings, snapshot):
        raise ValueError("Observation does not match the MV catalog")
    return [c.to_dict() for c in parse_changes(changes, snapshot, bindings)]


def capabilities() -> list[dict[str, Any]]:
    return [
        {k: b[k] for k in ("variable_id", "object_name", "property_name", "quantity_type", "unit")}
        for b in read_catalog()["variables"]
        if b["role"] == "mv"
    ]


def read_context(workspace: Path) -> dict[str, Any]:
    value = read_current_snapshot(workspace / "hysys/mjh_ATM.hsc").to_dict()
    validate_context(value)
    return value


def capture(context: dict[str, Any], directory: Path, workspace: Path) -> None:
    expected = validate_context(context)
    source = workspace / "hysys/mjh_ATM.hsc"
    if Path(expected.source_case_path).resolve() != source.resolve():
        raise ValueError("The approved source is not the configured project case")
    capture_baseline(source, directory)
    validate_baseline(context, directory)


def validate_baseline(
    context: dict[str, Any], directory: Path, catalog: dict[str, Any] | None = None
) -> str:
    expected = validate_context(context)
    baseline = read_baseline(directory / "manifest.json")
    if replace(baseline.snapshot, observed_at_utc=expected.observed_at_utc) != expected:
        raise ValueError("Source observation changed after preparation; read and prepare again")
    if catalog is not None and read_json(baseline.catalog_path) != catalog:
        raise ValueError("Baseline catalog differs from the approved plan")
    return baseline.baseline_sha256


def run_point(directory: Path, target: float | list[dict[str, Any]], output: Path) -> None:
    if isinstance(target, list):
        run_mv_point(directory / "manifest.json", target, output)
    else:
        run_t39_point(directory / "manifest.json", target, output)


def point_summary(
    directory: Path, baseline_dir: Path, target: float | list[dict[str, Any]], boundary_hash: str
) -> dict[str, Any]:
    baseline = read_baseline(baseline_dir / "manifest.json")
    point = read_point_result(directory / "report.json")
    report = read_json(directory / "report.json")
    if (
        (report["schema_id"], report["schema_version"])
        != (
            ("hysys-mv-point", "1.0.0")
            if isinstance(target, list)
            else ("hysys-t39-point", "2.0.0")
        )
        or point.baseline != baseline.snapshot
        or point.baseline_sha256 != baseline.baseline_sha256
        or ([c.to_dict() for c in point.changes] if isinstance(target, list) else point.target_c)
        != target
        or report["runtime_observations"]["baseline_manifest_sha256"]
        != file_hash(baseline_dir / "manifest.json")
        or file_hash(directory / "variables.json") != file_hash(baseline.catalog_path)
        or file_hash(directory / "boundary_definition.json") != boundary_hash
    ):
        raise ValueError("Point evidence is not bound to the approved comparison")
    metrics: list[dict[str, Any]] = []
    if point.status == "passed" and point.boundary is not None:
        sample = point.boundary.first
        for item in sample.materials:
            metrics.append(
                {
                    "metric_id": "mass_flow:" + item.name,
                    "label": item.name + "物流总流量",
                    "unit": "kg/h",
                    "value": item.mass_flow_kg_h,
                }
            )
        for direction, label in (("in", "边界能量输入"), ("out", "边界能量输出")):
            metrics.append(
                {
                    "metric_id": "energy:" + direction,
                    "label": label,
                    "unit": "MW",
                    "value": sum(
                        x.heat_flow_kJ_h for x in sample.energy if x.direction == direction
                    )
                    / 3_600_000,
                }
            )
        net = sum(x.heat_flow_kJ_h * (1 if x.direction == "in" else -1) for x in sample.energy)
        rise = sum(x.heat_flow_kJ_h * (1 if x.direction == "out" else -1) for x in sample.materials)
        metrics.append(
            {
                "metric_id": "energy:residual",
                "label": "能量衡算余额",
                "unit": "kW",
                "value": (net - rise) / 3600,
            }
        )
    actual = None
    if point.changed is not None and not isinstance(target, list):
        actual = next(x.current for x in point.changed.specifications if x.name == SPECIFICATION)
    return {
        "status": point.status,
        **(
            {
                "changes": target,
                "responses": report["checks"]["target_response"]["variables"]
                if report["checks"]["target_response"] is not None
                else [],
            }
            if isinstance(target, list)
            else {"target_c": target, "actual_c": actual}
        ),
        "metrics": metrics,
        "errors": [
            {"stage": x.stage, "phase": x.phase, "hresult": x.hresult, "scode": x.scode}
            for x in point.errors
        ],
    }
