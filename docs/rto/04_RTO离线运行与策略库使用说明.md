# RTO离线运行与策略库使用说明

_更新日期：2026-09-03 · 当前CLI只有一套目标数量无关的离线RTO入口。_

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

文件式`rto-offline run`可恢复同一内部workflow；内部恢复从最后一个完整阶段继续，并拒绝阶段跳跃、文件缺失或不闭合目录。当前offline workflow合同版本为`4.0.0`，manifest版本为`offline-rto-manifest-4.0.0`。Agent确认路径遇到已完成的同一问题时只读取简洁`result.json`，不会进入严格恢复。新鲜运行完成后直接返回内存记录，不再为了打印或聊天回复执行一次完整的事后读取。

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

Agent的确认运行入口不只返回七字段摘要，而是返回一份受信`OptimizationRunReceipt`：

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

启动`rto-chat`后可以直接说：

```text
把常压装置的炉子运行得更省能一些
同时兼顾能耗和收率，炉温和塔顶压力都可以调
可以，开始计算
把能耗改成第一优先
取消这次优化
```

模型只在固定语义类别内理解这些表达，不要求严格关键词匹配。同一条消息含有多个独立需求时，模型可以一次返回多个不重复的闭集语义route，由代码校验组合并执行固定只读能力。这些route不是工具名或路径，模型不能自由调用文件或其他动作。

只说“帮我优化”或日常语义的“生成一条优化策略”而没有具体目标时，系统会列出当前目标、变量和可照着说的示例，不自行补目标，也不直接拒绝。给出具体目标并要求设定值或操作方案时，日常语义的“策略”可进入优化。正式创建、审批、发布、下装或现场控制仍不在Agent动作中，界面会说明边界并引导到离线设定点建议。

优化route还必须输出完整D0意图；D0需要补充时按有限选项回答。一条消息即使同时询问能力或工况，也只能生成一份Intent。模型响应不包含确认文字；严格Intent通过后，程序按能力业务名称生成确认摘要。用户下一轮确认后，系统才执行一次RTO。

严格意图解析成功后，Agent会展示程序生成的目标顺序、调整变量和输出数量摘要。`max_candidates=N`会显示为1个推荐与最多N-1个其他候选，而不是N个备选。此时不会读取Context或生成Problem。用户整句输入“确认”或“取消”时，本地程序直接执行或清除；带标点的确认、其他说法和其他回应只由小合同分类为确认、修改、取消或提问。修改分类成立后，才生成完整替代Intent，新意图成功前原方案保留。用户也可以使用快捷命令：

```text
/capabilities
/confirm
/result <结果编号|目录|result.json>
/cancel
```

`/confirm`没有附加令牌。`/result`接收裸`workflow_id`时，只在固定`runs/rto`根下解析对应`result.json`；也可读取用户显式指定的运行目录或结果文件。这条路径只校验普通文件、UTF-8 JSON、重复键、非有限数和七个顶层字段；不读取manifest、重放证据或查询策略库。成功读取后，这份摘要会成为当前会话的最近结果，可继续自然语言追问。

刚完成的优化先把受信回执留在当前Agent会话，再把其中七字段摘要交给模型组织回复。用户后续自然语言追问时，`last-result`只引用当前会话的这份回执，并可与能力等只读需求合并。进程重启或`/clear`后这个会话引用消失，但磁盘文件保留；系统不用全局文件修改时间猜测用户的上次结果。需要时由用户显式给出`workflow_id`，成功`/result`读取后即可在新会话继续追问。

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
