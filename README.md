# Petroleum RTO Agent

当前Agent使用唯一的原生工具循环：自然语言统一交给模型，按需要查询装置身份、工况或已有结果，再把工具结果交回模型继续回答。支持`/model`切换精确模型ID及`/thinking`设置；进程内会话、工况快照、方案版本和结果随切换保留。

优化先读取工况并准备方案，用户确认后执行完整静态搜索和动态复核；“只调温度”等修改产生新版本并撤销旧确认资格。旧对话按当前模型容量自动摘要，大工具结果保留全文并可分页读取。独立`rto-offline`计算底座保持原有合同。真实DMX兼容性已进入逐模型验收，成功和失败组合见状态记录；GPT Sol CDX的精确容量未核实，当前仅可选择、暂不发请求。

已按用户选择收紧执行授权：只有`/confirm`或整句“确认”“确认执行”能授权计算（仅忽略首尾空白）。修改要求继续由模型处理并重新展示方案。Flash仅使用非思考模式，切换其他模型默认开启思考；CDX流式工具事件读取已修复，精确容量与门禁策略仍待处理，当前改造尚未通过总验收。

模型端点、推理参数及容量依据见[当前Agent说明](docs/domain_model/01_聚合式垂域模型综合说明.md)，实施证据见[状态文档](docs/STATUS_REACT_REBUILD.md)。所有计算结果属于合成工程仿真。

## 本地启动

当前开发和测试统一使用桌面项目目录，开发分支为`feat/react-agent`。

```bash
cd /Users/idc/Desktop/petroleum_rto_agent
.venv/bin/rto-chat
```

首次重建环境时执行`uv sync --locked --extra domain-model --group dev`。启动后用`/help`或`/model`核对新版命令；原有DMX密钥保留在桌面项目源码目录。

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
| Domain Model | `src/petroleum_rto/domain_model/` | DMX原生Chat/Responses适配、精确模型配置和本地凭据读取 |
| Assistant | `src/petroleum_rto/assistant/` | LangChain工具循环、共享会话、模型切换、方案确认与分阶段领域工具 |

命令行入口为`cdu-mini`、`rto-offline`和`rto-chat`。
