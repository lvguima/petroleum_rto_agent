"""Frozen strict-intent compatibility CLI; public commands use ``assistant.cli``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

from .api import (
    continue_intent,
    discover_models,
    evaluate_models,
    inspect_intent_session,
    interpret_intent,
)
from .egress import MAX_TEXT_BYTES
from .evidence import SessionConflictError
from .runtime import DomainIntentOutcome

_ERROR_SCHEMA_ID: Final[str] = "domain-model-cli-error"
_ERROR_SCHEMA_VERSION: Final[str] = "1.0.0"
_SENSITIVE_VALUE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}|"
    r"\bsk[-_][A-Za-z0-9_-]{8,}|"
    r"(?<![A-Za-z0-9_])(?:authorization|api[_ -]?key|access[_ -]?token|"
    r"client[_ -]?secret|password|secret|token)(?![A-Za-z0-9_])"
    r"\s*[:=]\s*[^\s\"',}]{4,}|"
    r"(?:DMX|API|访问|认证)?(?:令牌|密钥)\s*[:=：]\s*[^\s\"',}]{4,})"
)


class _StructuredParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"command arguments are invalid: {message}")


@dataclass(frozen=True)
class _CommandResult:
    payload: dict[str, object]
    exit_code: int = 0
    use_stderr: bool = False


def _common_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)


def _parser() -> argparse.ArgumentParser:
    parser = _StructuredParser(prog="rto-intent")
    commands = parser.add_subparsers(dest="command", required=True)

    models_parser = commands.add_parser(
        "models",
        help="discover models without changing the configured model list",
    )
    models_parser.add_argument("--provider", default="dmx-cn")
    _common_roots(models_parser)

    interpret_parser = commands.add_parser(
        "interpret",
        help="interpret one business request without solving or simulation",
    )
    interpret_parser.add_argument("--provider", default="dmx-cn", help=argparse.SUPPRESS)
    interpret_parser.add_argument("--model", required=True)
    interpret_parser.add_argument("--input", required=True, metavar="FILE|-")
    _common_roots(interpret_parser)

    continue_parser = commands.add_parser(
        "continue",
        help="continue one manifest-pinned clarification session",
    )
    continue_parser.add_argument("--session", type=Path, required=True, metavar="MANIFEST")
    continue_parser.add_argument("--answers", type=Path, required=True, metavar="FILE")
    _common_roots(continue_parser)

    inspect_parser = commands.add_parser(
        "inspect",
        help="strictly inspect a session without exposing approved egress bodies",
    )
    inspect_parser.add_argument("--session", type=Path, required=True, metavar="MANIFEST")
    inspect_parser.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)

    eval_parser = commands.add_parser(
        "eval",
        help="evaluate each selected model independently with three runs per gold case",
    )
    eval_parser.add_argument("--provider", default="dmx-cn", help=argparse.SUPPRESS)
    eval_parser.add_argument("--models", required=True, metavar="MODEL-ID,...")
    eval_parser.add_argument("--suite", type=Path, help=argparse.SUPPRESS)
    eval_parser.add_argument(
        "--case-id",
        action="append",
        help=argparse.SUPPRESS,
    )
    _common_roots(eval_parser)
    return parser


def _resolved(path: Path | None) -> Path | None:
    return None if path is None else path.resolve()


def _read_user_text(source: str) -> str:
    if source == "-":
        value = sys.stdin.read(MAX_TEXT_BYTES + 1)
        if len(value) > MAX_TEXT_BYTES:
            raise ValueError("standard input exceeds the 8 KiB user-text policy")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("standard input must be valid UTF-8 text") from exc
    else:
        path = Path(source).resolve()
        if not path.is_file():
            raise ValueError("intent input must be an existing file or '-'")
        payload = path.read_bytes()
        if len(payload) > MAX_TEXT_BYTES:
            raise ValueError("intent input exceeds the 8 KiB user-text policy")
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("intent input must be valid UTF-8 text") from exc
        size = len(payload)
    if size == 0 or not value.strip():
        raise ValueError("intent input must contain non-empty text")
    return value


def _outcome_result(outcome: DomainIntentOutcome) -> _CommandResult:
    if outcome.status not in {"provider_failed", "egress_blocked", "failed"}:
        return _CommandResult(outcome.as_dict())
    if outcome.provider_error is not None:
        error = outcome.provider_error.as_dict()
        category = str(error["category"])
        code = str(error["code"])
        message = str(error["message"])
        exit_code = 1
    else:
        category = "contract"
        code = "communication-failed"
        message = "model output did not produce a complete strict communication result"
        exit_code = 2
    return _CommandResult(
        _error_payload(
            category=category,
            code=code,
            message=message,
            details={"outcome": outcome.as_dict()},
        ),
        exit_code=exit_code,
        use_stderr=True,
    )


def _execute(args: argparse.Namespace) -> _CommandResult:
    if args.command == "models":
        return _CommandResult(
            discover_models(
                provider_id=args.provider,
                repo_root=_resolved(args.repo_root),
                project_root=_resolved(args.project_root),
            )
        )
    if args.command == "interpret":
        return _outcome_result(
            interpret_intent(
                _read_user_text(args.input),
                provider_id=args.provider,
                model_id=args.model,
                repo_root=_resolved(args.repo_root),
                project_root=_resolved(args.project_root),
            )
        )
    if args.command == "continue":
        return _outcome_result(
            continue_intent(
                manifest_path=args.session.resolve(),
                answers_path=args.answers.resolve(),
                repo_root=_resolved(args.repo_root),
                project_root=_resolved(args.project_root),
            )
        )
    if args.command == "inspect":
        return _CommandResult(
            inspect_intent_session(
                args.session.resolve(),
                project_root=_resolved(args.project_root),
            )
        )
    if args.command == "eval":
        model_ids = tuple(item.strip() for item in args.models.split(",") if item.strip())
        report = evaluate_models(
            model_ids,
            provider_id=args.provider,
            repo_root=_resolved(args.repo_root),
            project_root=_resolved(args.project_root),
            suite_path=_resolved(args.suite),
            case_ids=args.case_id,
        )
        return _CommandResult(
            report,
            exit_code=0 if report.get("all_models_meet_quality_target") is True else 3,
        )
    raise AssertionError("required command was not dispatched")


def _error_payload(
    *,
    category: str,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "category": category,
        "code": code,
        "message": _safe_message(message),
    }
    if details is not None:
        error["details"] = details
    return {
        "schema_id": _ERROR_SCHEMA_ID,
        "schema_version": _ERROR_SCHEMA_VERSION,
        "status": "error",
        "error": error,
    }


def _safe_message(message: str) -> str:
    normalized = " ".join(message.split()) or "operation failed"
    return _SENSITIVE_VALUE.sub("[REDACTED]", normalized)


def _exception_result(exc: Exception) -> _CommandResult:
    provider_error = getattr(exc, "error", None)
    if provider_error is not None and hasattr(provider_error, "as_dict"):
        error = provider_error.as_dict()
        evidence_manifest = getattr(exc, "evidence_manifest", None)
        evidence_fingerprint = getattr(exc, "evidence_fingerprint", None)
        details = (
            None
            if evidence_manifest is None
            else {
                "evidence_manifest": str(evidence_manifest),
                "evidence_fingerprint": evidence_fingerprint,
            }
        )
        return _CommandResult(
            _error_payload(
                category=str(error["category"]),
                code=str(error["code"]),
                message=str(error["message"]),
                details=details,
            ),
            exit_code=1,
            use_stderr=True,
        )
    if isinstance(exc, SessionConflictError):
        return _CommandResult(
            _error_payload(
                category="session",
                code=exc.code,
                message="session continuation was rejected by the concurrency policy",
            ),
            exit_code=2,
            use_stderr=True,
        )
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        return _CommandResult(
            _error_payload(
                category="validation",
                code="invalid-input",
                message="command input failed strict validation",
            ),
            exit_code=2,
            use_stderr=True,
        )
    if isinstance(exc, OSError):
        return _CommandResult(
            _error_payload(
                category="io",
                code="io-failure",
                message="a required local file could not be read or written",
            ),
            exit_code=1,
            use_stderr=True,
        )
    return _CommandResult(
        _error_payload(
            category="system",
            code="unexpected-failure",
            message="the domain-model command failed unexpectedly",
        ),
        exit_code=1,
        use_stderr=True,
    )


def _emit(result: _CommandResult) -> int:
    stream = sys.stderr if result.use_stderr else sys.stdout
    print(
        json.dumps(result.payload, ensure_ascii=False, sort_keys=True, indent=2),
        file=stream,
    )
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = _execute(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary always emits one safe JSON error
        result = _exception_result(exc)
    return _emit(result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
