"""Command-line facade for the single stable M7 Python runtime path."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from .api import preview, run
from .artifacts import RunRecord, read_run
from .contracts import RunRequest
from .custom_inputs import (
    list_runtime_input_specs,
    runtime_request_from_mapping,
    runtime_request_template,
)
from .presentation import build_result_summary, render_result_summary
from .presets import list_presets, load_preset


def _json_object(path: Path) -> Mapping[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value!r} is not supported")

    value: object = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{path} must contain one JSON object")
    return cast(Mapping[str, object], value)


def _print_json(value: object) -> None:
    print(
        json.dumps(
            _plain_json(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    )


def _configure_standard_streams() -> None:
    """Use one deterministic encoding for human and machine CLI output."""

    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _record_summary(record: RunRecord) -> dict[str, object]:
    return build_result_summary(record)


def _load_selected_request(args: argparse.Namespace) -> RunRequest:
    request_path = cast(Path | None, args.request)
    preset_id = cast(str | None, args.preset)
    return (
        runtime_request_from_mapping(_json_object(request_path))
        if request_path is not None
        else load_preset(cast(str, preset_id))
    )


def _render_preview(value: Mapping[str, object]) -> str:
    lines = [
        f"运行前预览: {value['preset_id']} ({value['run_type']})",
        f"自定义输入: {'是' if value['customized'] else '否'}",
    ]
    applied = cast(Mapping[str, object], value["applied_inputs"])
    if applied:
        lines.append("已解析输入:")
        for name, raw in applied.items():
            item = cast(Mapping[str, object], raw)
            lines.append(
                f"  {name} = {item['requested_value']} {item['requested_unit']}"
                f" -> {item['normalized_value']} {item['normalized_unit']}"
            )
    effective_case = cast(Mapping[str, object], value["effective_case"])
    feed = cast(Mapping[str, object], effective_case["feed"])
    fractions = cast(Mapping[str, object], feed["mass_fractions"])
    lines.append(
        "实际进料: "
        f"{float(cast(float, feed['mass_flow_kg_s'])) * 3.6:.3f} t/h, "
        f"{float(cast(float, feed['temperature_k'])) - 273.15:.2f} °C, "
        f"{float(cast(float, feed['pressure_pa'])) / 1_000_000.0:.6f} MPa(a)"
    )
    lines.append(
        "实际组成: "
        + ", ".join(
            f"{name}={float(cast(float, amount)):.6f}" for name, amount in fractions.items()
        )
    )
    operating = cast(Mapping[str, object], effective_case["operating_conditions_si"])
    lines.extend(
        (
            "实际温压:",
            (
                f"  flash={float(cast(float, operating['flash_temperature_k'])) - 273.15:.2f} °C / "
                f"{float(cast(float, operating['flash_pressure_pa'])) / 1_000_000.0:.6f} MPa(a)"
            ),
            (
                f"  furnace outlet={float(cast(float, operating['furnace_outlet_temperature_k'])) - 273.15:.2f} °C"
            ),
            (
                f"  tower top={float(cast(float, operating['tower_top_temperature_k'])) - 273.15:.2f} °C / "
                f"{float(cast(float, operating['tower_top_pressure_pa'])) / 1_000_000.0:.6f} MPa(a)"
            ),
            (
                f"  condenser={float(cast(float, operating['condenser_temperature_k'])) - 273.15:.2f} °C, "
                f"ambient={float(cast(float, operating['ambient_temperature_k'])) - 273.15:.2f} °C"
            ),
        )
    )
    effective_model = cast(Mapping[str, object], value["effective_model"])
    duties = cast(Sequence[object], effective_model["pump_around_duties_w"])
    cuts = cast(Sequence[object], effective_model["column_cut_points_k"])
    lines.append(
        "实际操作/模型: "
        f"wash={effective_model['wash_water_ratio']}, "
        f"reflux={effective_model['reflux_ratio']}, "
        "PA="
        + "/".join(f"{float(cast(float, item)) / 1_000_000.0:.3f}" for item in duties)
        + " MW, cuts="
        + "/".join(f"{float(cast(float, item)) - 273.15:.2f}" for item in cuts)
        + " °C"
    )
    if value["run_type"] in {"open_loop_dynamic", "closed_loop_dynamic"}:
        dynamic = cast(Mapping[str, object], effective_model["dynamic"])
        lines.append("实际动态参数:")
        for name, raw in dynamic.items():
            unit = "m³" if name == "top_gas_volume_m3" else "s"
            lines.append(f"  {name} = {raw} {unit}")
        inventory_ratios = cast(Mapping[str, object], value["initial_inventory_ratios"])
        lines.append(
            "实际初始库存比: "
            + ", ".join(f"{name}={raw}" for name, raw in inventory_ratios.items())
        )
    scenario = cast(Mapping[str, object], value["scenario"])
    if scenario["duration_s"] is not None:
        events = scenario["events"]
        event_count = 0 if events is None else len(cast(Sequence[object], events))
        lines.append(
            f"动态网格: {scenario['duration_s']} s / {scenario['time_step_s']} s, "
            f"事件 {event_count}"
        )
        if events is not None:
            for raw in cast(Sequence[object], events):
                event = cast(Mapping[str, object], raw)
                duration = event["duration_s"]
                duration_text = "" if duration is None else f", duration={duration} s"
                lines.append(
                    "  "
                    f"t={event['time_s']} s: {event['target']}={event['value']} "
                    f"({event['value_basis']}{duration_text})"
                )
    lines.append(f"有效输入指纹: {value['execution_input_fingerprint']}")
    lines.append(f"预览确认指纹: {value['preview_fingerprint']}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdu-mini",
        description="Run and inspect the packaged CDU Mini Loop engineering simulator.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    presets = subcommands.add_parser("presets", help="list fixed packaged presets")
    presets.add_argument("--json", action="store_true", dest="as_json")

    inputs = subcommands.add_parser("inputs", help="list controlled custom inputs")
    inputs.add_argument("--preset", required=True)
    inputs.add_argument("--json", action="store_true", dest="as_json")

    template = subcommands.add_parser("template", help="emit an editable run request")
    template.add_argument("--preset", required=True)
    template.add_argument("--output", type=Path)
    template.add_argument("--json", action="store_true", dest="as_json")

    preview_parser = subcommands.add_parser(
        "preview",
        help="resolve effective inputs without running a solver",
    )
    preview_selection = preview_parser.add_mutually_exclusive_group(required=True)
    preview_selection.add_argument("--preset")
    preview_selection.add_argument("--request", type=Path)
    preview_parser.add_argument("--json", action="store_true", dest="as_json")

    run_parser = subcommands.add_parser("run", help="execute and publish one run")
    selection = run_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--preset")
    selection.add_argument("--request", type=Path)
    run_parser.add_argument("--output", type=Path, default=Path("runs"))
    run_parser.add_argument("--confirm-preview")
    run_display = run_parser.add_mutually_exclusive_group()
    run_display.add_argument("--quiet", action="store_true")
    run_display.add_argument("--verbose", action="store_true")
    run_parser.add_argument("--json", action="store_true", dest="as_json")

    batch = subcommands.add_parser("batch", help="execute or resume an ordered batch")
    batch_selection = batch.add_mutually_exclusive_group(required=True)
    batch_selection.add_argument("--request", type=Path)
    batch_selection.add_argument("--resume", type=Path)
    batch.add_argument("--output", type=Path, default=Path("runs"))
    batch.add_argument("--retry-failed", action="store_true")
    batch.add_argument("--json", action="store_true", dest="as_json")

    inspect = subcommands.add_parser("inspect", help="verify and summarize one run")
    inspect.add_argument("run_dir", type=Path)
    inspect_display = inspect.add_mutually_exclusive_group()
    inspect_display.add_argument("--quiet", action="store_true")
    inspect_display.add_argument("--verbose", action="store_true")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _command_presets(*, as_json: bool) -> int:
    rows = [
        {
            "preset_id": preset.preset_id,
            "run_type": preset.run_type,
            "engine_layer": preset.engine_layer,
            "scenario_id": preset.scenario_id,
            "duration_s": preset.duration_s,
            "time_step_s": preset.time_step_s,
            "description": preset.description,
        }
        for preset in list_presets()
    ]
    if as_json:
        _print_json({"presets": rows})
    else:
        for row in rows:
            print(f"{row['preset_id']:<29} {row['run_type']:<23} {row['description']}")
    return 0


def _command_inputs(*, preset_id: str, as_json: bool) -> int:
    rows = [item.as_dict() for item in list_runtime_input_specs(preset_id)]
    if as_json:
        _print_json({"preset_id": preset_id, "inputs": rows})
    else:
        print(f"可调整输入: {preset_id}")
        for row in rows:
            print(
                f"{row['input_id']:<48} {row['display_unit']:<14} "
                f"[{row['minimum']}, {row['maximum']}]"
            )
    return 0


def _command_template(args: argparse.Namespace) -> int:
    payload = runtime_request_template(cast(str, args.preset))
    output = cast(Path | None, args.output)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload["request"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
    else:
        _print_json(payload if cast(bool, args.as_json) else payload["request"])
    return 0


def _command_preview(args: argparse.Namespace) -> int:
    request = _load_selected_request(args)
    resolved = preview(request)
    payload = resolved.as_dict()
    if cast(bool, args.as_json):
        _print_json(payload)
    else:
        print(_render_preview(payload))
    return 0


def _command_run(args: argparse.Namespace) -> int:
    request = _load_selected_request(args)
    confirmation = cast(str | None, args.confirm_preview)
    resolved = preview(request)
    request_path = cast(Path | None, args.request)
    if request_path is not None and confirmation is None:
        if cast(bool, args.as_json) or cast(bool, args.quiet):
            raise ValueError("non-interactive request runs require --confirm-preview <fingerprint>")
        print(_render_preview(resolved.as_dict()))
        print()
        try:
            answer = input("确认按以上实际输入运行？输入 y/yes/是/确认 继续 [N]: ")
        except (EOFError, OSError) as exc:
            raise ValueError(
                "interactive confirmation is unavailable; use preview and "
                "--confirm-preview <fingerprint>"
            ) from exc
        if answer.strip().casefold() not in {"y", "yes", "是", "确认"}:
            print("已取消，未启动仿真。")
            return 0
        confirmation = resolved.preview_fingerprint
        print("输入已确认，开始运行。")
        print()
    elif resolved.is_custom and confirmation is None:
        raise ValueError("custom requests require preview confirmation before execution")
    record = (
        run(request, output_root=cast(Path, args.output))
        if confirmation is None
        else run(
            request,
            output_root=cast(Path, args.output),
            expected_preview_fingerprint=confirmation,
        )
    )
    summary = _record_summary(record)
    if cast(bool, args.as_json):
        _print_json(summary)
    elif cast(bool, args.quiet):
        print(f"{record.payload.runtime_status}: {record.request.preset_id} -> {record.run_dir}")
    else:
        print(render_result_summary(record, verbose=cast(bool, args.verbose)))
    return 0 if record.payload.runtime_status in {"success", "limited", "rejected"} else 1


def _command_batch(args: argparse.Namespace) -> int:
    # Imported lazily so presets/run/inspect remain available while a batch is recovering.
    from .batch import BatchRequest, execute_batch, resume_batch

    resume_dir = cast(Path | None, args.resume)
    retry_failed = cast(bool, args.retry_failed)
    if resume_dir is not None:
        record = resume_batch(resume_dir, retry_failed=retry_failed)
    else:
        request_path = cast(Path, args.request)
        request = BatchRequest.from_mapping(_json_object(request_path))
        record = execute_batch(
            request,
            output_root=cast(Path, args.output),
            retry_failed=retry_failed,
        )
    summary = record.as_summary_dict()
    if cast(bool, args.as_json):
        _print_json(summary)
    else:
        print(
            f"{summary['batch_status']}: {summary['completed_items']}/"
            f"{summary['item_count']} items -> {summary['batch_dir']}"
        )
    return 0 if summary["batch_status"] in {"success", "limited"} else 1


def _command_inspect(args: argparse.Namespace) -> int:
    record = read_run(cast(Path, args.run_dir))
    summary = _record_summary(record)
    if cast(bool, args.as_json):
        _print_json(summary)
    elif cast(bool, args.quiet):
        print(f"{summary['runtime_status']}: {summary['preset_id']} ({summary['run_id']})")
    else:
        print(render_result_summary(record, verbose=cast(bool, args.verbose)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_standard_streams()
    args = _parser().parse_args(argv)
    try:
        if args.command == "presets":
            return _command_presets(as_json=cast(bool, args.as_json))
        if args.command == "inputs":
            return _command_inputs(
                preset_id=cast(str, args.preset),
                as_json=cast(bool, args.as_json),
            )
        if args.command == "template":
            return _command_template(args)
        if args.command == "preview":
            return _command_preview(args)
        if args.command == "run":
            return _command_run(args)
        if args.command == "batch":
            return _command_batch(args)
        if args.command == "inspect":
            return _command_inspect(args)
        raise AssertionError(f"unhandled command {args.command!r}")
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - covered through package __main__
    raise SystemExit(main())


__all__ = ["main"]
