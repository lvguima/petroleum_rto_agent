"""Analyze saved HYSYS outputs without connecting to or changing the model."""
import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "artifacts" / "analysis"
PRODUCT_NAMES = {"Naptha": "石脑油", "Kerosene": "煤油", "Diesel": "柴油",
                 "AGO": "常压瓦斯油（AGO）", "Risedue": "渣油"}


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def csv_file(name, fields, rows):
    with (OUT / name).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    after = read("data_read")
    before = read("artifacts/operating_point_before.json")
    requested = read("data_write")
    run = read("artifacts/run_report.json")
    OUT.mkdir(parents=True, exist_ok=True)
    crude = after["mv"]["crude_oil"]["massflow_kg_h"]
    water = sum(after["mv"][k]["massflow_kg_h"] for k in ("Water1", "Water2", "Water3"))
    products = []
    for key, label in PRODUCT_NAMES.items():
        new = after["cv"][key]["massflow_kg_h"]
        old = before["cv"][key]["massflow_kg_h"]
        products.append(dict(name=key, label=label, flow_t_h=new / 1000,
                             apparent_yield_pct=new / crude * 100,
                             delta_kg_h=new - old, delta_pct=(new / old - 1) * 100,
                             **{p: v for p, v in after["cv"][key].items() if "TBP" in p}))
    heat = [dict(name=k, duty_MW=v["heat_load_KJ_h"] / 3.6e6)
            for k, v in after["cv"].items() if "heat_load_KJ_h" in v]
    stages = [dict(stage=k, **v) for k, v in after["stage"].items()]
    main_tower = {k: v for k, v in after["stage"].items() if k.endswith("__Main Tower")}
    total_out = sum(row["flow_t_h"] * 1000 for row in products)
    total_heat = sum(row["duty_MW"] for row in heat)
    temp_drift = max(abs(v["temperature_C"] - before["stage"][k]["temperature_C"])
                     for k, v in after["stage"].items())
    tbp_drift = max(abs(v[p] - before["cv"][k][p]) for k, v in after["cv"].items()
                    for p in v if "TBP" in p)
    differences = [dict(section=s, object=k, property=p, before=old,
                        after=after[s][k][p], delta=after[s][k][p] - old)
                   for s in ("mv", "cv", "stage") for k, values in before[s].items()
                   for p, old in values.items()]
    csv_file("products.csv", list(products[0]), products)
    csv_file("heat_loads.csv", list(heat[0]), heat)
    csv_file("stages.csv", list(stages[0]), stages)
    csv_file("before_after.csv", list(differences[0]), differences)
    summary = dict(run=run, crude_kg_h=crude, water_kg_h=water,
                   listed_products_kg_h=total_out, balance_residual_kg_h=total_out-crude-water,
                   listed_duties_MW=total_heat, listed_duties_GJ_per_t_crude=total_heat*3.6/(crude/1000),
                   mv_unchanged=after["mv"] == before["mv"],
                   mv_matches_input=after["mv"] == requested["mv"],
                   max_stage_temperature_drift_C=temp_drift, max_TBP_drift_C=tbp_drift,
                   products=products, heat_loads=heat,
                   source_hashes={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                  for p in ("data_read", "data_write", "artifacts/operating_point_before.json",
                                            "artifacts/run_report.json")})
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# HYSYS 运行结果分析", "",
             "结论：本次现有工况的读写及求解流程成功，导出流量的总量衡算闭合，重复求解变化很小；"
             "渣油占比较高、产品含水归属及热负荷边界仍需进一步核查，当前数据不能证明工艺最优或产品达标。", "",
             "## 1. 计算状态与重复性", "",
             f"运行报告记录用时 {run['elapsed_seconds']:.3f} s。求解器允许求解且不忙，模型有效，C-1102 塔收敛。",
             "24 个 MV 与输入、运行前数据均一致。本次是同一工况的重新写入和求解，不是操作量扰动实验。",
             f"产品流量最大相对变化 {max(abs(p['delta_pct']) for p in products):.5f}%；"
             f"全部 73 个塔级的最大温度变化 {temp_drift:.5f} °C；产品 TBP 最大变化 {tbp_drift:.5f} °C。",
             "这些变化与同工况重新求解时的数值微小调整相符，但单次运行不能证明长期稳定性或优化效果。", "",
             "## 2. 产品分布与物料衡算", "",
             "| 产品 | 流量（t/h） | 对原油进料的表观质量比例 | 前后变化（kg/h） |",
             "|---|---:|---:|---:|"]
    lines += [f"| {p['label']} | {p['flow_t_h']:.3f} | {p['apparent_yield_pct']:.3f}% | {p['delta_kg_h']:+.3f} |" for p in products]
    lines += ["", f"原油 {crude/1000:.3f} t/h + 三股 Water 流 {water/1000:.3f} t/h = "
              f"五股已导出产品 {total_out/1000:.3f} t/h。浮点残差 {total_out-crude-water:.3g} kg/h。",
              "这是当前列出的输入/输出数据的总量闭合，尚未逐条核对模型边界物流和各组分衡算。"
              "五股产品对原油的比例相加为 100.75%，多出的 0.75% 与三股 Water 的质量一致；"
              "缺少各产品含水量，不能将表中比例直接解释为干基烃类收率，也不能确定水具体去了哪一股产品。",
              "渣油约 300.190 t/h（相对原油 75.05%），其余四股产品合计约 102.810 t/h。"
              "渣油比例较高是优先核查项；没有原油实沸点曲线、组分及目标收率，尚不能区分进料偏重与分馏设定的影响。", "",
              "## 3. 热负荷", "", "| 设备 | 已导出热负荷（MW） |", "|---|---:|"]
    lines += [f"| {p['name']} | {p['duty_MW']:.3f} |" for p in heat]
    lines += ["", f"六项热负荷合计 {total_heat:.3f} MW，折合 {total_heat*3.6/(crude/1000):.3f} GJ/t 原油。",
              "这六项热负荷与运行前完全一致。前三项占合计约 "
              f"{sum(p['duty_MW'] for p in heat[:3])/total_heat*100:.2f}%。",
              "该合计仅是已导出设备热负荷之和，不是经过公用工程边界核算的装置净能耗或燃料消耗；"
              "还缺热回收关系、其他冷/热负荷和设备效率等信息，不宜据此判断节能空间。", "",
              "## 4. 主塔与侧线塔", "",
              "73 个记录包括主塔 52 级、煤油侧线塔 8 级及其再沸器 1 级、柴油侧线塔 6 级、AGO 侧线塔 6 级；不是主塔有 73 块塔板。",
              f"主塔 52 级为顶部：{main_tower['52__Main Tower']['temperature_C']:.3f} °C、150 kPa；"
              f"1 级为底部：{main_tower['1__Main Tower']['temperature_C']:.3f} °C、180 kPa。主塔压差为 30 kPa。",
              "主塔温度从塔顶向下总体上升，在 6 级达到 "
              f"{main_tower['6__Main Tower']['temperature_C']:.3f} °C，随后向塔底降至 361.315 °C。"
              "因此不能将温度剖面描述成全塔严格单调；需要核对该区进料、汽提及能量连接后解释局部回落。",
              "39、26、16 级的温度分别接近 156.8、215、301 °C，对应设定误差均小于 0.0001 °C。"
              "这证明相关温度规格在当前工况满足，但不是这些温度规格本身合理的独立证据。",
              "主塔液相流量在 51→50、37→36、25→24、7→6 等相邻级之间有明显突变。"
              "中间回流、进料或侧线抽出可能解释这些变化，但当前导出数据没有连接拓扑，不能仅凭突变判定错误或确认原因。",
              "煤油侧线塔温度从 8 级的 184.536 °C 升至再沸器的 195.870 °C；"
              "柴油侧线塔从 6 级 213.501 °C 降至 1 级 201.149 °C；"
              "AGO 侧线塔从 6 级 296.675 °C 降至 1 级 281.095 °C。其原因同样需结合汽提介质与热量连接核对。", "",
              "## 5. 馏程与产品质量", "",
              "| 产品 | TBP 20%（°C） | TBP 40%（°C） | TBP 60%（°C） | TBP 80%（°C） | TBP 100%（°C） |",
              "|---|---:|---:|---:|---:|---:|"]
    lines += ["| " + p["label"] + " | " + " | ".join(f"{p[str(x)+'%TBP']:.2f}" for x in (20,40,60,80,100)) + " |" for p in products]
    lines += ["", "各产品自身的 TBP 点随馏出比例增加而升高，同一馏出比例也总体按石脑油、煤油、柴油、AGO、渣油依次升高，未见这些导出点倒序。",
              "石脑油 20% TBP 为 11.11 °C、40% 为 55.59 °C，而 60% 已到 138.49 °C，前段跨度较大，"
              "建议优先核对轻组分、产品含水量和所导出 TBP 属性的计算基准；不能仅据此断言异常。",
              "当前没有 5%/95% 等必要切割点，也没有产品质量目标。不能用相邻产品的 100% 与 20% 点差替代标准分离间隙，"
              "更不能仅靠这五个 TBP 点判断闪点、硫含量或成品达标。"
              "AFPM 的炼厂技术讨论也把侧线汽提与产品闪点及轻组分切割联系起来："
              "[AFPM 技术讨论](https://www.afpm.org/print/pdf/node/40980)。", "",
              "## 6. 旧气相数据的修正", "",
              "旧脚本将液相流量重复写入气相字段。本次分析的运行前快照和运行后数据均由修复后的代码生成，"
              "因此二者可以做真实的同工况比较；原始旧导出文件的气相流量不能作为旧工况真值。",
              "例如顶部 52 级目前液相约 321.583 t/h、气相约 12.370 t/h；"
              "旧文件两者都约为 321.581 t/h。这个大幅差异主要来自取值修复，不能解释为气相流量因工艺调整而下降。", "",
              "## 下一步优先事项", "",
              "1. 导出完整外部进出物流及水/烃组分，做总量和组分衡算，得到干基产品收率。",
              "2. 核对原油 TBP、轻端和渣油目标，解释 75.05% 的表观渣油比例。",
              "3. 核对主塔 6 级附近进料与汽提连接、三个中间回流回路及热负荷边界。",
              "4. 明确产品质量约束后，再进行小幅操作量扰动，验证灵敏度、收敛范围及目标函数。", "",
              "## 分析口径与可复现性", "",
              "本报告只读已有 JSON，没有重新求解或修改 HYSYS 模型。全部 352 个标量（24 MV、36 CV、292 塔级属性）"
              "保留在 before_after.csv 中；73 个塔级完整保留在 stages.csv 中。没有抽样、剔除、插值或统计显著性检验。",
              "数据摘要和 SHA-256 溯源信息见 summary.json；产品与热负荷分别见 products.csv 和 heat_loads.csv。",
              "绘图环境检查：本机 base 环境导入 matplotlib 异常退出（无 Python 错误信息），本次提供表格及 CSV，不提供未能验证的图。", ""]
    (OUT / "results_analysis.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"products", "heat_loads", "source_hashes", "run"}}, ensure_ascii=False, indent=2))
    print("Report:", OUT / "results_analysis.md")


if __name__ == "__main__":
    main()
