"""The diagnostic CLI delegates calculation and evidence reading to the formal API."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.simulation import probe_hysys_single_change as cli


@pytest.mark.parametrize(("status", "exit_code"), [("passed", 0), ("failed", 1)])
def test_diagnostic_delegates_fixed_change_and_reads_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str, exit_code: int
) -> None:
    manifest = Path("frozen/manifest.json")
    output = Path("new-run")
    report = output / "report.json"
    calls = []
    monkeypatch.setattr("sys.argv", ["probe", "--baseline", str(manifest), "--output", str(output)])
    monkeypatch.setattr(
        cli,
        "read_baseline",
        lambda path: SimpleNamespace(
            snapshot=SimpleNamespace(specifications=(SimpleNamespace(name="T-39", goal=156.8),))
        ),
    )

    def run(path: Path, target: float, directory: Path) -> Path:
        calls.append((path, target, directory))
        return report

    def read(path: Path) -> SimpleNamespace:
        assert path == report
        return SimpleNamespace(status=status)

    monkeypatch.setattr(cli, "run_t39_point", run)
    monkeypatch.setattr(cli, "read_point_result", read)
    assert cli.main() == exit_code
    assert calls == [(manifest, 156.9, output)]
    assert status in capsys.readouterr().out
