"""Automation interface for the Table spreadsheet in mjh_ATM.hsc."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import time

import pythoncom
import win32com.client


ROOT = Path(__file__).resolve().parent


def project_path(path):
    path = Path(path).expanduser()
    return path if path.is_absolute() else ROOT / path


def write_json(path, data):
    """Replace a JSON file only after serialization and writing succeed."""
    path = project_path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=4, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False, suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(payload + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value <= -1e20:
        raise ValueError(f"{label}: invalid or undefined HYSYS value {value!r}")
    return value


class HYSYSControl:
    MV_ROWS = range(2, 26)
    CV_ROWS = range(28, 64)

    def __init__(self, file_name, table_name="Table", column_name="C-1102", visible=True):
        file_path = project_path(file_name).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        self.file_name = str(file_path)
        self.column_name = column_name
        self.hysys = win32com.client.Dispatch("HYSYS.Application")
        self.hysys.Visible = visible
        self.case = None
        cases = self.hysys.SimulationCases
        for i in range(cases.Count):
            case = cases.Item(i)
            if os.path.normcase(os.path.abspath(case.FullName)) == os.path.normcase(str(file_path)):
                self.case = case
                break
        if self.case is None:
            self.case = cases.Open(str(file_path))
        self.case.Activate()
        self.flowsheet = self.case.Flowsheet
        self.table = self.flowsheet.Operations.Item(table_name)
        self.column = self.flowsheet.Operations.Item(column_name)

    def suspend(self):
        self.case.Solver.CanSolve = False

    def resume(self):
        self.case.Solver.CanSolve = True

    def read_cell_value(self, cell):
        return self.table.Cell(cell).CellValue

    def read_cell_text(self, cell):
        return self.table.Cell(cell).CellText

    def write_cell(self, cell, value):
        self.table.Cell(cell).CellValue = finite_number(value, cell)

    @staticmethod
    def unit_factor(unit):
        # CellValue uses internal kg/s and kJ/s; D labels desired JSON units.
        # Other supported units match this model's values, including % fields.
        unit = str(unit or "").strip().lower().replace(" ", "")
        if unit in {"kg/h", "kj/h"}:
            return 3600.0
        if unit in {"", "c", "°c", "kpa", "%", "kg/s", "kj/s", "kw"}:
            return 1.0
        raise ValueError(f"Unsupported Table unit: {unit!r}")

    def _mapping(self, rows):
        result = {}
        for row in rows:
            prop = str(self.read_cell_text(f"B{row}") or "").strip()
            if not prop:
                continue
            obj = str(self.read_cell_text(f"A{row}") or "").strip()
            if not obj:
                raise ValueError(f"Missing object name at A{row}")
            key = (obj, prop)
            if key in result:
                raise ValueError(f"Duplicate Table entry: {key}")
            result[key] = (f"C{row}", self.unit_factor(self.read_cell_text(f"D{row}")))
        return result

    def _read_section(self, rows):
        result = {}
        for (obj, prop), (cell, factor) in self._mapping(rows).items():
            value = finite_number(self.read_cell_value(cell), f"{cell} ({obj}.{prop})")
            result.setdefault(obj, {})[prop] = value * factor
        return result

    def get_tower_status(self, column_name=None):
        column = self.column if column_name is None else self.flowsheet.Operations.Item(column_name)
        stages = column.ColumnFlowsheet.ColumnStages
        data = {}
        for i in range(stages.Count):
            stage = stages.Item(i)
            sep = stage.SeparationStage
            name = stage.Name
            if name in data:
                raise ValueError(f"Duplicate column stage name: {name}")
            data[name] = {
                "pressure_kPa": finite_number(sep.Pressure.GetValue("kPa"), name),
                "temperature_C": finite_number(sep.Temperature.GetValue("C"), name),
                "liquid_kg_h": finite_number(sep.MassLiquidFlow.GetValue("kg/h"), name),
                "vapor_kg_h": finite_number(sep.MassVapourFlow.GetValue("kg/h"), name),
            }
        return data

    def get_operating_point(self, file_name=None):
        op = {"mv": self._read_section(self.MV_ROWS),
              "cv": self._read_section(self.CV_ROWS),
              "stage": self.get_tower_status()}
        if file_name is not None:
            write_json(file_name, op)
        return op

    def set_mv(self, file_name, *, resume=True):
        """Write full or partial MV JSON by name, independent of JSON ordering.

        Validate before touching the solver. Restore old values on a write
        failure; if rollback fails, leave the solver paused.
        """
        with project_path(file_name).open(encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get("mv"), dict) or not data["mv"]:
            raise ValueError("Input must contain a nonempty 'mv' object")
        mapping = self._mapping(self.MV_ROWS)
        changes = []
        for obj, properties in data["mv"].items():
            if not isinstance(properties, dict) or not properties:
                raise ValueError(f"{obj}: expected a nonempty property object")
            for prop, value in properties.items():
                key = (obj, prop)
                if key not in mapping:
                    raise ValueError(f"Unknown MV: {obj}.{prop}")
                cell, factor = mapping[key]
                target = finite_number(value, f"{obj}.{prop}") / factor
                changes.append((cell, target, finite_number(self.read_cell_value(cell), cell)))
        was_enabled = bool(self.case.Solver.CanSolve)
        self.suspend()
        attempted = []
        try:
            for cell, target, old in changes:
                attempted.append((cell, old))
                self.write_cell(cell, target)
                actual = finite_number(self.read_cell_value(cell), cell)
                if not math.isclose(actual, target, rel_tol=1e-8, abs_tol=1e-8):
                    raise RuntimeError(f"Write verification failed at {cell}: {actual} != {target}")
        except Exception as error:
            rollback_errors = []
            for cell, old in reversed(attempted):
                try:
                    self.write_cell(cell, old)
                    if not math.isclose(self.read_cell_value(cell), old, rel_tol=1e-8, abs_tol=1e-8):
                        raise RuntimeError("rollback verification failed")
                except Exception as rollback_error:
                    rollback_errors.append(f"{cell}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError("MV write failed; solver paused; rollback errors: "
                                   + "; ".join(rollback_errors)) from error
            self.case.Solver.CanSolve = was_enabled
            raise
        if resume:
            self.resume()
        return len(changes)

    def solver_status(self):
        solver = self.case.Solver
        return {"can_solve": bool(solver.CanSolve),
                "is_solving": bool(solver.IsSolving),
                "is_valid": bool(self.case.IsValid),
                "column_converged": bool(self.column.ColumnFlowsheet.CfsConverged)}

    def get_convergence_status(self):
        """Legacy codes: 0 paused, 1 solving/not converged, 2 converged."""
        status = self.solver_status()
        if not status["can_solve"]:
            return 0
        return 2 if (not status["is_solving"] and status["is_valid"]
                     and status["column_converged"]) else 1

    def wait_for_convergence(self, timeout=120.0, poll_interval=0.5, stable_seconds=1.0):
        """Wait for an idle valid case and a converged column to remain stable.

        Timeout bounds polling, not a blocking call inside the COM server.
        """
        for label, value in (("timeout", timeout), ("poll_interval", poll_interval),
                             ("stable_seconds", stable_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be finite and positive")
        deadline = time.monotonic() + timeout
        stable_since = None
        while True:
            pythoncom.PumpWaitingMessages()
            status = self.solver_status()
            now = time.monotonic()
            if not status["can_solve"]:
                raise RuntimeError("Solver is suspended; call resume() before waiting")
            ready = not status["is_solving"] and status["is_valid"] and status["column_converged"]
            if ready:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= stable_seconds:
                    return status
            else:
                stable_since = None
            if now >= deadline:
                raise TimeoutError(f"HYSYS did not converge within {timeout}s: {status}")
            time.sleep(min(poll_interval, deadline - now))
