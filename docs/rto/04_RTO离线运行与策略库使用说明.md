# RTO离线运行与策略库使用说明

_更新日期：2026-09-11 · 当前CLI只有一套目标数量无关的离线RTO入口。_

所有输出均为合成工程仿真。本文集中说明这项边界，普通问答和每次结果回复不重复输出冗长声明。

## 输入与算法

一次文件式运行使用两个独立严格JSON：

- `intent-file`表达目标、方向、决策变量、偏好和结果形式，不带运行事实或算法；
- `context-file`表达模型/案例、进料、组成、当前设定值、初态、时刻和数据质量。

当前fixture：

- [单目标意图](../../configs/rto/intents/minimize_specific_furnace_energy.json)
- [多目标意图](../../configs/rto/intents/quality_yield_energy.json)
- [CDU受信Context](../../configs/rto/contexts/case_20260604.json)

一个目标和多个目标使用同一命令。当前算法均为确定性网格搜索：单目标使用粗网格加局部细化，多目标使用全网格Pareto搜索。

覆盖策略：

| 策略 | 含义 |
| --- | --- |
| `point` | 只评价当前Context点 |
| `sampled-anchors` | 额外评价政策列出的离散进料锚点，不声明连续区间 |

## 无求解检查

以下命令不创建运行目录，也不调用仿真。

查看公开能力：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime capabilities \
  --repo-root .
```

只解析意图：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime validate-intent \
  --repo-root . \
  --intent-file configs/rto/intents/quality_yield_energy.json
```

绑定Context并构造一次Problem：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime validate-problem \
  --repo-root . \
  --intent-file configs/rto/intents/quality_yield_energy.json \
  --context-file configs/rto/contexts/case_20260604.json
```

这些开发者命令仍返回结构化合同信息，但不承担用户确认交互。Agent的确认摘要不展示或要求复制内部引用。

## 运行

单目标point运行：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime run \
  --repo-root . \
  --intent-file configs/rto/intents/minimize_specific_furnace_energy.json \
  --context-file configs/rto/contexts/case_20260604.json \
  --coverage-policy point \
  --run-root runs/rto
```

多目标只替换`--intent-file`；离散锚点评价只替换`--coverage-policy sampled-anchors`。未提供`--run-root`时使用`<repo-or-cwd>/runs/rto`。

运行入口加载能力、Intent和Context，各自严格校验后构造一个不可变Problem。该Problem同时交给路由、求解和编排，不为确认或结果生成重复构造。

文件式`rto-offline run`可恢复同一内部workflow；内部恢复从最后一个完整阶段继续，并拒绝阶段跳跃、文件缺失或不闭合目录。当前offline workflow合同版本为`4.0.0`，manifest版本为`offline-rto-manifest-4.0.0`。当前Agent恢复、复用已有阶段及显式结果查询使用严格阶段/证据重载，同一进程也不能仅信任会话回执。新鲜运行完成后直接返回内存记录，不再为了首次打印或聊天回复执行一次完整的事后读取。

## 结果摘要与运行回执

CLI和Agent使用同一内存结果投影：

| 顶层字段 | 解释 |
| --- | --- |
| `status` | 最终优化状态 |
| `targets` | 目标、方向、优先级和单位 |
| `operating_context` | 运行模式、进料摘要、时刻和数据质量 |
| `baseline_values` | 各目标同上下文基准值 |
| `recommended_adjustments` | 当前值、推荐值、调整量和单位 |
| `predicted_effects` | 预测目标值及方向性、相对改善 |
| `alternative_candidates` | 未选中候选的排序、调整、预测效果和实际M2/M4验证状态 |

编排器从内存中的最终选择及候选M2/M4评价生成这七部分，并写入格式化`result.json`。`alternative_candidates`始终存在，可为空；它不重复顶层推荐，并按最终全局`rank`升序排列。每项固定包含`rank`、`adjustments`、`predicted_effects`、`verification_stage`和`verification_status`。M2表示只完成稳态评价，M4表示已有动态复核，结果不会把M2候选写成完成M4。正常完成路径不重新读取该文件，不重复校验哈希，不重放物理证据，也不比较策略副本。

Intent中的`max_candidates`是推荐与其他候选合计的上限，不是备选数量。例如`max_candidates=5`最多返回1个推荐和4个其他候选。没有选中推荐时，`alternative_candidates`为空；其他候选也只是本次计算的设定点组合，不是正式策略。

独立`run_confirmed_optimization`公共API返回受信`OptimizationRunReceipt`，继续保护现有受信调用方合同；当前Agent使用staged接口，不能把两者混为一条入口。该回执包含：

| 回执字段 | 解释 |
| --- | --- |
| `workflow_id` | 形如`offline-rto-<16位十六进制指纹>`的确定性工作流标识 |
| `result_source` | 严格等于`<workflow_id>/result.json`的受控相对位置 |
| `result_summary` | 上述七字段摘要 |

回执是RTO与Agent之间的内存边界，不是新的磁盘结果格式。`result.json`仍直接保存七个顶层业务字段，不包含`workflow_id`、`result_source`或外层`result_summary`。同一问题命中已完成workflow时，编排器轻量读取该七字段文件后组成相同回执。

没有最终选中候选时，目标和工况仍可保留，而基准、调整和预测数组按可用事实为空。系统错误、工艺不可行和无已验证候选仍保持不同状态。

## 内部workflow与显式inspect

运行目录中有两类内容：

```text
result.json                  # 面向用户的简洁结果
request.json                 # 以下为内部恢复与开发诊断文件
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
workflow.json
events.jsonl
manifest.json
simulator/
```

`result.json`由内存结果单独生成，不放入内部manifest。其他阶段文件用于恢复和开发诊断，不直接交给普通问答模型。

需要检查历史内部workflow时，开发者显式运行：

```bash
PYTHONPATH=src .venv/bin/python -m petroleum_rto.rto.runtime inspect \
  --run-dir runs/rto/<workflow-id>
```

`inspect`只接受当前v4内部合同，检查文件集、哈希、事件、引用和可重建结果，并读取已有物理证据；它不应启动新仿真。由于七字段结果改变了严格合同，当前reader不兼容旧v3六字段结果。这个命令不在普通Agent工具集合中，也不参与确认后的回复。

## 本地自然语言Agent

2026-09-09起，`rto-chat`与`python -m petroleum_rto.domain_model`使用统一原生工具循环。当前可查询装置身份、工况和已有结果，可通过`/model`切换模型、`/thinking`设置思考。工况和工具结果进入共同会话，模型可以继续查询或回答。

Agent先读取工况并准备方案；确认绑定已展示的具体版本及快照。整条`/confirm`、“确认”或“确认执行”由真实输入转换为同一公共审批动作，随后固定节点完成静态搜索和动态复核，模型没有确认或阶段执行工具。`/cancel`取消后续执行并保留结果。修改尝试即撤销旧资格，失败后也须重新准备、展示并在后续用户轮确认。独立离线计算仍可使用本文前面的`rto-offline`公共入口。

`rto.runtime.staged`提供快照绑定的准备与阶段入口：M2在`static-selection-ready`停止，M4严格恢复同一问题并复核完整入围列表。RTO的v4目录、事件与恢复语义保持，静态回执不包含最终结果。Agent另以官方SQLite检查点保存当前会话、完整固定方案、审批及阶段回执；重启从保存的能力与Context重建同一问题，只读取并展示恢复摘要，不请求模型或计算。待审批方案仍须确认；已批准未完成方案必须在展示后输入`/resume`续接，普通追问不续算。已有完整阶段严格验真后复用；半个阶段可能重算，不保证每个候选恰好执行一次。

`/result [workflow_id]`只接受受控结果编号，省略时读取当前会话结果。已有结果及取消后保留的最近结果须通过严格证据读取，缺失或损坏明确拒绝。成功后保存结果引用，后续自然语言可以追问；不根据全局修改时间猜测“最近结果”，不把路径作为模型工具参数。`/clear`清除全部可恢复会话、快照、摘要、分页、方案和授权，保留模型选择，磁盘已有结果不受影响。完整启动及协议限制见[当前Agent说明](../domain_model/01_聚合式垂域模型综合说明.md)。

## 策略库

普通`run`和Agent确认运行都不自动创建策略。`alternative_candidates`中的其他候选不是策略，运行目录和策略库都不会因一次优化自动出现策略条目。

需要策略时，独立治理代码必须显式使用`StrategyBuilder`从已完成且满足相应条件的评价结果构造`StrategyEntry`，再调用`StrategyRepository.create_draft()`保存。当前v3策略恰有八个顶层字段：

```text
schema_version
strategy_id
revision
supersedes
adjustments
applicability
evidence
fingerprint
```

其中`adjustments`给出当前值与推荐值；`applicability`给出case、运行模式和已评价锚点；`evidence`只保存最小来源引用。生命周期状态由追加事件保存，不重复塞进payload。

只有已经显式创建的草案才能审核：

```bash
rto-offline approve \
  --library-root runs/rto/strategy-library \
  --strategy-id <strategy-id> \
  --revision 1 \
  --actor offline-reviewer \
  --reason "offline evidence reviewed"
```

批准后才可发布：

```bash
rto-offline publish \
  --library-root runs/rto/strategy-library \
  --strategy-id <strategy-id> \
  --revision 1 \
  --actor offline-release-owner \
  --reason "offline library release"
```

当前查询使用Python `StrategyRepository.query(StrategyQuery(...))`，只返回`published`修订，并要求请求命中一个明确锚点的测量容差。策略审批和发布是离线仓储状态，不等于现场批准、MOC、SIS或控制下装。

## Python入口

`petroleum_rto.rto.runtime`公开：

- `capabilities`
- `validate_intent_file`
- `validate_problem_files`
- `run_confirmed_optimization`
- `run_offline`
- `inspect_offline`
- `approve_strategy`
- `publish_strategy`
- `query_strategies`
- `run_summary`

`run_confirmed_optimization`返回上述`OptimizationRunReceipt`的映射；`run_offline`仍返回完整内部运行记录，两者职责不同。没有面向用户的问题确认令牌API，也没有按目标数量或历史版本命名的公共入口。

## 相关资料

- [RTO系统综合说明](01_RTO系统综合说明.md)
- [垂域模型与RTO通信协议](02_垂域模型与RTO通信协议.md)
- [源码区领域Agent](../domain_model/01_聚合式垂域模型综合说明.md)
- [项目实施状态](../STATUS.md)
