# Petroleum RTO Agent

面向石油加工的离线工程系统：以CDU Mini Loop提供机理仿真，以一套目标数量无关的RTO链完成问题构造、候选搜索、配对评价和策略治理，并提供一个不自动执行RTO的本地对话入口。

所有结果均来自合成工程仿真，不代表现场验证、安全边界、实际收益或可直接下装的控制策略。

## 项目入口

- [当前实施状态](docs/STATUS.md)
- [项目文档导航](docs/README.md)
- [目录与模块边界](docs/architecture/01_项目目录与模块边界.md)
- [CDU机理模型综合说明](docs/cdu/01_CDU_Mini_Loop机理模型综合说明.md)
- [RTO系统综合说明](docs/rto/01_RTO系统综合说明.md)
- [RTO离线运行与策略库使用说明](docs/rto/04_RTO离线运行与策略库使用说明.md)
- [垂域对话工具说明](docs/domain_model/01_聚合式垂域模型综合说明.md)

## 当前模块

| 模块 | 位置 | 当前职责 |
| --- | --- | --- |
| CDU | `src/petroleum_rto/cdu/` | 稳态、动态、闭环、校正、验证和严格运行证据 |
| RTO | `src/petroleum_rto/rto/` | `1..N`目标的统一意图、问题、求解、评价、离线编排和策略治理 |
| Domain Model | `src/petroleum_rto/domain_model/` | 有界的本地Chat Completions客户端，不自动调用求解或仿真 |
| Assistant | `src/petroleum_rto/assistant/` | 组合Chat与已有RTO结果/仿真配置的安全只读摘要 |

命令行入口只有`cdu-mini`、`rto-offline`和`rto-chat`。配置、数据、测试、报告和运行产物按模块放在各自的顶层子目录；详细边界见[目录与模块边界](docs/architecture/01_项目目录与模块边界.md)。
