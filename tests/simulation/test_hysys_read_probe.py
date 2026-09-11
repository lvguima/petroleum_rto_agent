"""Offline boundary tests. No HYSYS process or COM import is required."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.simulation import probe_hysys_read as probe


class FakeComError(Exception):
    hresult = -2147352567


class FakePythonCom:
    def __init__(self, events):
        self.events = events
        self.fail_initialize = False

    def CoInitialize(self):
        self.events.append(("initialize", threading.get_ident()))
        if self.fail_initialize:
            raise FakeComError("cannot initialize")

    def CoUninitialize(self):
        self.events.append(("uninitialize", threading.get_ident()))


class FakeValue:
    def __init__(self, value, unit):
        self.value = value
        self.unit = unit

    def GetValue(self, unit):
        assert unit == self.unit
        return self.value


class FakeTable:
    def __init__(self):
        self.cells = {}
        self.value_reads = 0
        self.on_cell = None
        for row in [*probe.MV_ROWS, *probe.CV_ROWS]:
            for column, value in (("A", "equipment"), ("B", f"property_{row}"), ("D", "kg/h")):
                self.cells[f"{column}{row}"] = SimpleNamespace(CellText=value)
            self.cells[f"C{row}"] = SimpleNamespace(CellValue=row / 10)

    def Cell(self, cell):
        if self.on_cell is not None:
            self.on_cell(cell)
        if cell.startswith("C"):
            self.value_reads += 1
        return self.cells[cell]


class FakeStages:
    def __init__(self):
        self.values = [
            SimpleNamespace(
                Name=f"stage_{index}",
                SeparationStage=SimpleNamespace(
                    Pressure=FakeValue(150 + index, "kPa"),
                    Temperature=FakeValue(100 + index, "C"),
                    MassLiquidFlow=FakeValue(1000 + index, "kg/h"),
                    MassVapourFlow=FakeValue(2000 + index, "kg/h"),
                ),
            )
            for index in range(73)
        ]

    @property
    def Count(self):
        return len(self.values)

    def Item(self, index):
        return self.values[index]


class FakeSolver:
    def __init__(self):
        self.is_solving = False
        self.on_read = None

    @property
    def CanSolve(self):
        return True

    @CanSolve.setter
    def CanSolve(self, value):
        raise AssertionError("must not write CanSolve")

    @property
    def IsSolving(self):
        if self.on_read:
            self.on_read()
        return self.is_solving


class FakeOperations:
    def __init__(self, table, column):
        self.values = {"Table": table, "C-1102": column}

    def Item(self, name):
        return self.values[name]


class FakeCase:
    def __init__(self, path):
        self.FullName = str(path)
        self.table = FakeTable()
        self.stages = FakeStages()
        self.column = SimpleNamespace(
            ColumnFlowsheet=SimpleNamespace(CfsConverged=True, ColumnStages=self.stages)
        )
        self.Flowsheet = SimpleNamespace(Operations=FakeOperations(self.table, self.column))
        self.Solver = FakeSolver()
        self.IsValid = True

    def Activate(self):
        raise AssertionError("must not activate case")

    def Close(self):
        raise AssertionError("must not close case")

    def Save(self):
        raise AssertionError("must not save case")


class FakeCases:
    def __init__(self, source, events):
        self.existing = [FakeCase(source)]
        self.events = events
        self.on_open = None
        self.opened = None

    @property
    def Count(self):
        return len(self.existing)

    def Item(self, index):
        return self.existing[index]

    def Open(self, path):
        self.events.append(("open", path))
        self.opened = FakeCase(path)
        if self.on_open:
            self.on_open(self.opened)
        return self.opened


class FakeApp:
    def __init__(self, cases):
        self.SimulationCases = cases
        info = SimpleNamespace(
            GetTypeAttr=lambda: SimpleNamespace(cFuncs=0),
            GetDocumentation=lambda index: ("FakeHysysApp",),
        )
        self._oleobj_ = SimpleNamespace(GetTypeInfo=lambda: info)

    @property
    def Visible(self):
        raise AssertionError("must not inspect or change Visible")

    @Visible.setter
    def Visible(self, value):
        raise AssertionError("must not change Visible")

    def Quit(self):
        raise AssertionError("must not quit application")


class FakeClient:
    def __init__(self, app, events):
        self.app = app
        self.events = events
        self.fail_connect = False

    def GetActiveObject(self, name):
        self.events.append(("attach", name))
        if self.fail_connect:
            raise FakeComError("no active object")
        return self.app

    def DispatchEx(self, name):
        self.events.append(("dispatch_ex", name))
        if self.fail_connect:
            raise FakeComError("connection failed")
        return self.app


class ReadProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "原始模型.hsc"
        self.source.write_bytes(b"unchanged model bytes")
        self.outputs = self.root / "runs"
        self.events = []
        self.pythoncom = FakePythonCom(self.events)
        self.cases = FakeCases(self.source, self.events)
        self.client = FakeClient(FakeApp(self.cases), self.events)
        self.loader = patch.object(probe, "_load_com", return_value=(self.pythoncom, self.client))
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def run_probe(self, *, attach=False):
        return probe.run_probe(self.source, self.outputs, attach_existing=attach)

    def snapshot(self, result):
        return json.loads((result[0] / "snapshot.json").read_text(encoding="utf-8"))

    def test_disk_copy_and_outputs_preserve_source_and_earlier_runs(self):
        original = self.source.read_bytes()
        result = self.run_probe()
        run_dir, report = result
        self.assertEqual(report["status"], "observed_stable")
        self.assertEqual(self.cases.opened.FullName, str(run_dir / self.source.name))
        self.assertEqual((run_dir / self.source.name).read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(report["source"]["disk_unchanged"])
        before = (run_dir / "report.json").read_bytes()
        second, _ = self.run_probe()
        self.assertNotEqual(run_dir, second)
        self.assertEqual((run_dir / "report.json").read_bytes(), before)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["counts"], {"mv": 24, "cv": 36, "stage": 73})
        self.assertEqual(snapshot["first_sample"]["mv"][0]["raw_cell_value"], 0.2)
        self.assertEqual(snapshot["first_sample"]["mv"][0]["unit_label"], "kg/h")
        stage = snapshot["first_sample"]["stage"][0]
        self.assertEqual((stage["liquid_kg_h"], stage["vapor_kg_h"]), (1000, 2000))
        self.assertFalse(snapshot["assessment"]["eligible_for_prepare"])

    def test_attach_only_reads_exact_existing_case_without_opening(self):
        result = self.run_probe(attach=True)
        report = result[1]
        self.assertEqual(report["status"], "observed_stable")
        self.assertEqual(
            [event[0] for event in self.events], ["initialize", "attach", "uninitialize"]
        )
        self.assertEqual(report["source"]["kind"], "existing_case_memory")
        self.assertFalse(report["source"]["memory_state_bound_to_disk_hash"])
        self.assertFalse(report["ownership"]["probe_opened_case"])
        self.assertEqual(len(list(result[0].iterdir())), 2)

    def test_attach_missing_case_never_opens_disk_or_launches_app(self):
        self.cases.existing.clear()
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["errors"][0]["code"], "case_match_count")
        self.assertNotIn("open", [event[0] for event in self.events])
        self.assertNotIn("dispatch_ex", [event[0] for event in self.events])

    def test_attach_rejects_duplicate_fullname(self):
        self.cases.existing.append(FakeCase(self.source))
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["errors"][0]["code"], "case_match_count")

    def test_table_rejects_missing_fields_and_duplicate_cross_section_mapping(self):
        for field in ("A2", "B2", "D2"):
            with self.subTest(field=field):
                self.cases.existing[0].table = FakeTable()
                table = self.cases.existing[0].table
                table.cells[field].CellText = " "
                with self.assertRaisesRegex(probe.ProbeValidationError, "nonempty"):
                    probe.read_table(table)
        table = FakeTable()
        table.cells["B28"].CellText = table.cells["B2"].CellText
        with self.assertRaises(probe.ProbeValidationError) as raised:
            probe.read_table(table)
        self.assertEqual(raised.exception.code, "duplicate_mapping")

    def test_nonfinite_undefined_and_nonnumeric_values_are_rejected(self):
        for value in (float("nan"), float("inf"), -1e30, None, "1.0", True):
            with self.subTest(value=value):
                table = FakeTable()
                table.cells["C2"].CellValue = value
                with self.assertRaises(probe.ProbeValidationError):
                    probe.read_table(table)

    def test_incomplete_or_duplicate_stages_are_rejected(self):
        case = self.cases.existing[0]
        case.stages.values.pop()
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["errors"][0]["code"], "stage_count")
        self.assertIsNone(report["snapshot"])
        case.stages.values.append(case.stages.values[0])
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["errors"][0]["code"], "duplicate_stage")

    def test_missing_required_stage_property_is_recorded(self):
        case = self.cases.existing[0]
        del case.stages.values[0].SeparationStage.MassVapourFlow
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["errors"][0]["phase"], "read_first_sample")
        self.assertEqual(report["errors"][0]["exception_type"], "AttributeError")

    def test_changed_cv_and_stage_mark_observation_unstable(self):
        case = self.cases.existing[0]

        def change_after_first_read(cell):
            if case.table.value_reads >= 60:
                case.table.cells["C28"].CellValue = 999
                case.stages.values[0].SeparationStage.Temperature.value = 333

        case.table.on_cell = change_after_first_read
        result = self.run_probe(attach=True)
        self.assertEqual(result[1]["status"], "observed_unstable")
        assessment = self.snapshot(result)["assessment"]
        self.assertEqual(assessment["reasons"], ["cv_changed", "stage_changed"])
        self.assertFalse(assessment["consistent_observation"])
        self.assertFalse(assessment["eligible_for_prepare"])

    def test_solver_changes_and_nonconvergence_are_observation_conditions(self):
        case = self.cases.existing[0]
        calls = 0

        def solver_change():
            nonlocal calls
            calls += 1
            case.Solver.is_solving = calls == 1

        case.Solver.on_read = solver_change
        case.column.ColumnFlowsheet.CfsConverged = False
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["status"], "observed_unstable")
        self.assertEqual(report["errors"], [])
        self.assertIn("solver_status_changed", report["assessment"]["reasons"])
        self.assertIn("solver_was_solving", report["assessment"]["reasons"])
        self.assertIn("column_not_converged", report["assessment"]["reasons"])

    def test_invalid_status_is_not_coerced_to_success(self):
        self.cases.existing[0].IsValid = "False"
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["errors"][0]["code"], "invalid_status")

    def test_changed_mv_and_mapping_are_not_hidden_by_equal_outputs(self):
        case = self.cases.existing[0]

        def change_after_first_read(cell):
            if case.table.value_reads >= 60:
                case.table.cells["C2"].CellValue = 999
                case.table.cells["D2"].CellText = "kg/s"

        case.table.on_cell = change_after_first_read
        _, report = self.run_probe(attach=True)
        self.assertEqual(report["status"], "observed_unstable")
        self.assertEqual(report["assessment"]["reasons"], ["mv_changed"])

    def test_com_objects_and_failure_traceback_are_released_before_uninitialize(self):
        for fail_read in (False, True):
            with self.subTest(fail_read=fail_read):
                references = []

                def make_app(name, references=references, fail_read=fail_read):
                    cases = FakeCases(self.source, self.events)

                    def opened(case):
                        references.append(weakref.ref(case))
                        if fail_read:

                            def failure(cell):
                                raise FakeComError("read failed")

                            case.table.on_cell = failure

                    cases.on_open = opened
                    app = FakeApp(cases)
                    references.append(weakref.ref(app))
                    return app

                def uninitialize(references=references):
                    self.assertTrue(references)
                    self.assertTrue(all(reference() is None for reference in references))

                pythoncom = SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=uninitialize)
                client = SimpleNamespace(DispatchEx=make_app)
                with patch.object(probe, "_load_com", return_value=(pythoncom, client)):
                    _, report = self.run_probe()
                self.assertTrue(report["com_lifecycle"]["uninitialized"])
                if fail_read:
                    self.assertEqual(report["errors"][0]["hresult"], FakeComError.hresult)
                    self.assertEqual(report["errors"][0]["phase"], "read_first_sample")
                else:
                    self.assertEqual(report["status"], "observed_stable")

    def test_com_error_retains_phase_type_hresult_and_balances_same_thread(self):
        self.client.fail_connect = True
        _, report = self.run_probe()
        error = report["errors"][0]
        self.assertEqual(error["phase"], "connect_application")
        self.assertEqual(error["exception_type"], "FakeComError")
        self.assertEqual(error["hresult"], FakeComError.hresult)
        self.assertEqual(error["code"], "com_error")
        self.assertEqual(self.events[0][1], self.events[-1][1])
        self.assertEqual(report["com_lifecycle"], {"initialized": True, "uninitialized": True})
        self.assertTrue(report["source"]["disk_unchanged"])

    def test_com_open_error_preserves_server_scode_source_and_description(self):
        for scode in (-1072879842, -2147024891):
            with self.subTest(scode=scode):
                error = FakeComError("generic outer message")
                error.excepinfo = (
                    0,
                    "HYSYS.SimulationCases",
                    "Server could not open the case",
                    None,
                    0,
                    scode,
                )
                with patch.object(self.cases, "Open", side_effect=error):
                    run_dir, report = self.run_probe()
                self.assertEqual(report["status"], "failed")
                saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
                recorded = saved["errors"][0]
                self.assertEqual(recorded["phase"], "open_copy")
                self.assertEqual(recorded["hresult"], FakeComError.hresult)
                self.assertEqual(
                    recorded["excepinfo"],
                    {
                        "scode": scode,
                        "source": "HYSYS.SimulationCases",
                        "description": "Server could not open the case",
                    },
                )
                self.assertTrue(report["source"]["disk_unchanged"])
                self.assertTrue(report["com_lifecycle"]["uninitialized"])

    def test_absent_or_malformed_excepinfo_does_not_hide_original_error(self):
        for excepinfo in (None, "not a tuple", (0,), (0, object(), 12, None, 0, True)):
            with self.subTest(excepinfo=excepinfo):
                error = FakeComError("original failure")
                error.excepinfo = excepinfo
                recorded = probe.error_record(error, "open_copy")
                self.assertEqual(recorded["message"], "original failure")
                self.assertEqual(recorded["hresult"], FakeComError.hresult)
                json.dumps(recorded, allow_nan=False)
                expected = (
                    {"scode": None, "source": None, "description": None}
                    if isinstance(excepinfo, tuple) and len(excepinfo) == 6
                    else None
                )
                self.assertEqual(recorded["excepinfo"], expected)

    def test_failed_initialization_does_not_uninitialize(self):
        self.pythoncom.fail_initialize = True
        _, report = self.run_probe()
        self.assertEqual(report["errors"][0]["phase"], "initialize_com")
        self.assertEqual([event[0] for event in self.events], ["initialize"])
        self.assertEqual(report["com_lifecycle"], {"initialized": False, "uninitialized": False})

    def test_missing_com_dependency_is_saved_as_a_structured_error(self):
        with patch.object(probe, "_load_com", side_effect=ModuleNotFoundError("pythoncom")):
            _, report = self.run_probe()
        self.assertEqual(report["errors"][0]["phase"], "load_com")
        self.assertEqual(self.events, [])

    def test_source_drift_invalidates_otherwise_complete_snapshot(self):
        self.cases.on_open = lambda case: self.source.write_bytes(b"external change")
        result = self.run_probe()
        report = result[1]
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["source"]["disk_unchanged"])
        self.assertEqual(report["errors"][0]["phase"], "verify_source_after_read")
        self.assertEqual(report["errors"][0]["code"], "source_changed")
        self.assertFalse(self.snapshot(result)["assessment"]["consistent_observation"])

    def test_wrong_opened_identity_stops_before_sampling(self):
        self.cases.on_open = lambda case: setattr(case, "FullName", str(self.source))
        _, report = self.run_probe()
        self.assertEqual(report["errors"][0]["code"], "wrong_case")
        self.assertEqual(self.cases.opened.table.value_reads, 0)

    def test_source_validation_and_existing_outputs_do_not_call_com_or_overwrite(self):
        self.source.unlink()
        run_dir, report = self.run_probe()
        self.assertEqual(report["errors"][0]["code"], "invalid_case_file")
        self.assertEqual(self.events, [])
        saved = (run_dir / "report.json").read_bytes()
        with self.assertRaises(FileExistsError):
            probe.write_new_json(run_dir / "report.json", {"overwrite": True})
        self.assertEqual((run_dir / "report.json").read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
