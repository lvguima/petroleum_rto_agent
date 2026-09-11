"""Run a read/write/solve cycle using the configured HYSYS model."""
import argparse
from datetime import datetime, timezone
import logging
import math
import time

from hysys_control import HYSYSControl, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="mjh_ATM.hsc")
    parser.add_argument("--input", default="data_write", help="MV JSON to write")
    parser.add_argument("--output", default="data_read", help="Final operating point JSON")
    parser.add_argument("--before", default="artifacts/operating_point_before.json")
    parser.add_argument("--report", default="artifacts/run_report.json")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--read-only", action="store_true", help="Export without changing MVs or solver state")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "case": args.case,
              "read_only": args.read_only, "success": False}
    try:
        logging.info("Connecting to HYSYS: %s", args.case)
        hysys = HYSYSControl(args.case)
        report["initial_status"] = hysys.solver_status()
        hysys.get_operating_point(args.before)
        if not args.read_only:
            logging.info("Writing MVs from %s", args.input)
            report["mv_written"] = hysys.set_mv(args.input)
            logging.info("Waiting for convergence (polling timeout %.1fs)", args.timeout)
            hysys.wait_for_convergence(timeout=args.timeout)
        op = hysys.get_operating_point(args.output)
        report["final_status"] = hysys.solver_status()
        report["counts"] = {"mv": sum(map(len, op["mv"].values())),
                            "cv": sum(map(len, op["cv"].values())), "stages": len(op["stage"])}
        report["output"] = args.output
        report["success"] = True
        logging.info("%s; exported %s: %s", "Read completed" if args.read_only else "Converged",
                     args.output, report["counts"])
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        logging.exception("HYSYS run failed")
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(args.report, report)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
