from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.cdu.runtime import provenance


class _Distribution:
    def __init__(self, root: Path, *, direct_url: str | None) -> None:
        self._root = root
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url

    def locate_file(self, path: str) -> Path:
        assert path == "petroleum_rto"
        return self._root


def _source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "petroleum_rto"
    (root / "cdu" / "runtime" / "data").mkdir(parents=True)
    (root / "rto").mkdir()
    (root / "domain_model").mkdir()
    (root / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (root / "cdu" / "__init__.py").write_text("", encoding="utf-8")
    (root / "cdu" / "runtime" / "model.py").write_text("MODEL_VERSION = 1\n", encoding="utf-8")
    (root / "cdu" / "runtime" / "data" / "input.json").write_text('{"value":1}\n', encoding="utf-8")
    (root / "rto" / "solver.py").write_text("SOLVER_VERSION = 1\n", encoding="utf-8")
    (root / "domain_model" / "model.py").write_text("MODEL_VERSION = 1\n", encoding="utf-8")
    return root


def test_source_tree_hash_tracks_only_explicit_cdu_closure(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    initial = provenance._source_tree_sha256(root)

    (root / "rto" / "solver.py").write_text("SOLVER_VERSION = 2\n", encoding="utf-8")
    (root / "domain_model" / "model.py").write_text("MODEL_VERSION = 2\n", encoding="utf-8")
    assert provenance._source_tree_sha256(root) == initial

    (root / "cdu" / "runtime" / "model.py").write_text("MODEL_VERSION = 2\n", encoding="utf-8")
    after_python_change = provenance._source_tree_sha256(root)
    assert after_python_change != initial

    (root / "cdu" / "runtime" / "data" / "input.json").write_text('{"value":2}\n', encoding="utf-8")
    after_json_change = provenance._source_tree_sha256(root)
    assert after_json_change != after_python_change

    (root / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    assert provenance._source_tree_sha256(root) != after_json_change


def test_editable_or_development_source_is_rehashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_tree(tmp_path)
    provenance._cached_installed_source_tree_sha256.cache_clear()
    monkeypatch.setattr(provenance, "_installed_source_tree_root", lambda: root)
    distribution = _Distribution(
        root,
        direct_url='{"dir_info":{"editable":true},"url":"file:///development"}',
    )
    monkeypatch.setattr(provenance.metadata, "distribution", lambda _: distribution)

    assert not provenance._installed_source_is_immutable(root)

    initial = provenance.installed_source_tree_sha256()
    (root / "cdu" / "runtime" / "model.py").write_text("MODEL_VERSION = 2\n", encoding="utf-8")

    assert provenance.installed_source_tree_sha256() != initial
    assert provenance._cached_installed_source_tree_sha256.cache_info().currsize == 0


def test_noneditable_installed_source_uses_process_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_tree(tmp_path)
    provenance._cached_installed_source_tree_sha256.cache_clear()
    monkeypatch.setattr(provenance, "_installed_source_tree_root", lambda: root)
    distribution = _Distribution(root, direct_url=None)
    monkeypatch.setattr(provenance.metadata, "distribution", lambda _: distribution)

    assert provenance._installed_source_is_immutable(root)

    try:
        initial = provenance.installed_source_tree_sha256()
        (root / "cdu" / "runtime" / "model.py").write_text("MODEL_VERSION = 2\n", encoding="utf-8")

        assert provenance.installed_source_tree_sha256() == initial
        cache_info = provenance._cached_installed_source_tree_sha256.cache_info()
        assert cache_info.misses == 1
        assert cache_info.hits == 1
    finally:
        provenance._cached_installed_source_tree_sha256.cache_clear()
