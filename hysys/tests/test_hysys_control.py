"""Offline regression tests; no HYSYS connection or model changes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hysys_control import HYSYSControl, write_json


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "input.json"
        self.control = HYSYSControl.__new__(HYSYSControl)
        self.control.MV_ROWS = range(2, 5)
        self.control.case = SimpleNamespace(Solver=SimpleNamespace(CanSolve=True))
        self.cells = {}
        for row, obj, prop, value, unit in [(2, "feed", "flow", 1.0, "kg/h"),
                                           (3, "heater", "duty", 2.0, "KJ/h"),
                                           (4, "feed", "temperature", 30.0, "C")]:
            for col, text in [("A", obj), ("B", prop), ("D", unit)]:
                self.cells[f"{col}{row}"] = SimpleNamespace(CellText=text)
            self.cells[f"C{row}"] = SimpleNamespace(CellValue=value)
        self.control.table = SimpleNamespace(Cell=self.cells.__getitem__)

    def input(self, mv):
        self.path.write_text(json.dumps({"mv": mv}), encoding="utf-8")
        return self.path

    def test_reordered_input_and_symmetric_units(self):
        count = self.control.set_mv(self.input({"heater": {"duty": 18000},
                                               "feed": {"temperature": 42, "flow": 7200}}))
        self.assertEqual(count, 3)
        self.assertEqual([self.cells[f"C{r}"].CellValue for r in range(2, 5)], [2, 5, 42])
        self.assertEqual(self.control._read_section(self.control.MV_ROWS),
                         {"feed": {"flow": 7200, "temperature": 42}, "heater": {"duty": 18000}})

    def test_partial_input_leaves_other_values_unchanged(self):
        self.control.set_mv(self.input({"feed": {"temperature": 31}}), resume=False)
        self.assertEqual(self.cells["C2"].CellValue, 1)
        self.assertEqual(self.cells["C3"].CellValue, 2)
        self.assertFalse(self.control.case.Solver.CanSolve)

    def test_validation_is_completed_before_any_write(self):
        for invalid in [True, "32", float("nan"), float("inf"), -1e30]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.control.set_mv(self.input({"feed": {"flow": 7200, "temperature": invalid}}))
                self.assertEqual(self.cells["C2"].CellValue, 1)
                self.assertTrue(self.control.case.Solver.CanSolve)
        with self.assertRaisesRegex(ValueError, "Unknown MV"):
            self.control.set_mv(self.input({"feed": {"flow": 7200, "typo": 42}}))
        self.assertEqual(self.cells["C2"].CellValue, 1)

    def test_write_failure_rolls_back_and_restores_paused_state(self):
        self.control.case.Solver.CanSolve = False
        original = self.control.write_cell

        def fail(cell, value):
            if cell == "C3" and value == 5:
                raise RuntimeError("simulated COM failure")
            original(cell, value)

        with patch.object(self.control, "write_cell", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "simulated COM failure"):
                self.control.set_mv(self.input({"feed": {"flow": 7200}, "heater": {"duty": 18000}}))
        self.assertEqual(self.cells["C2"].CellValue, 1)
        self.assertEqual(self.cells["C3"].CellValue, 2)
        self.assertFalse(self.control.case.Solver.CanSolve)

    def test_failed_rollback_leaves_solver_paused(self):
        original = self.control.write_cell

        def fail(cell, value):
            if cell == "C3" or (cell == "C2" and value == 1):
                raise RuntimeError("simulated persistent failure")
            original(cell, value)

        with patch.object(self.control, "write_cell", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "rollback errors"):
                self.control.set_mv(self.input({"feed": {"flow": 7200}, "heater": {"duty": 18000}}))
        self.assertFalse(self.control.case.Solver.CanSolve)

    def test_duplicate_mapping_rejected(self):
        self.cells["B4"].CellText = "flow"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.control._mapping(self.control.MV_ROWS)

    def test_convergence_requires_idle_and_column_convergence(self):
        ready = dict(can_solve=True, is_solving=False, is_valid=True, column_converged=True)
        for changes, expected in [({}, 2), ({"is_solving": True}, 1),
                                  ({"column_converged": False}, 1), ({"is_valid": False}, 1),
                                  ({"can_solve": False}, 0)]:
            with patch.object(self.control, "solver_status", return_value=ready | changes):
                self.assertEqual(self.control.get_convergence_status(), expected)

    def test_wait_does_not_accept_a_transient_valid_state(self):
        ready = dict(can_solve=True, is_solving=False, is_valid=True, column_converged=True)
        statuses = [ready, ready | {"is_solving": True}, ready, ready, ready]
        with patch.object(self.control, "solver_status", side_effect=statuses), \
             patch("hysys_control.time.monotonic", side_effect=[0, 0, .5, 1, 1.5, 2]), \
             patch("hysys_control.time.sleep"):
            self.assertEqual(self.control.wait_for_convergence(timeout=5), ready)

    def test_wait_times_out(self):
        status = dict(can_solve=True, is_solving=False, is_valid=True, column_converged=False)
        with patch.object(self.control, "solver_status", return_value=status), \
             patch("hysys_control.time.monotonic", side_effect=[0, 0, 1]), \
             patch("hysys_control.time.sleep"):
            with self.assertRaises(TimeoutError):
                self.control.wait_for_convergence(timeout=1)

    def test_invalid_export_preserves_previous_file(self):
        self.path.write_text("original", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_json(self.path, {"value": float("nan")})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
