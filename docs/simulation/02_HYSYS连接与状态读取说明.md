# HYSYS连接与状态读取说明

当前仿真模块提供已打开案例的只读连接、真实变量绑定与单位读取、不可变快照、基准捕获/恢复、MV正式单点及严格结果重载，并提供独立的物料/组分/能量边界读取。模块独立于RTO、Agent和LangChain，当前Agent已接入24项MV的单项或组合稳态比较路径，尚未提供优化搜索。实际进度与下一步以[STATUS](../STATUS_REACT_REBUILD.md)为准，证据见[变量与正式读取](../../reports/simulation/hysys_binding_qualification_20260911.json)、[基准恢复](../../reports/simulation/hysys_baseline_recovery_20260911.json)、[正式单点验收](../../reports/simulation/hysys_t39_point_20260911.json)及[顺序与边界验收](../../reports/simulation/hysys_order_boundary_20260911.json)。

## 使用方式

本机使用项目Python 3.12 x64和pywin32，依赖以[pyproject](../../pyproject.toml)及[uv.lock](../../uv.lock)为准。先在HYSYS中打开`D:\pyproject\petroleum_rto_agent\hysys\mjh_ATM.hsc`并处理软件自身提示，保持案例打开；从项目根目录执行：

```powershell
.\.venv\python.exe -B -m petroleum_rto.simulation --case hysys/mjh_ATM.hsc
```

命令默认消费[变量目录](../../configs/simulation/mjh_atm_variables.json)，可用`--catalog`指定符合当前合同的目录。只选择完整路径匹配的唯一已打开案例，不会自动打开、激活、保存或关闭模型。当前目录对应本机中文属性名和指定模型，切换模型或语言后绑定不符会明确失败。

代码调用入口如下；`read_snapshot`仅读取文件，不连接HYSYS：

```python
from pathlib import Path
from petroleum_rto.simulation.hysys import read_current_snapshot
from petroleum_rto.simulation.models import read_snapshot, write_snapshot

snapshot = read_current_snapshot(Path("hysys/mjh_ATM.hsc"))
write_snapshot(Path("snapshot.json"), snapshot)  # 文件已存在时拒绝覆盖
restored = read_snapshot(Path("snapshot.json"))
assert restored == snapshot
```

## 当前读取合同

| 内容 | 当前行为 |
| --- | --- |
| 案例来源 | 记录请求的完整路径、磁盘源文件哈希、HYSYS版本、观测时间、内存IsDirty；读取后复核路径和磁盘哈希 |
| 变量 | 读取24条MV和36条CV的实际ImportedVariable，校验AttachedObjectName、VariableName和UnitConversionType；逐项GetValue请求约定单位 |
| 塔级 | 读取C-1102的73级名称、压力、温度、液相/气相质量流量；分别请求kPa、C、kg/h，气相使用MassVapourFlow |
| 塔规格 | 记录规格类型、活动/估计标志、目标及当前值；未知值为null，校验两者物理量类型一致；活动集合与ActiveSpecifications一致 |
| 规格绑定 | 变量目录的10项塔规格须处于活动状态、物理量匹配且目标与表中真实变量读数一致 |
| 求解状态 | 每份采样前后读取CanSolve、IsSolving、IsValid、CfsConverged；直接读取DegreesOfFreedom |
| 观测一致性 | 连续两份完整采样精确一致、前后状态一致、没有正在求解、有效、塔收敛且自由度为0，才标记observed_stable |

`CanModify`和变量状态保留为观测事实；MV角色不等于已批准优化自由度。配置保存绑定与单位，不保存当前值、虚构范围或未实现的求解选项。纯Python的[快照合同](../../src/petroleum_rto/simulation/models.py)不导入COM；[读取器](../../src/petroleum_rto/simulation/hysys.py)仅在实际连接时加载Windows依赖。

## 已核实的语义

真实属性与本机类型库确认加热器的D_temperature字段是温差，泵的D_pressure是压差；Flash的44.5%对应液相体积百分比。煤油侧线规格Kerosene SS BoilUp Ratio为无量纲0.5，不能按原表D22的百分号解释为0.5%。配置因此使用`flash_column.liquid_volume_percent`及`C-1102.kerosene_ss_boilup_ratio`；来源文件保留原样。

三组PA分别绑定循环流量与返回温度，另有再沸比和三条塔温规格。当前读取到20项规格、10项活动规格及0自由度；这些是本次模型观测，不是根据规格数量推算的自由度。活动与用作估计是不同属性，均单独保留。

规范单位与物理量以运行时合同及目录为准。相同的C标签不能消除温度和温差的区别；质量/热流量使用GetValue转换，不再依赖旧脚本统一乘3600或表单显示标签。原始内部值另存为internal_value。

## 读取输出与失败

每次命令创建独立的`runs/simulation/hysys-snapshot-*`目录，包含`snapshot.json`和`report.json`。报告保存快照SHA-256和读取结果；命令在返回成功前执行严格重载并比较对象。退出码0表示一致观测，2表示观测不一致，1表示读取失败。

快照拒绝重复键、未知/缺失字段、非有限数值、单位不符、重复标识及不完整变量/塔级集合；写入使用排他创建，已存在文件不覆盖。快照文件可严格重载，但哈希并非身份认证，报告及快照也不是防恶意篡改证明。

绑定变化、规格变化、案例变化、源文件变化和COM失败保留结构化错误码，不按异常文本分类为工艺不可行。COM对象及错误栈在同一线程释放后再CoUninitialize；当前同步COM调用仍可能阻塞，不宣称Python超时能终止它。

两次一致观测不证明原子快照或完整物理有效性，磁盘哈希也不能绑定未保存的内存状态。快照合同始终记录`source_kind=existing_case_memory`、`memory_state_bound_to_disk_hash=false`、`atomic_snapshot_proven=false`、`eligible_for_optimization=false`。以下基准与恢复能力不会提升优化资格。

## 基准捕获与严格重载

[baseline.py](../../src/petroleum_rto/simulation/baseline.py)提供三个参数的`capture_baseline(case_path, output_dir, catalog_path=DEFAULT_CATALOG)`，返回新目录中的`manifest.json`路径。它通过GetActiveObject定位完整路径唯一匹配的已打开源案例，在稳定观测下调用`SaveCopyAs(新路径, False)`，保留源文件和用户尚未保存的源内存状态；不调用SaveAs、打开副本或关闭源案例。保存前后比较本合同全部可观测字段（仅排除观测时间）、源文件哈希、打开案例数量及源COM对象身份，拒绝同路径同读数的对象替换。

输出目录必须尚不存在，父目录必须已存在；拒绝覆盖。成功目录包含固定四个文件：

| 文件 | 内容 |
| --- | --- |
| `baseline.hsc` | HYSYS从当前源内存保存的案例副本；可能与原始磁盘文件哈希不同 |
| `snapshot.json` | 保存前源内存的稳定观测，仍保留源路径、原件磁盘哈希及IsDirty |
| `variables.json` | 本次实际消费的变量目录原始字节副本 |
| `manifest.json` | 固定版本声明及上述三个文件的SHA-256；成功完成后才写入 |

`read_baseline(manifest_path)`只读本地文件，返回不可变`Baseline`，包含目录、冻结模型哈希、严格快照和冻结变量目录路径；不连接HYSYS，不证明案例可重开。它拒绝重复JSON键、未知/缺失字段、错误声明、非有限值、无效哈希、哈希不符、空模型，以及文件链接、硬链接或目录重解析点；同时校验快照稳定性和目录与快照的案例/变量绑定一致性。

从项目根目录调用示例，源案例须保持打开，两个输出目录均须使用尚未存在的名称：

```python
import json
from pathlib import Path
from petroleum_rto.simulation.baseline import capture_baseline, read_baseline
from petroleum_rto.simulation.recovery import verify_baseline

manifest_path = capture_baseline(
    Path("hysys/mjh_ATM.hsc"), Path("runs/simulation/my-baseline")
)
baseline = read_baseline(manifest_path)  # 仅读磁盘，不打开HYSYS案例
report_path = verify_baseline(manifest_path, Path("runs/simulation/my-recovery"))
report = json.loads(report_path.read_text(encoding="utf-8"))
assert report["status"] == "verified"
assert report["eligible_for_optimization"] is False
```

捕获manifest固定记录`source_unchanged=true`、`reopen_verified=false`和`eligible_for_optimization=false`。后续重开验证写独立报告，不回写或升级这份捕获记录。捕获失败会抛出异常，保留已创建目录及部分产物；它没有保证生成失败报告，不能把残留`baseline.hsc`当作成功基准。

## 工作副本重开与恢复验证

[recovery.py](../../src/petroleum_rto/simulation/recovery.py)的`verify_baseline(manifest_path, output_dir)`先严格读取基准，再将冻结`baseline.hsc`字节复制为新目录的`work.hsc`并复核哈希。它连接已有HYSYS应用，按捕获快照中的源完整路径定位源案例，读取本次源观测，记录全部既有案例路径与对象身份，然后在同一应用中打开工作文件。

Open必须返回正确完整路径的新案例对象，既有案例集合与对象必须保留，且只能新增本任务工作案例。读取工作快照后写入`observed.json`、严格重载，并用[compare_operating_points](../../src/petroleum_rto/simulation/comparison.py)与捕获快照进行零数值容差比较：包括MV、CV、塔级、规格及求解状态。比较有意排除观测时间、文件路径、磁盘哈希和IsDirty；文件完整性与源保护另行验证，可见值一致不证明隐藏模型状态完全相同。

结束时仅对确认归属本任务的工作案例调用`Close(False)`，复核工作案例关闭、原案例集合及对象恢复、源本次前后观测一致（仅排除时间），并复核原件、基准四文件及工作文件哈希。源保护比较的是本次验证前后，不要求用户源内存始终等于早先捕获工况。即使Open抛错后留下文档，也仅在唯一完整工作路径及新对象身份确认后关闭；不使用CloseAll或Quit，不关闭其他案例。

成功的恢复目录包含`work.hsc`、`observed.json`和`report.json`。报告记录基准manifest哈希、工作快照路径及哈希、逐项差异、工作案例归属/关闭结果、源保护、文件复核、COM生命周期及结构化错误。只有比较等价、源与文件保护通过、工作案例已关闭且无错误时才为`status=verified`；始终`eligible_for_optimization=false`。

无效基准或已存在输出目录会在连接COM前抛错，不生成恢复报告。新目录创建后的错误会尽量完成归属清理和保护检查，保留`status=failed`报告及已生成文件；差异不会写回基准或被归类为工艺不可行。COM引用和异常栈在CoUninitialize前释放；同步外部调用及磁盘故障仍可能阻碍结束或报告落盘，没有硬超时保证。

2026-09-11正式`capture_baseline/read_baseline`与`verify_baseline`均已实测通过：捕获目录为`runs/simulation/hysys-baseline-20260911-formal`，恢复目录为`runs/simulation/hysys-recovery-20260911-formal`；重开比较零差，源观测、案例集合和磁盘保护通过。此路径提供案例文件与案例对象隔离，**共享同一HYSYS进程**，采用串行调用；不宣称进程隔离，共享进程故障仍可能影响用户源会话。

## 固定单变量诊断

[probe_hysys_single_change.py](../../scripts/simulation/probe_hysys_single_change.py)只诊断独立工作案例的T-39目标增加`0.1 °C`，B要求实际Current与目标的绝对残差在`0.01 °C`内，随后关闭工作案例、从冻结基准重新打开A并执行零容差恢复比较。该幅度仅为本次诊断，不是已确认的安全范围、优化步长或通用MV权限。

前两次真实运行均未通过B跟随判据：第一次经`ImportedVariable.SetValue`写入，第二次改用规格拥有者`GoalValue` setter；两次Goal均正确读回新值，但Current基本停留在原工况。IsValid、CfsConverged和双样本一致不能替代跟随判据；GoalValue本身未解决问题。两次随后A恢复均为零差，源观测及原件保持不变，原失败报告保留。

第三版仅在拥有的工作塔内增加`Reset()`后`Run()`，保持相同扰动、跟随及恢复判据，**真实验证通过**：目标156.9℃，Current为156.90049807320577℃，残差约0.000498℃；其他输入与规格无意外变化，双样本稳定，A恢复零差，源案例身份/观测和文件保护均通过。证据位于`runs/simulation/hysys-single-change-20260911-reset`。

Reset会清除该工作塔当前解与估计；诊断没有修改HYSYS原生容差。官方本机帮助建议小改先Run、必要时Reset，本次结果支持此工作副本的重算路径；不证明此前写入失败或所有变量都必须Reset。原生规格及整体求解容差与本诊断的0.01℃跟随标准不同，单看收敛标志不足以证明设定已满足。固定两目标顺序实测见下文；T-39是早期代表变量的实测证据；当前通用MV接口见下文，优化搜索未开放，产物不具备优化资格。

## 历史T-39单点接口与证据

固定诊断的工作案例生命周期已移入[simulation.point](../../src/petroleum_rto/simulation/point.py)，提供`run_t39_point(baseline_manifest, target_c, output_dir)`，结果由[read_point_result](../../src/petroleum_rto/simulation/point_evidence.py)严格重载。只支持已核定T-39温度规格，目标单位固定为℃；调用方必须提供有限数值，布尔值、非数值和无穷值会拒绝。这里没有发布目标的工艺有效范围，当前Agent使用下文通用MV接口；该旧接口保留给已有诊断与历史结果消费者。

调用前，已有HYSYS应用中必须只打开捕获基准所指的源案例。输入基准及T-39资格在创建输出目录和连接COM前验证；输出目录必须是尚未存在的新名称，父目录须已存在。例如，使用前文捕获的基准：

```python
from pathlib import Path
from petroleum_rto.simulation.point import run_t39_point
from petroleum_rto.simulation.point_evidence import read_point_result

report_path = run_t39_point(
    Path("runs/simulation/my-baseline/manifest.json"),
    target_c=156.9,
    output_dir=Path("runs/simulation/my-t39-point"),
)
result = read_point_result(report_path)  # 仅读已有文件，不连接或重算HYSYS
print(result.status, result.target_c)
```

同目标请求也会写回规格并执行工作塔Reset/Run，用于以相同协议重新计算基准。变更点按绝对目标残差检查，不要求温度只能上升；其他输入、活动规格、有效/收敛/自由度、基准重开和源保护仍必须通过。重新计算出的基准输出与保存时的旧解可以有数值差异，恢复则继续对保存的A执行零容差比较。

成功目录包含固定`report.json`、`baseline_snapshot.json`、`variables.json`、`candidate.hsc`、`restored.hsc`及五份快照：`source_before.json`、`A_before.json`、`B_changed.json`、`A_restored.json`、`source_after.json`。两个工作模型文件均保留冻结基准字节，不保存修改后的模型；B的计算观测由快照承载。`events.jsonl`仅为诊断进度，不参与结果资格判定。

报告采用`hysys-t39-point/1.0.0`合同，保存显式目标、冻结模型哈希、证据文件哈希、分阶段错误和运行观察。读取器拒绝未知字段/文件引用、重复JSON键、非有限值、单位或资格不符、文件链接和哈希不符；重新计算初始A、目标残差、其他输入/状态、恢复及源前后观测，核对规格诊断数值与快照一致。缺失B、未恢复、文件漂移或错误不能被报告的成功标签覆盖。

`PointResult`是不可变对象，提供`status`（passed/failed）、`target_c`、`baseline_sha256`、基准及可选的开始/计算后/恢复快照、分阶段`errors`，并保持`eligible_for_optimization=false`。前置输入错误直接抛出，不创建成功结果；执行失败保留已有快照和错误，但磁盘损坏或不可写仍可能使报告无法落盘。当时的COM对象身份和文档关闭结果属于运行记录，文件哈希不能重新证明这些历史动作。

当前严格读取按产生时的工作路径核对快照；结果目录重定位尚未实现。各批自动验证见STATUS。正式接口真机验证中，156.8℃原目标重算得到156.80060827962535℃，156.9℃变更点得到156.90049807320577℃；两点均通过目标残差、A零差恢复、源保护和离线严格重载。此结果仅覆盖本案例的这两个目标。

## 固定两目标顺序验收

[verify_t39_order.py](../../scripts/simulation/verify_t39_order.py)调用同一个正式单点接口，固定执行A→B→B→A：A取冻结基准的T-39目标，B为A加0.1℃。每点使用新目录和冻结基准的独立工作案例，执行相同Reset/Run、恢复和源保护协议；不复用上个候选的解。

```powershell
.\.venv\python.exe -B scripts/simulation/verify_t39_order.py --baseline runs/simulation/my-baseline/manifest.json --output runs/simulation/my-order
```

每点严格重载成功后才继续，并在后续点和结束时复核冻结输入及前序产物的哈希。单点失败、文件漂移或中断会停止，保留失败记录而不重试。四点通过后，对两份A计算后快照、两份B计算后快照分别进行全部输入、输出和状态的零容差比较；任何差异使总体失败。总`report.json`保存固定顺序、目标、文件身份、逐点结果与两组完整比较；每点证据的严格读取仍由`read_point_result`负责。该验收只证明本案例这两个目标在本次固定顺序中的可见结果，不证明任意候选、隐藏状态或优化范围。

2026-09-11实测**未通过复现验收**：四个单点各自通过目标跟随、冻结初值、恢复及源保护，但A/A和B/B各有296项输出差异，输入及状态均无差异。相同A的AGO流量相差约71.47kg/h，相同B相差约17.19kg/h。记录位于`runs/simulation/hysys-order-20260911`，未重试或放宽容差；重复重算差异的原因尚未确定，不能仅由该顺序实验断定是执行顺序引起，也不能宣布候选排名可靠。

## 物料、组分与能量边界读取

[boundary.py](../../src/petroleum_rto/simulation/boundary.py)提供独立只读接口，实际消费[边界定义](../../configs/simulation/mjh_atm_boundary.json)。本合同限定mjh_atm的主流程与C-1102塔子流程；核对完整设备库存、18条主物料流和主/子流程能流的名字及连接。外部物料以FeederBlock连接识别，TABLE只属于设备库存。新增设备、物流、能流或连接漂移均须重新核定，不能被旧清单静默漏算。

```python
from pathlib import Path
from petroleum_rto.simulation.boundary import (
    read_current_boundary, write_boundary_snapshot, read_boundary_snapshot,
)

snapshot = read_current_boundary(Path("hysys/mjh_ATM.hsc"))
write_boundary_snapshot(Path("boundary.json"), snapshot)
loaded = read_boundary_snapshot(Path("boundary.json"))
assert loaded == snapshot
print(loaded.consistent_observation)
```

接口只附着唯一已打开的源案例，不写入或求解。快照包含前后完整核心观测及两份边界采样，保存44项组分名称、公式和假组分标志，以及9条外部物料和11条独立物理能流。每条物料记录温压、质量流量、质量焓、焓流、摩尔气相分率和按同一物性包顺序排列的44项质量分率。质量分率的1e-12求和容差只处理数字表示误差，不定义工程守恒或质量门限。

四条外进为Crude_Oil、Water1/2/3，五条外出为Risedue、Naptha、Kerosene、Diesel、AGO。能量账包含主流程的加热和泵功、塔内煤油侧线再沸器供热，以及三路PA冷却。TopStagePA_Q-Cooler_1在主/子流程中是同一桥接负荷，两处读数必须一致，只计子流程一次。读取标量时检查IsKnown、真实物理量及显式单位；不使用SSDuties未知项替代实际能流。

快照内含本次消费的完整不可变边界定义及规范JSON哈希。离线读取不访问当前配置、HYSYS或源模型，校验定义、单位、集合、组分长度/数值、桥接一致性、前后核心观测及与边界重复的九物流流量/原油温压，并根据保存的双样本重新计算`consistent_observation`。哈希是完整性检查，不是身份认证；原COM身份保护只能由运行时检查，离线文件不能重新证明当时的COM对象。文件排他创建，拒绝覆盖、链接、未知字段、重复键及非有限值。

2026-09-11正式只读接口实测通过，双样本一致、落盘严格重载相等，源身份、核心观测和文件保护通过。此次源工况4进/5出共约403000kg/h，44组分最大质量残差约2.64e-11kg/h；8股能量输入减3股输出，再减物流焓升，余额约0.102688478kW。实际余额保留，未定义工程通过阈值；证据在`runs/simulation/hysys-boundary-20260911`及本批验收报告。

连续采样一致仍不证明原子快照，源内存状态也不由源磁盘哈希绑定；优化资格固定为false。边界快照尚未接入单点结果或RTO评价，不把原源工况的边界值混作Reset/Run后候选的评价数据。当前模型的Naptha出口接近全气相且含水，不能将其总流量直接当成液体石脑油成品；扣水也不等于扣除了其他非烃。物理供热、取热和泵功尚未映射燃料、公用工程效率或经济指标，质量和能量验收阈值仍需单独定义。

本机官方帮助确认[组分数组的索引顺序](<C:/ProgramData/AspenTech/Aspen HYSYS V12.0/HtmlHelp/Subsystems/HYSYS_Customization/Content/html/Key_HYSYS_Objects.htm>)与[摩尔气相分率](<C:/ProgramData/AspenTech/Aspen HYSYS V12.0/HtmlHelp/Subsystems/Properties_Env/Content/TechRef/VaporFractionFlashCalcs.htm>)的含义；本机类型库及真实GetValue读取用于核定量纲和单位。这些官方来源解释软件数据语义，不证明当前原油物性或成品质量已得到外部验证。

## 早期诊断与后续

[probe_hysys_read.py](../../scripts/simulation/probe_hysys_read.py)保留为早期连接故障诊断入口，其Table原始值和未认证标签不属于正式快照。它的自动打开副本路径在本机两次失败并伴随HYSYS进程异常退出，原始记录见[连接报告](../../reports/simulation/windows_hysys_read_20260911.json)。用户手动打开正常、附着成功；后续另建HYSYS实例仍遇到启动/Open失败，但在已有应用内自动打开独立工作案例已通过，不能概括为所有自动Open均失败。早期失败根因仍未完全确定，正式状态读取入口继续只附着已打开案例。

下一步先在隔离工作案例中定位重复重算差异，并将每次计算后的边界观测纳入单点证据，再接稳态RTO评价；变量范围及目标/约束仍须有具体依据。当前结果仅为合成工程仿真证据。


## 单点v2与Agent稳态接入

新单点报告使用hysys-t39-point/2.0.0，在工作案例B_changed计算之后、关闭及恢复之前保存B_boundary.json和boundary_definition.json。报告严格核对边界定义、双采样、边界核心观测与B_changed的一致性；缺少或不匹配时即使运行标记成功也不能通过。历史v1报告保持原义，只读返回boundary=None，不能凭空补边界或参与新Agent比较。

Agent的M2/M4节点已替换为单一稳态比较，见[Agent说明](../domain_model/01_聚合式垂域模型综合说明.md)。保存基准副本采用既有capture_baseline；暂停/恢复计算采用与原hysys_control.suspend/resume相同的Solver.CanSolve开关。正式单点按已确认changes写入选定MV，使用SetValue或活动规格GoalValue；所有选定值读回后统一Reset/Run，并校验未选输入不变及同次稳定状态。逐次数据属于各自副本，原源案例边界不会拼接进候选结果。


## 当前MV单点接口

[run_mv_point](../../src/petroleum_rto/simulation/point.py)接收`baseline_manifest, changes, output_dir`。changes为变量目录内一项或多项MV的绝对目标，例如：

```python
changes = [
    {"variable_id": "C-1102.39_temperature_C", "value": 156.9, "unit": "C"},
    {"variable_id": "crude_oil.pressure_kPa", "value": 180.1, "unit": "kPa"},
]
```

示例数值是联调设定，不是工艺上下限。24项MV共用[单位与写入实现](../../src/petroleum_rto/simulation/mv.py)，其中活动塔规格使用规格GoalValue，其余绑定使用ImportedVariable.SetValue；流量对外kg/h、内部kg/s。逐项预检完成后暂停求解，统一写入，再读回全部选定值以发现互相覆盖；读回通过才Reset、恢复CanSolve并Run。部分写入失败不会求解部分方案，而是沿原工作案例生命周期关闭并恢复。源案例不被写入或保存。

结果采用hysys-mv-point/1.0.0，保存changes和各变量target/readback/actual；共用原单点源保护、零差恢复、逐点边界和严格重载。设定读回允许数值表示误差；活动温度规格保留0.01℃实际跟随检查，其他规格记录实际值并检查HYSYS有效/收敛状态，不伪造工程质量或残差限值。所有未选MV及非目标规格保持原检查。当前没有优化资格，实际联调范围见STATUS，不能将接口支持等同于24项逐个真机试验通过。


2026-09-12的[MV接入验收](../../reports/simulation/hysys_mv_controls_20260912.json)覆盖24项真实绑定/单位核对，以及T-39、原油入口压力和Water1流量的组合写入。正式Agent比较、同次边界、恢复和会话重载通过；模型使用本地替身。首轮候选曾出现Risedue温度未定义并正确返回evaluation_error，同组合后续通过，根因仍待定位。未逐项真机写入其余MV，也未扩大为范围扫描或优化搜索。
