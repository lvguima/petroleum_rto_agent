"""Read the explicitly selected open HYSYS case into an independent snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from .hysys import DEFAULT_CATALOG, read_current_snapshot
from .models import read_snapshot, write_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    output_root = Path(__file__).resolve().parents[3] / "runs/simulation"
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="hysys-snapshot-", dir=output_root))
    try:
        snapshot = read_current_snapshot(args.case, args.catalog)
        path = run_dir / "snapshot.json"
        write_snapshot(path, snapshot)
        if read_snapshot(path) != snapshot:
            raise ValueError("Snapshot did not round trip through the strict reader")
        report: dict[str, object] = {
            "status": "observed_stable" if snapshot.consistent_observation else "observed_unstable",
            "snapshot_path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "counts": {
                "variables": len(snapshot.variables),
                "stages": len(snapshot.stages),
                "specifications": len(snapshot.specifications),
            },
            "eligible_for_optimization": False,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostic CLI, never classifies process feasibility
        report = {
            "status": "failed",
            "code": getattr(exc, "code", "read-failed"),
            "message": str(exc),
            "hresult": getattr(exc, "hresult", None),
            "scode": getattr(exc, "scode", None),
        }
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({**report, "report_path": str(run_dir / "report.json")}, ensure_ascii=True))
    return {"observed_stable": 0, "observed_unstable": 2, "failed": 1}[str(report["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
