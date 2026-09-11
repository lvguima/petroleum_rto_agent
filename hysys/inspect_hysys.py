"""Read-only inspection of the local HYSYS automation interface."""
import json
from pathlib import Path

import win32com.client


def members(obj):
    info = obj._oleobj_.GetTypeInfo()
    return sorted({info.GetDocumentation(info.GetFuncDesc(i).memid)[0]
                   for i in range(info.GetTypeAttr().cFuncs)})


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    app = win32com.client.Dispatch("HYSYS.Application")
    app.Visible = True
    path = str(root / "mjh_ATM.hsc")
    case = next((app.SimulationCases.Item(i)
                 for i in range(app.SimulationCases.Count)
                 if app.SimulationCases.Item(i).FullName.casefold() == path.casefold()), None)
    if case is None:
        case = app.SimulationCases.Open(path)
    table = case.Flowsheet.Operations.Item("Table")
    column = case.Flowsheet.Operations.Item("C-1102")
    stage = column.ColumnFlowsheet.ColumnStages.Item(0).SeparationStage
    report = {"case": case.FullName,
              "solver_members": members(case.Solver),
              "case_members": members(case),
              "column_members": members(column),
              "column_flowsheet_members": members(column.ColumnFlowsheet),
              "stage_members": members(stage),
              "can_solve": case.Solver.CanSolve, "is_valid": case.IsValid,
              "table": [[r, table.Cell(f"A{r}").CellText,
                         table.Cell(f"B{r}").CellText,
                         table.Cell(f"C{r}").CellValue,
                         table.Cell(f"D{r}").CellText] for r in [*range(2, 26), *range(28, 64)]]}
    out = root / "artifacts"
    out.mkdir(exist_ok=True)
    (out / "hysys_inspection.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
