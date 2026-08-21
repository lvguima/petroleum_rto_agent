"""Command-line entry point for the objective-count-neutral offline RTO."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..compilation import SystemCompilationError
from ..contracts.problem import OptimizationProblem
from .api import (
    OfflineInspectionError,
    approve_strategy,
    capabilities,
    inspect_offline,
    publish_strategy,
    run_offline,
    run_summary,
    validate_intent_file,
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
        "capabilities", help="print atomic capabilities without simulation"
    )
    _repo_root(capabilities_parser)

    validate_intent = commands.add_parser(
        "validate-intent", help="validate an intent without simulation"
    )
    _repo_root(validate_intent)
    validate_intent.add_argument("--intent-file", type=Path, required=True)

    validate_problem = commands.add_parser(
        "validate-problem", help="build a problem without solving"
    )
    _repo_root(validate_problem)
    _intent_and_context(validate_problem)

    run_parser = commands.add_parser("run", help="run or resume one workflow")
    _common_paths(run_parser)
    _intent_and_context(run_parser)
    run_parser.add_argument("--run-root", type=Path)
    run_parser.add_argument("--actor", required=True)
    run_parser.add_argument(
        "--coverage-policy", choices=("point", "sampled-anchors"), default="point"
    )

    inspect_parser = commands.add_parser("inspect", help="strictly reload one workflow")
    inspect_parser.add_argument("--library-root", type=Path)
    inspect_parser.add_argument("--run-dir", type=Path, required=True)

    for name in ("approve", "publish"):
        lifecycle = commands.add_parser(name, help=f"explicitly {name} a strategy revision")
        lifecycle.add_argument("--library-root", type=Path, required=True)
        lifecycle.add_argument("--strategy-id", required=True)
        lifecycle.add_argument("--revision", type=int, required=True)
        lifecycle.add_argument("--actor", required=True)
        lifecycle.add_argument("--reason")
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


def _problem_summary(problem: OptimizationProblem) -> dict[str, object]:
    if not isinstance(problem, OptimizationProblem):
        raise TypeError("problem must be OptimizationProblem")
    return {
        "status": "valid",
        "intent_ref": problem.intent_ref.as_dict(),
        "context_ref": problem.context_ref.as_dict(),
        "problem_ref": problem.ref.as_dict(),
        "execution_route_ref": problem.execution_route_ref.as_dict(),
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


def _execute(args: argparse.Namespace) -> int:
    if args.command == "capabilities":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        value = capabilities(repo_root=repo_root)
    elif args.command == "validate-intent":
        repo_root = None if args.repo_root is None else args.repo_root.resolve()
        resolution = validate_intent_file(
            repo_root=repo_root, intent_file=args.intent_file.resolve()
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
        library_root = (
            Path.cwd().resolve() / "runs" / "rto" / "strategy-library"
            if args.library_root is None
            else args.library_root.resolve()
        )
        value = run_summary(inspect_offline(args.run_dir.resolve(), library_root=library_root))
    elif args.command == "approve":
        value = _lifecycle_value(args, publish=False)
    else:
        value = _lifecycle_value(args, publish=True)
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
        return _execute(args)
    except (OfflineInspectionError, SystemCompilationError, OSError, RuntimeError) as exc:
        return _report_error(exc, exit_code=1)
    except (TypeError, ValueError) as exc:
        return _report_error(exc, exit_code=2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
