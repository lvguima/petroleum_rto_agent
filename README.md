# Petroleum RTO Agent

_模块化石油加工装置优化项目；当前已完成CDU Mini Loop仿真后端、统一`objectives[1..N]`离线RTO主链，以及可多轮对话并解释离线RTO结果的极简DMX入口。_

---

## 📍 项目入口

- [当前实施状态](docs/STATUS.md)
- [项目文档导航](docs/README.md)
- [目录与模块边界](docs/architecture/01_项目目录与模块边界.md)
- [CDU机理模型综合说明](docs/cdu/01_CDU_Mini_Loop机理模型综合说明.md)
- [RTO系统综合说明](docs/rto/01_RTO系统综合说明.md)
- [DMX垂域模型与对话入口](docs/domain_model/01_聚合式垂域模型综合说明.md)
- [RTO离线运行与策略库使用说明](docs/rto/04_RTO离线运行与策略库使用说明.md)
- [RTO历史文档索引](docs/rto/archive/README.md)
- [R11多目标离线RTO验证报告](reports/rto/R11_MULTI_OBJECTIVE_OFFLINE_REPORT.md)

## 📦 当前模块

| 模块 | 安装源码 | 状态 |
| --- | --- | --- |
| **CDU** | `src/petroleum_rto/cdu/` | M0～M7.2已完成 |
| **RTO** | `src/petroleum_rto/rto/` | 统一Intent、原子能力、受信Context、ProblemBuilder、Solver路由、M2/M4评价、可恢复workflow和策略治理已成为默认入口；历史V1/V2只保留显式兼容读取 |
| **Domain Model** | `src/petroleum_rto/domain_model/` | `chat_settings.py`直接配置URL/模型/提示词，`chat.py`只发送`model + messages`；默认DeepSeek已完成真实短对话和一次RTO结果解释联动 |
| **Assistant CLI** | `src/petroleum_rto/assistant/` | `rto-intent`/`rto-chat`提供内存多轮对话及显式`/result`结果解释，不自动运行RTO |

配置、数据、测试、脚本、报告和运行产物按模块放在对应顶层目录的子目录中。普通Chat的三个可编辑设置位于`src/petroleum_rto/domain_model/chat_settings.py`，本地SK位于同目录下被Git忽略的`dmx_api.json`；`configs/domain_model/`只保留旧严格意图实现兼容。详细约束见[目录与模块边界](docs/architecture/01_项目目录与模块边界.md)。
