"""Compact result parsing and terminal presentation for M7 runtime records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from .artifacts import RunRecord

_PRODUCTS = ("gasoline", "kerosene", "light_diesel", "heavy_diesel", "residue")


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _stream_summary(stream: object, *, feed_kg_s: float) -> dict[str, float]:
    item = _mapping(stream)
    flow = _number(item.get("mass_flow_kg_s"))
    return {
        "mass_flow_t_h": flow * 3.6,
        "yield_percent_of_feed": 0.0 if feed_kg_s <= 0.0 else flow / feed_kg_s * 100.0,
        "temperature_c": _number(item.get("temperature_k")) - 273.15,
    }


def _steady_summary(record: RunRecord) -> dict[str, object]:
    result = _mapping(record.payload.summary)
    flowsheet = _mapping(result.get("flowsheet"))
    streams = _mapping(flowsheet.get("streams"))
    products = _mapping(flowsheet.get("products"))
    qualities = _mapping(flowsheet.get("qualities"))
    diagnostics = _mapping(flowsheet.get("diagnostics"))
    balance = _mapping(flowsheet.get("balance"))
    feed = _mapping(streams.get("fresh_crude"))
    feed_kg_s = _number(feed.get("mass_flow_kg_s"))
    product_rows: dict[str, object] = {}
    for name in _PRODUCTS:
        row = _stream_summary(products.get(name), feed_kg_s=feed_kg_s)
        quality = _mapping(qualities.get(name))
        row.update(
            {
                "density_kg_m3_proxy": _number(quality.get("density_kg_m3_proxy")),
                "t90_c_proxy": _number(quality.get("t90_k_proxy")) - 273.15,
            }
        )
        product_rows[name] = row
    boundary_rows = {
        name: _stream_summary(products.get(name), feed_kg_s=feed_kg_s)
        for name in ("offgas", "aqueous", "brine")
    }
    return {
        "iterations": result.get("iterations"),
        "final_residual": result.get("final_residual"),
        "feed": {
            "mass_flow_t_h": feed_kg_s * 3.6,
            "temperature_c": _number(feed.get("temperature_k")) - 273.15,
            "pressure_mpa_a": _number(feed.get("pressure_pa")) / 1_000_000.0,
        },
        "products": product_rows,
        "other_boundary_outlets": boundary_rows,
        "energy": {
            "furnace_fuel_mw": _number(diagnostics.get("furnace_fuel_duty_w")) / 1_000_000.0,
            "actual_recovered_mw": _number(diagnostics.get("actual_recovered_duty_w"))
            / 1_000_000.0,
        },
        "conservation": {
            "mass_residual_kg_s": _number(balance.get("residual_kg_s")),
            "maximum_component_residual_kg_s": max(
                (
                    abs(_number(value))
                    for value in _mapping(balance.get("component_residuals_kg_s")).values()
                ),
                default=0.0,
            ),
            "salt_residual_kg_s": _number(balance.get("salt_residual_kg_s")),
        },
    }


def _plant_sample(sample: Mapping[str, object], run_type: str) -> Mapping[str, object]:
    return _mapping(sample.get("plant")) if run_type == "closed_loop_dynamic" else sample


def _dynamic_summary(record: RunRecord) -> dict[str, object]:
    samples = cast(Sequence[Mapping[str, object]], record.payload.timeseries)
    if not samples:
        return {
            "requested_duration_s": record.payload.duration_s,
            "sample_count": 0,
            "events": [event.as_dict() for event in record.payload.events],
        }
    first = _plant_sample(samples[0], record.request.run_type)
    final = _plant_sample(samples[-1], record.request.run_type)
    first_state = _mapping(first.get("state"))
    final_state = _mapping(final.get("state"))
    first_inventories = _mapping(first_state.get("liquid_inventories"))
    final_inventories = _mapping(final_state.get("liquid_inventories"))
    final_sensors = _mapping(final_state.get("sensor_states"))
    final_thermal = _mapping(final_state.get("thermal_states"))
    evaluation = _mapping(final.get("evaluation"))
    streams = _mapping(evaluation.get("stream_mass_flows_kg_s"))
    inventory_rows: dict[str, object] = {}
    for name in ("flash_drum", "reflux_drum", "tower_bottom"):
        initial_mass = _number(_mapping(first_inventories.get(name)).get("total_mass_kg"))
        final_mass = _number(_mapping(final_inventories.get(name)).get("total_mass_kg"))
        inventory_rows[name] = {
            "initial_kg": initial_mass,
            "final_kg": final_mass,
            "final_ratio": 0.0 if initial_mass <= 0.0 else final_mass / initial_mass,
        }
    residue_key = "residue_product"
    products = {
        name: {
            "final_mass_flow_t_h": _number(streams.get(residue_key if name == "residue" else name))
            * 3.6
        }
        for name in _PRODUCTS
    }
    balance_summary = _mapping(record.payload.summary.get("balance"))
    summary: dict[str, object] = {
        "requested_duration_s": record.payload.duration_s,
        "completed_time_s": record.payload.summary.get("completed_time_s"),
        "time_step_s": record.payload.time_step_s,
        "sample_count": len(samples),
        "events": [event.as_dict() for event in record.payload.events],
        "final_process": {
            "furnace_outlet_temperature_c": _number(
                final_thermal.get("furnace_outlet_temperature_k")
            )
            - 273.15,
            "tower_top_temperature_c": _number(final_thermal.get("tower_top_temperature_k"))
            - 273.15,
            "tower_top_pressure_mpa_a": _number(final_sensors.get("tower_top_pressure_pa"))
            / 1_000_000.0,
        },
        "inventories": inventory_rows,
        "products": products,
        "conservation": {
            "mass_residual_kg": _number(balance_summary.get("mass_residual_kg")),
            "maximum_component_residual_kg": _number(
                balance_summary.get("maximum_absolute_component_residual_kg")
            ),
        },
    }
    if record.request.run_type == "closed_loop_dynamic":
        controls = _mapping(samples[-1].get("controls"))
        performance = _mapping(record.payload.summary.get("loop_performance"))
        summary["control_loops"] = {
            loop_id: {
                "target_setpoint": _number(_mapping(item).get("target_setpoint")),
                "process_value": _number(_mapping(item).get("process_value")),
                "error_normalized": _number(_mapping(item).get("error_normalized")),
                "output": _number(_mapping(item).get("output")),
                "saturated": bool(_mapping(item).get("saturated", False)),
                "settling_time_s": _mapping(performance.get(loop_id)).get("settling_time_s"),
            }
            for loop_id, item in controls.items()
        }
        summary["acceptance_passed"] = record.payload.summary.get("acceptance_passed")
    return summary


def build_result_summary(record: RunRecord) -> dict[str, object]:
    """Parse one verified record into a compact stable presentation object."""

    result: dict[str, object] = {
        "runtime_status": record.payload.runtime_status,
        "engine_status": record.payload.engine_status,
        "preset_id": record.request.preset_id,
        "run_type": record.request.run_type,
        "run_id": record.manifest.run_id,
        "run_dir": str(record.run_dir),
    }
    if record.request.run_type == "steady_recycle":
        result["key_results"] = _steady_summary(record)
    elif record.request.run_type in {
        "open_loop_dynamic",
        "closed_loop_dynamic",
    }:
        result["key_results"] = _dynamic_summary(record)
    else:
        result["key_results"] = {
            "scenario": dict(record.payload.summary),
            "event_count": len(record.payload.events),
        }
    if record.payload.runtime_status in {"failed", "not_converged", "rejected"}:
        result["failure"] = {
            "stage": getattr(record.payload, "failure_stage", None),
            "reason": getattr(record.payload, "failure_reason", None),
            "time_s": getattr(record.payload, "failure_time_s", None),
            "last_valid": getattr(record.payload, "last_valid", None),
        }
    result["result_fingerprint"] = record.payload.result_fingerprint
    result["manifest_fingerprint"] = record.manifest.manifest_fingerprint
    return result


def _format_number(value: object, digits: int = 4) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-" if value is None else str(value)
    return f"{float(value):.{digits}f}"


def render_result_summary(record: RunRecord, *, verbose: bool = False) -> str:
    """Render a useful human summary without repeating long scope notices."""

    parsed = build_result_summary(record)
    lines = [
        f"结果: {record.payload.runtime_status} | {record.request.preset_id}",
    ]
    key = _mapping(parsed.get("key_results"))
    if record.request.run_type == "steady_recycle":
        feed = _mapping(key.get("feed"))
        lines.append(
            "进料: "
            f"{_format_number(feed.get('mass_flow_t_h'), 3)} t/h, "
            f"{_format_number(feed.get('temperature_c'), 2)} °C"
        )
        lines.append(
            "产品 (t/h | 收率%): "
            + "; ".join(
                f"{name} {_format_number(_mapping(row).get('mass_flow_t_h'), 3)} | "
                f"{_format_number(_mapping(row).get('yield_percent_of_feed'), 2)}"
                for name, row in _mapping(key.get("products")).items()
            )
        )
        energy = _mapping(key.get("energy"))
        conservation = _mapping(key.get("conservation"))
        lines.append(
            "能量: 燃料 "
            f"{_format_number(energy.get('furnace_fuel_mw'), 3)} MW, 回收 "
            f"{_format_number(energy.get('actual_recovered_mw'), 3)} MW"
        )
        lines.append(
            f"守恒残差: 质量 {_format_number(conservation.get('mass_residual_kg_s'), 3)} kg/s"
        )
    elif record.request.run_type in {"open_loop_dynamic", "closed_loop_dynamic"}:
        lines.append(
            "时间: "
            f"{_format_number(key.get('completed_time_s'), 1)}/"
            f"{_format_number(key.get('requested_duration_s'), 1)} s, "
            f"样本 {key.get('sample_count')}, 事件 {len(record.payload.events)}"
        )
        process = _mapping(key.get("final_process"))
        lines.append(
            "终态: 炉出口 "
            f"{_format_number(process.get('furnace_outlet_temperature_c'), 2)} °C, "
            "塔顶 "
            f"{_format_number(process.get('tower_top_temperature_c'), 2)} °C / "
            f"{_format_number(process.get('tower_top_pressure_mpa_a'), 5)} MPa(a)"
        )
        lines.append(
            "库存终值/初值: "
            + "; ".join(
                f"{name} {_format_number(_mapping(row).get('final_ratio'), 5)}"
                for name, row in _mapping(key.get("inventories")).items()
            )
        )
        lines.append(
            "终态产品 (t/h): "
            + "; ".join(
                f"{name} {_format_number(_mapping(row).get('final_mass_flow_t_h'), 3)}"
                for name, row in _mapping(key.get("products")).items()
            )
        )
        if record.request.run_type == "closed_loop_dynamic":
            lines.append(
                "闭环: "
                f"acceptance={key.get('acceptance_passed')}, "
                + "; ".join(
                    f"{name} err={_format_number(_mapping(row).get('error_normalized'), 6)}"
                    for name, row in _mapping(key.get("control_loops")).items()
                )
            )
    else:
        lines.append(f"场景事件: {len(record.payload.events)}")
    failure = _mapping(parsed.get("failure"))
    if failure:
        lines.append(
            f"终止: {failure.get('stage')} @ {failure.get('time_s')} s — {failure.get('reason')}"
        )
    lines.append(f"产物: {record.run_dir}")
    if verbose:
        lines.extend(
            (
                f"结果指纹: {record.payload.result_fingerprint}",
                f"清单指纹: {record.manifest.manifest_fingerprint}",
                f"有效输入指纹: {record.payload.effective_input_fingerprint}",
            )
        )
    return "\n".join(lines)


__all__ = ["build_result_summary", "render_result_summary"]
