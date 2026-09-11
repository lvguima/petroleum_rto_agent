import win32com.client
import os
import json


class HYSYSControl:
    def __init__(self,file_name):

        self.file_name = file_name

        #工程文件地址（脚本所在当前目录）
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, file_name)

        #启动HYSYS这个软件
        self.hysys = win32com.client.Dispatch("HYSYS.Application")
        self.hysys.Visible = True

        #查询需要使用的文件是否已打开，避免重复打开
        self.case = None
        cases = self.hysys.SimulationCases
        for i in range(cases.Count):
            case = cases.Item(i)
            if case.FullName == file_path:
                print("Case already exists")
                self.case = case
                break
        if(self.case == None):
            self.case = self.hysys.SimulationCases.Open(file_path)
            self.case.Activate()

        self.flowsheet = self.case.Flowsheet
        self.table = self.flowsheet.Operations.Item("Table")

    #挂起，模型不解算
    def suspend(self):
        self.case.Solver.CanSolve = False

    #启用，模型解算
    def resume(self):
        self.case.Solver.CanSolve = True

    #读取工作表中单元的数值
    def read_cell_value(self,cell):
        return self.table.Cell(cell).CellValue

    # 读取工作表中单元的文本
    def read_cell_text(self,cell):
        return self.table.Cell(cell).CellText

    #写入工作表中单元的数值
    def write_cell(self, cell, value):
        self.table.Cell(cell).CellValue = value


    #获取塔的初始收敛数据
    def get_tower_status(self,column_name):

        #获取塔
        column = self.case.Flowsheet.Operations.Item(column_name)

        #进入塔内流程
        column_flowsheet = column.ColumnFlowsheet

        #获取所有塔板
        stages = column_flowsheet.ColumnStages

        data = {}

        for i in range(stages.Count):

            stage = stages.Item(i)

            sep = stage.SeparationStage

            data[stage.Name] =  \
                {
                    "pressure_kPa": sep.PressureValue,
                    "temperature_C": sep.TemperatureValue,

                    "liquid_kg_h": sep.MassLiquidFlowValue * 3600,
                    "vapor_kg_h": sep.MassLiquidFlowValue * 3600

                }

        return data

    #获取所有参数，包括精馏塔的初始收敛数据、控制量和被控量
    def get_operating_point(self,file_name):

        mv = {}
        for row in range(2, 26):

            obj = self.table.Cell( f"A{row}").CellText

            prop = self.table.Cell(f"B{row}").CellText

            value = self.table.Cell(f"C{row}").CellValue

            unit = self.table.Cell(f"D{row}").CellText

            if unit == "kg/h" or unit == "KJ/h":
                value = value * 3600

            if prop:

                if obj not in mv:
                    mv[obj] = {}

                mv[obj][prop] = value

        cv = {}
        for row in range(28, 64):

            obj = self.table.Cell(f"A{row}").CellText

            prop = self.table.Cell(f"B{row}").CellText

            value = self.table.Cell(f"C{row}").CellValue

            unit = self.table.Cell(f"D{row}").CellText

            if unit == "kg/h" or unit == "KJ/h":
                value = value * 3600

            if prop:

                if obj not in cv:
                    cv[obj] = {}

                cv[obj][prop] = value

        stage = self.get_tower_status("C-1102")

        op = {}
        op["mv"] = mv
        op["cv"] = cv
        op["stage"] = stage

        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(
                op,
                f,
                ensure_ascii=False,
                indent=4
            )
        return op

    def set_mv(self,file_name):

        # 读取 JSON
        with open(file_name, "r", encoding="utf-8") as f:
            data = json.load(f)

        mv = data["mv"]

        # 暂停解算
        self.suspend()

        try:
            row = 2

            # 按 JSON 顺序读取所有控制量
            for obj_name, properties in mv.items():

                for property_name, value in properties.items():
                    if "kg_h" in property_name :
                        value = value / 3600
                    # 直接依次写入 C 列
                    self.write_cell(f"C{row}", value)

                    row += 1

        finally:
            # 恢复解算
            self.resume()

        print("MV 写入完成。")


    #获取模型的收敛情况。0-不解算，1-解算但不收敛，2-收敛
    def get_convergence_status(self):

        solver = self.case.Solver

        can_solve = solver.CanSolve

        case_valid = self.case.IsValid

        # 判断状态
        if not can_solve:
            return 0

        if can_solve and not case_valid:
            return 1

        if can_solve and case_valid:
            return 2




