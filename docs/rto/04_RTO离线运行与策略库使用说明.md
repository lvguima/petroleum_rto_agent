# RTO离线运行与策略库使用说明

_更新日期：2026-08-21 · 当前CLI只有一套目标数量无关的离线RTO入口。_

所有输出均为`engineering_simulation_only`和`offline_simulation_only`，没有DCS写入能力，不代表产品放行、安全边界、现场收益或可直接下装的控制策略。

## 输入与运行模式

一次运行使用两个相互独立的严格JSON：

- `intent-file`表达目标、方向、决策变量、偏好和返回形式，不带运行事实或算法名；
- `context-file`表达受信模型/案例、进料、组成、当前设定值、初态、时刻和数据质量。

当前fixture：

- [单目标意图](../../configs/rto/intents/minimize_specific_furnace_energy.json)
- [多目标意图](../../configs/rto/intents/quality_yield_energy.json)
- [CDU受信上下文](../../configs/rto/contexts/case_20260604.json)

一个目标和多个目标使用同一命令。系统从目标数选择唯一版本化执行路线：单目标执行确定性粗网格加局部细化，多目标执行确定性全网格Pareto搜索。

覆盖策略只有两种：

| 策略 | 含义 |
| --- | --- |
| `point` | 只验证当前上下文点 |
| `sampled-anchors` | 额外验证政策中列出的离散进料锚点，不声称连续区间 |

## 无求解检查

以下命令都不会创建运行目录或调用仿真。

查看内部安全能力投影：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime capabilities --repo-root .
```

只解析意图：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime validate-intent \
  --repo-root . \
  --intent-file configs/rto/intents/quality_yield_energy.json
```

绑定上下文并构造不可变问题：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime validate-problem \
  --repo-root . \
  --intent-file configs/rto/intents/quality_yield_energy.json \
  --context-file configs/rto/contexts/case_20260604.json
```

输出包含问题引用、已绑定执行路线引用、目标、决策、系统硬门禁、独立发布门禁和结果形式，并明确`solver_called=false`。未知字段、重复JSON键、`NaN/Infinity`、布尔值冒充数值、未知能力、方向冲突、未解决歧义和当前无法绑定的业务约束都会在这里或更早被拒绝。

## 运行与恢复

单目标点运行：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime run \
  --repo-root . \
  --intent-file configs/rto/intents/minimize_specific_furnace_energy.json \
  --context-file configs/rto/contexts/case_20260604.json \
  --coverage-policy point \
  --run-root runs/rto \
  --library-root runs/rto/strategy-library \
  --actor offline-rto-operator
```

多目标运行只替换`--intent-file`；离散锚点验证只替换`--coverage-policy sampled-anchors`。

未提供`--run-root`时默认使用`<repo-or-cwd>/runs/rto`，未提供`--library-root`时默认使用`<repo-or-cwd>/runs/rto/strategy-library`。

相同语义输入产生相同workflow ID。再次执行时，编排器先严格读取已经提交的阶段，从最后一个完整阶段恢复；完整workflow再次执行时，本次新增的M2/M4物理执行数应均为零。事件存在但artifact缺失、阶段跳跃、文件被替换或manifest不闭合都会停止恢复，不会覆盖可疑目录。

## Artifact结构

当前workflow目录包含：

```text
request.json
intent.json
context.json
capability_bundle.json
problem.json
solver_route.json
static_solve.json
static_selection.json
dynamic_evaluations.json
finalization.json
anchor_validation.json       # sampled-anchors时可选
strategy_draft.json          # 可发布且覆盖通过时可选
result.json
events.jsonl
manifest.json                # 最后提交
simulator/                   # CDU物理运行证据
```

阶段文件原子提交，事件随后追加到连续hash链，manifest最后写入。workflow内仿真引用使用安全相对路径，因此整个目录可以作为一个单元移动；不能把其中单个物理证据拆走后仍期待严格读取通过。

## 严格检查

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime inspect \
  --run-dir runs/rto/<workflow-id> \
  --library-root runs/rto/strategy-library
```

`inspect`只接受当前workflow manifest合同。它核对精确文件集、大小、哈希、事件链、合同引用和相对路径，并从持久化证据重算问题、路线、求解结果、M2/M4评价、动态回退、发布判定、锚点和策略引用。检查期间不执行新仿真。

结构或版本不匹配的artifact会被明确拒绝；系统不按目录名、目标数或字段形状猜测旧合同。

## 策略审核与发布

workflow只在最终候选通过完整M2/M4门禁、发布改善门槛和所选覆盖策略时创建不可变草稿。运行本身不会批准或发布。

先审核：

```bash
rto-offline approve \
  --library-root runs/rto/strategy-library \
  --strategy-id <strategy-id> \
  --revision 1 \
  --actor offline-reviewer \
  --reason "offline evidence reviewed"
```

再发布：

```bash
rto-offline publish \
  --library-root runs/rto/strategy-library \
  --strategy-id <strategy-id> \
  --revision 1 \
  --actor offline-release-owner \
  --reason "offline library release"
```

策略库直接在给定根目录下使用`entries/`和`releases/`，不再有按历史实现划分的子目录。payload和release不可变，生命周期变化使用追加事件。离线`approved`或`published`只表示仓库治理状态，不等于MOC、SIS、工艺、生产或质量审批。

当前查询只提供Python `StrategyRepository.query(StrategyQuery(...))`。它只返回`published`修订，并要求请求落入一个明确采样锚点的测量容差；不会把`min/max`解释为锚点之间连续可用。

## 结果字段

| 字段 | 解释 |
| --- | --- |
| `objective_count` | 当前问题的目标数，不是合同版本 |
| `selected_solver_id` | 问题已绑定路线对应的求解实现 |
| `static_evaluation_count` | 保存的M2候选评价数 |
| `dynamic_shortlist_count` | 完成M4动态复核的短名单数 |
| `selected_setpoints` | 最终选中设定值、数值和规范单位 |
| `selected_objectives` | 选中候选相对同上下文基准的目标结果 |
| `optimization_status` | 动态复核后的最终优化状态 |
| `strategy_state` | workflow是否形成草稿；不表示当前仓储生命周期状态 |
| `physical_*_executions_this_call` | 本次调用新发生的物理执行数 |
| `manifest_fingerprint` | 当前workflow artifact集合的完整性指纹 |

备选引用可能包含M4不通过的静态候选，不能直接当作可执行动作。`invalid_request`、`evaluation_error`和`process_infeasible`含义不同；系统或证据错误不能解释为“装置无可行解”。

## 本地自然语言解释

运行`rto-chat`后，可输入：

```text
/result <run-dir或result.json>
```

组合CLI会先用严格reader重载已有workflow，在本地显示确定性设定值，再只把白名单结果摘要交给模型解释。该命令不会启动RTO、仿真、审批或发布；模型失败也不会改变本地结果。

普通自然语言询问当前仿真工况时，CLI把固定受信配置的白名单数据交给模型理解，并只显示模型整理后的中文回答，不会先输出原始JSON或固定边界提示。完整行为见[源码区DMX对话工具](../domain_model/01_聚合式垂域模型综合说明.md)。

## Python入口

`petroleum_rto.rto.runtime`公开：

- `capabilities`
- `validate_intent_file`
- `validate_problem_files`
- `run_offline`
- `inspect_offline`
- `approve_strategy`
- `publish_strategy`
- `query_strategies`
- `run_summary`

顶层`petroleum_rto.rto`公开中立合同、`ProblemBuilder`、`SolverRouter`、`CandidatePlanCompiler`、M2/M4评价、`FinalSelector`、`OfflineRtoOrchestrator`和`StrategyRepository`。没有按目标数量或历史版本命名的公共入口。

## 相关资料

- [RTO系统综合说明](01_RTO系统综合说明.md)
- [垂域模型与RTO通信协议](02_垂域模型与RTO通信协议.md)
- [项目实施状态](../STATUS.md)
