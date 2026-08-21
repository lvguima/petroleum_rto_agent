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
