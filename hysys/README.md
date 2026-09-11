# HYSYS 常压蒸馏模型接口

Python 通过 Windows COM 连接 Aspen HYSYS，打开 `mjh_ATM.hsc`，从 `Table` 工作表读取运行点、写入操作量，并检查 `C-1102` 塔的收敛状态。

## 运行

已在本机 Anaconda base（Python 3.11、pywin32）和 HYSYS V12 验证。应在当前 Windows 桌面用户会话运行，以便连接 HYSYS COM 服务。

```powershell
conda activate base
python main.py
```

默认流程：连接模型 → 保存写入前快照 → 写入 `data_write` 的 `mv` → 等待收敛 → 将最新运行点写入 `data_read` → 保存报告。

```powershell
# 只读取，不修改操作量和求解器状态
python main.py --read-only

# 指定输入、输出和求解轮询超时（秒）
python main.py --input data_write --output artifacts/result.json --timeout 180

# 离线回归测试，不连接 HYSYS
python -m unittest discover -s tests -v
```

相对文件路径均以脚本所在目录为基准，因此从其他工作目录执行也可使用。程序操作内存中的模型，不自动保存或关闭 `.hsc` 文件，也不退出 HYSYS。退出码 0 表示本次操作成功，1 表示连接、读写或收敛失败；`--read-only` 成功仅表示导出成功，收敛情况见报告。

## 数据和接口约定

- `data_write` / `data_read` 为 UTF-8 JSON，保留原有 `mv`、`cv`、`stage` 结构。
- `mv`：24 个操作量，对应 Table 第 2–25 行；支持仅提供部分操作量。
- `cv`：36 个输出量，对应第 28–63 行。
- Table A/B/C/D 列依次为设备名、属性名、内部数值、输出单位。
- 写入以 `(设备名, 属性名)` 匹配行，JSON 顺序可自由调整；`cv` 和 `stage` 不用于写入。
- Table 内部 `kg/s`、`kJ/s` 数值根据 D 列 `kg/h`、`KJ/h` 标签双向换算；其他支持的单位见 `unit_factor()`。本模型 `%` 字段保留原数值，不额外乘除 100。
- 塔级数据通过显式单位读取：kPa、C、kg/h；气相使用 `MassVapourFlow`，液相使用 `MassLiquidFlow`。

例如只调整原油温度：

```json
{"mv": {"crude_oil": {"temperature_C": 32.6}}}
```

写入前校验名称、数值、单位及重复映射；写入期间暂停求解，每个值立即回读验证。写入失败则回滚，并恢复原求解器状态；若回滚失败则保持暂停，报告具体单元格。校验覆盖接口完整性，不替代工艺参数的可行范围约束。

收敛需要同时满足：允许求解、求解器不忙、模型有效、塔 `CfsConverged=True`，且持续稳定 1 秒。超时不会自动暂停模型；超时参数约束轮询等待，无法中断 HYSYS 内部阻塞的 COM 调用。写入成功后若不收敛，参数保留在内存模型中，可用写入前快照恢复。

## 输出与诊断

- `data_read`：最新运行点。
- `artifacts/operating_point_before.json`：最近一次执行的写入前快照。
- `artifacts/run_report.json`：执行时间、初始/最终状态、写入数量及错误信息。
- `artifacts/original/`：本次完善代码前的脚本和原始导出数据备份。
- `inspect_hysys.py`：只读接口探查脚本，生成 `artifacts/hysys_inspection.json`。

上述默认输出在下次运行时更新；需要保留某次结果时使用不同的 `--output`、`--before` 和 `--report` 路径。

2026-09-09 本机实际运行：24 个 MV 写入及回读匹配，36 个 CV、73 个塔级成功导出，模型有效且塔收敛。此次验证使用现有 `data_write` 工况，不代表任意新工况均能收敛。
