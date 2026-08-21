from __future__ import annotations

import ast
from pathlib import Path


def test_domain_model_source_cannot_import_solver_simulator_or_trusted_context(
    repo_root: Path,
) -> None:
    source_root = repo_root / "src/petroleum_rto/domain_model"
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: tuple[str, ...]
            if isinstance(node, ast.Import):
                imported = tuple(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            else:
                continue
            for module in imported:
                if module.startswith("petroleum_rto.cdu") or (
                    module.startswith("petroleum_rto.rto.")
                    and not module.startswith("petroleum_rto.rto.communication")
                ):
                    violations.append(f"{path.relative_to(repo_root)}:{node.lineno}:{module}")

    assert violations == []


def test_domain_model_package_root_only_imports_active_chat_modules(repo_root: Path) -> None:
    source = repo_root / "src/petroleum_rto/domain_model/__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is not None
    }

    assert imported == {"chat", "chat_settings"}


def test_assistant_can_only_import_approved_read_only_rto_functions(repo_root: Path) -> None:
    source = repo_root / "src/petroleum_rto/assistant/cli.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    allowed = {
        "petroleum_rto.rto": {"load_operating_context"},
        "petroleum_rto.rto.runtime": {
            "build_chat_operating_status",
            "build_chat_result_summary",
            "inspect_offline",
        },
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.startswith(("petroleum_rto.rto", "petroleum_rto.cdu")):
                    violations.append(f"{node.lineno}:{item.name}")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.startswith("petroleum_rto.cdu"):
                violations.append(f"{node.lineno}:{node.module}")
            if node.module.startswith("petroleum_rto.rto"):
                imported = {item.name for item in node.names}
                if node.module not in allowed or not imported <= allowed[node.module]:
                    violations.append(f"{node.lineno}:{node.module}:{sorted(imported)}")

    assert violations == []
