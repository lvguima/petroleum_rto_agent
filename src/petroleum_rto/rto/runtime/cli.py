"""Objective-count-neutral CLI with explicit legacy compatibility commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..compilation import SystemCompilationError
from .api import (
    OfflineInspectionError,
    approve_strategy,
    capabilities,
    inspect_legacy_v1_offline,
    inspect_legacy_v2_offline,
    inspect_offline,
    legacy_external_request_summary_v1,
    legacy_external_request_summary_v2,
    publish_strategy,
    query_legacy_v1_strategies,
    run_legacy_v1_offline,
    run_legacy_v1_request,
    run_legacy_v2_request,
    run_offline,
    run_summary,
    run_summary_legacy_v1,
    run_summary_legacy_v2,
    validate_intent_file,
    validate_legacy_v1_request,
    validate_legacy_v2_request,
    validate_problem_files,
)


def _repo_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path)


def _common_paths(parser: argparse.ArgumentParser) -> None:
    _repo_root(parser)
    parser.add_argument("--library-root", type=Path)


def _intent_and_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--intent-file", type=Path, required=True)
    parser.add_argument("--context-file", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rto-offline")
    commands = parser.add_subparsers(dest="command", required=True)

    capabilities_parser = commands.add_parser(
        "capabilities",
        help="print the unified atomic capability manifest without simulation",
    )
    _repo_root(capabilities_parser)

    validate_intent = commands.add_parser(
        "validate-intent",
        help="validate one context-free unified intent without simulation",
    )
    _repo_root(validate_intent)
    validate_intent.add_argument("--intent-file", type=Path, required=True)

    validate_problem = commands.add_parser(
        "validate-problem",
        help="bind intent and trusted context into one unified problem without solving",
    )
    _repo_root(validate_problem)
    _intent_and_context(validate_problem)

    run_parser = commands.add_parser(
        "run",
        help="run or resume one unified objective-count-neutral workflow",
    )
    _common_paths(run_parser)
    _intent_and_context(run_parser)
    run_parser.add_argument("--run-root", type=Path)
    run_parser.add_argument("--actor", required=True)
    run_parser.add_argument(
        "--coverage-policy",
        choices=("point", "sampled-anchors"),
        default="point",
    )

    inspect_parser = commands.add_parser(
        "inspect",
        help="strictly reload a workflow using its stored manifest version",
    )
    _common_paths(inspect_parser)
    inspect_parser.add_argument("--run-dir", type=Path, required=True)
    inspect_parser.add_argument(
        "--legacy-request-file",
        type=Path,
        help="required only for a legacy V1 run whose request stores an external ref",
    )

    for name in ("approve", "publish"):
        lifecycle = commands.add_parser(
            name,
            help=f"explicitly {name} one unified strategy revision",
        )
        lifecycle.add_argument("--library-root", type=Path, required=True)
        lifecycle.add_argument("--strategy-id", required=True)
        lifecycle.add_argument("--revision", type=int, required=True)
        lifecycle.add_argument("--actor", required=True)
        lifecycle.add_argument("--reason")

    legacy_validate_v1 = commands.add_parser(
        "legacy-validate-v1",
        help="validate one historical V1 external request",
    )
    _repo_root(legacy_validate_v1)
    legacy_validate_v1.add_argument("--request-file", type=Path, required=True)

    legacy_validate_v2 = commands.add_parser(
        "legacy-validate-v2",
        help="validate one historical V2 external request",
    )
    _repo_root(legacy_validate_v2)
    legacy_validate_v2.add_argument("--request-file", type=Path, required=True)

    legacy_run_v1 = commands.add_parser(
        "legacy-run-v1",
        help="run or resume a historical V1 workflow",
    )
    _common_paths(legacy_run_v1)
    legacy_run_v1.add_argument("--run-root", type=Path)
    legacy_run_v1.add_argument("--actor", required=True)
    legacy_run_v1.add_argument("--request-file", type=Path)
    legacy_run_v1.add_argument(
        "--coverage-policy",
        choices=("point", "sampled-anchors"),
    )

    legacy_run_v2 = commands.add_parser(
        "legacy-run-v2",
        help="run or resume a historical V2 workflow",
    )
    _common_paths(legacy_run_v2)
    legacy_run_v2.add_argument("--run-root", type=Path)
    legacy_run_v2.add_argument("--actor", required=True)
    legacy_run_v2.add_argument("--request-file", type=Path, required=True)

    legacy_inspect_v1 = commands.add_parser(
        "legacy-inspect-v1",
        help="strictly reload a historical V1 workflow",
    )
    _common_paths(legacy_inspect_v1)
    legacy_inspect_v1.add_argument("--run-dir", type=Path, required=True)
    legacy_inspect_v1.add_argument("--request-file", type=Path)

    legacy_inspect_v2 = commands.add_parser(
        "legacy-inspect-v2",
        help="strictly reload a historical V2 workflow",
    )
    _common_paths(legacy_inspect_v2)
    legacy_inspect_v2.add_argument("--run-dir", type=Path, required=True)
    legacy_inspect_v2.add_argument("--request-file", type=Path, required=True)

    legacy_query = commands.add_parser(
        "legacy-query-v1",
        help="query historical V1 published exact sampled anchors",
    )
    _common_paths(legacy_query)
    legacy_query.add_argument("--feed-t-h", type=float, required=True)
    legacy_query.add_argument("--tolerance-t-h", type=float, default=0.001)
    return parser


def _roots(args: argparse.Namespace) -> tuple[Path | None, Path, Path]:
    repo_root = None if args.repo_root is None else args.repo_root.resolve()
    workspace = Path.cwd().resolve() if repo_root is None else repo_root
    run_root = (
        workspace / "runs" / "rto"
        if getattr(args, "run_root", None) is None
        else args.run_root.resolve()
    )
    library_root = (
        workspace / "runs" / "rto" / "strategy-library"
        if args.library_root is None
        else args.library_root.resolve()
    )
    return repo_root, run_root, library_root


def _problem_summary(problem: object) -> dict[str, object]:
    from ..contracts.problem import OptimizationProblem

    if not isinstance(problem, OptimizationProblem):
        raise TypeError("problem must be OptimizationProblem")
    return {
        "status": "valid",
        "intent_ref": problem.intent_ref.as_dict(),
        "context_ref": problem.context_ref.as_dict(),
        "problem_ref": problem.ref.as_dict(),
        "objectives": [item.as_dict() for item in problem.objectives],
        "decision_variables": [item.variable_id for item in problem.decision_domains],
        "hard_constraints": [item.as_dict() for item in problem.hard_constraints],
        "publishability_constraints": [
            item.as_dict() for item in problem.publishability_constraints
        ],
        "result_request": problem.result_request.as_dict(),
        "claim_scope": problem.claim_scope,
        "solver_called": False,
    }


def _lifecycle_value(args: argparse.Namespace, *, publish: bool) -> dict[str, object]:
    kwargs = {
        "library_root": args.library_root.resolve(),
        "strategy_id": args.strategy_id,
        "revision": args.revision,
        "actor": args.actor,
        **({} if args.reason is None else {"reason": args.reason}),
    }
    if publish:
        return publish_strategy(**kwargs).as_dict()
    record = approve_strategy(**kwargs)
    return {
        "strategy_ref": record.entry.ref.as_dict(),
        "current_state": record.current_state,
        "control_authority": "none",
    }


def _execute(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "capabilities":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        value = capabilities(repo_root=repo_root)
    elif args.command == "validate-intent":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        resolution = validate_intent_file(
            repo_root=repo_root,
            intent_file=args.intent_file.resolve(),
        )
        value = {**resolution.as_dict(), "solver_called": False}
    elif args.command == "validate-problem":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        value = _problem_summary(
            validate_problem_files(
                repo_root=repo_root,
                intent_file=args.intent_file.resolve(),
                context_file=args.context_file.resolve(),
            )
        )
    elif args.command == "run":
        repo_root, run_root, library_root = _roots(args)
        value = run_summary(
            run_offline(
                repo_root=repo_root,
                intent_file=args.intent_file.resolve(),
                context_file=args.context_file.resolve(),
                run_root=run_root,
                library_root=library_root,
                actor=args.actor,
                coverage_policy=args.coverage_policy,
            )
        )
    elif args.command == "inspect":
        repo_root, _, library_root = _roots(args)
        value = run_summary(
            inspect_offline(
                args.run_dir.resolve(),
                repo_root=repo_root,
                library_root=library_root,
                legacy_request_file=(
                    None if args.legacy_request_file is None else args.legacy_request_file.resolve()
                ),
            )
        )
    elif args.command == "approve":
        value = _lifecycle_value(args, publish=False)
    elif args.command == "publish":
        value = _lifecycle_value(args, publish=True)
    elif args.command == "legacy-validate-v1":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        value = legacy_external_request_summary_v1(
            validate_legacy_v1_request(
                repo_root=repo_root,
                request_file=args.request_file.resolve(),
            )
        )
    elif args.command == "legacy-validate-v2":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        value = legacy_external_request_summary_v2(
            validate_legacy_v2_request(
                repo_root=repo_root,
                request_file=args.request_file.resolve(),
            )
        )
    elif args.command == "legacy-run-v1":
        repo_root, run_root, library_root = _roots(args)
        if args.request_file is None:
            record_v1 = run_legacy_v1_offline(
                repo_root=repo_root,
                run_root=run_root,
                library_root=library_root,
                actor=args.actor,
                coverage_policy=args.coverage_policy or "sampled-anchors",
            )
        else:
            if args.coverage_policy is not None:
                parser.error("legacy V1 external requests define their own coverage policy")
            record_v1 = run_legacy_v1_request(
                repo_root=repo_root,
                request_file=args.request_file.resolve(),
                run_root=run_root,
                library_root=library_root,
                actor=args.actor,
            )
        value = run_summary_legacy_v1(record_v1)
    elif args.command == "legacy-run-v2":
        repo_root, run_root, library_root = _roots(args)
        value = run_summary_legacy_v2(
            run_legacy_v2_request(
                repo_root=repo_root,
                request_file=args.request_file.resolve(),
                run_root=run_root,
                library_root=library_root,
                actor=args.actor,
            )
        )
    elif args.command == "legacy-inspect-v1":
        repo_root, _, library_root = _roots(args)
        value = run_summary_legacy_v1(
            inspect_legacy_v1_offline(
                args.run_dir.resolve(),
                repo_root=repo_root,
                library_root=library_root,
                request_file=(None if args.request_file is None else args.request_file.resolve()),
            )
        )
    elif args.command == "legacy-inspect-v2":
        repo_root, _, library_root = _roots(args)
        value = run_summary_legacy_v2(
            inspect_legacy_v2_offline(
                args.run_dir.resolve(),
                repo_root=repo_root,
                library_root=library_root,
                request_file=args.request_file.resolve(),
            )
        )
    else:
        repo_root, _, library_root = _roots(args)
        records = query_legacy_v1_strategies(
            repo_root=repo_root,
            library_root=library_root,
            feed_mass_flow_kg_s=args.feed_t_h / 3.6,
            measurement_tolerance_kg_s=args.tolerance_t_h / 3.6,
        )
        value = {
            "workflow_kind": "legacy-v1",
            "match_count": len(records),
            "strategies": [
                {
                    "strategy_ref": item.entry.ref.as_dict(),
                    "current_state": item.current_state,
                    "action_setpoints": dict(item.entry.action_setpoints),
                }
                for item in records
            ],
        }
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _report_error(exc: Exception, *, exit_code: int) -> int:
    label = "error" if exit_code == 2 else "system error"
    message = " ".join(str(exc).split()) or type(exc).__name__
    print(f"rto-offline: {label}: {message}", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _execute(parser, args)
    except (OfflineInspectionError, SystemCompilationError, OSError, RuntimeError) as exc:
        return _report_error(exc, exit_code=1)
    except (TypeError, ValueError) as exc:
        return _report_error(exc, exit_code=2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
