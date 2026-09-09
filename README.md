# Petroleum RTO Agent

ReAct改造的状态与实施规划见[新阶段状态文档](docs/STATUS_REACT_REBUILD.md)。以下功能说明仍描述现有实现，改造尚未开始。

面向石油加工的离线工程系统：CDU Mini Loop提供机理仿真；一套目标数量无关的RTO链负责问题构造、网格搜索、配对评价和结果生成；本地Agent用自然语言完成语义路由、优化意图澄清与计算确认。

用户给出具体优化目标后，模型只能在固定语义类别中选择并生成严格D0意图；确认摘要由程序依据已校验意图和当前能力生成。整句只有“确认”或“取消”时由本地程序直接处理，其他说法、修改和追问仍由模型理解。没有给出目标的“帮我优化”或“生成优化策略”不会被冷拒绝，也不会擅自猜目标，而是展示当前可用目标、变量和可照着说的示例。

确认时系统重新校验当前能力，读取最新受信工况，并只构造一次`OptimizationProblem`后执行离线RTO。结果可包含一个推荐方案和若干其他候选；候选会如实标明只完成M2稳态评价，还是已有M4动态复核。这些候选不是已创建或发布的正式策略。

计算完成后，Agent直接使用内存中的结构化结果回答，并保存一份简洁`result.json`。普通对话路径不回读运行文件、不重复校验哈希、不重放物理证据，也不自动创建策略草案；严格workflow检查和策略治理是独立的开发者操作。

所有结果均来自合成工程仿真，不代表现场验证、安全边界、实际收益或可直接下装的控制策略。

## 项目入口

- [ReAct改造状态与实施规划](docs/STATUS_REACT_REBUILD.md)
- [改造前状态基线](docs/STATUS.md)
- [项目文档导航](docs/README.md)
- [目录与模块边界](docs/architecture/01_项目目录与模块边界.md)
- [领域智能体架构](docs/architecture/02_领域智能体架构与功能设计.md)
- [CDU机理模型综合说明](docs/cdu/01_CDU_Mini_Loop机理模型综合说明.md)
- [RTO系统综合说明](docs/rto/01_RTO系统综合说明.md)
- [RTO离线运行与策略库说明](docs/rto/04_RTO离线运行与策略库使用说明.md)
- [本地领域Agent说明](docs/domain_model/01_聚合式垂域模型综合说明.md)

## 当前模块

| 模块 | 位置 | 当前职责 |
| --- | --- | --- |
| CDU | `src/petroleum_rto/cdu/` | 稳态、动态、闭环、校正、验证和机理证据 |
| RTO | `src/petroleum_rto/rto/` | `1..N`目标意图、问题、求解、评价、离线编排、结果和独立策略治理 |
| Domain Model | `src/petroleum_rto/domain_model/` | 有界DMX Chat客户端和本地凭据读取 |
| Assistant | `src/petroleum_rto/assistant/` | LLM闭集语义路由、D0严格意图、程序生成确认摘要、离线执行和轻量结果读取 |

命令行入口为`cdu-mini`、`rto-offline`和`rto-chat`。
