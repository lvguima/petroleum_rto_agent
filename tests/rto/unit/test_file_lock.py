from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from petroleum_rto.rto._file_lock import exclusive_file_lock


def test_kernel_lock_is_exclusive_and_released_when_writer_crashes(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "writer.lock"
    script = """
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from petroleum_rto.rto._file_lock import exclusive_file_lock

with exclusive_file_lock(Path(sys.argv[1]), label="child writer"):
    print("locked", flush=True)
    os.read(0, 1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), str(repo_root / "src")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with (
            pytest.raises(RuntimeError, match="locked by another writer"),
            exclusive_file_lock(lock_path, label="parent writer"),
        ):
            pytest.fail("second writer unexpectedly acquired the kernel lock")
        child.kill()
        assert child.wait(timeout=10) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    with exclusive_file_lock(lock_path, label="replacement writer"):
        pass
    assert lock_path.read_text(encoding="ascii") == f"pid={os.getpid()}\n"
    assert lock_path.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point boundary")
def test_windows_writer_rejects_junction_and_hardlink_targets(tmp_path: Path) -> None:
    import _winapi

    original = tmp_path / "original"
    original.mkdir()
    linked = tmp_path / "linked"
    _winapi.CreateJunction(str(original), str(linked))
    with pytest.raises(ValueError), exclusive_file_lock(linked / "writer.lock", label="writer"):
        pytest.fail("junction path accepted")
    assert not (original / "writer.lock").exists()
    preserved = original / "preserved"
    preserved.write_text("unchanged")
    lock_path = original / "writer.lock"
    os.link(preserved, lock_path)
    with pytest.raises(ValueError), exclusive_file_lock(lock_path, label="writer"):
        pytest.fail("hardlinked lock accepted")
    assert preserved.read_text() == "unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows mandatory lock and directory handles")
def test_windows_writer_pins_directory_until_release(tmp_path: Path) -> None:
    original = tmp_path / "original"
    with exclusive_file_lock(original / "writer.lock", label="writer"), pytest.raises(OSError):
        original.rename(tmp_path / "moved")
    original.rename(tmp_path / "moved")
