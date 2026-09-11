# Petroleum RTO Agent

当前Agent使用唯一的原生工具循环：除本地命令和整条确认输入外，自然语言交给模型，按需要查询装置身份、工况或已有结果，再把工具结果交回模型继续回答。支持`/model`切换精确模型ID及`/thinking`设置；原始会话、工况快照、固定方案和结果保存到本机SQLite，切换模型及重启后保留。重启只展示恢复摘要，不自动请求模型或计算。

优化先读取工况并准备方案，用户确认后由固定节点执行完整静态搜索和动态复核；“只调温度”等修改撤销旧确认资格，修改失败也不能执行旧方案。旧对话按当前模型容量自动摘要，大工具结果保留全文并可分页读取。普通模型及摘要只对结构化临时错误有限重试，不自动换模型。独立`rto-offline`计算底座保持原有合同。

只有`/confirm`或整句“确认”“确认执行”能确认已展示方案（仅忽略首尾空白），三者都由程序通过公共审批节点处理。已批准但未完成的任务，重启展示后须明确输入`/resume`；已有完整阶段严格核对证据后复用。新会话默认使用GPT Sol CDX，可按已配置的应用容量正常请求和摘要；已有会话恢复保存的模型选择。Flash仅使用非思考模式，其他模型遵循各自支持的设置。

模型端点、推理参数及容量依据见[当前Agent说明](docs/domain_model/01_聚合式垂域模型综合说明.md)，实施证据见[状态文档](docs/STATUS_REACT_REBUILD.md)。所有计算结果属于合成工程仿真。

## 本地启动

当前开发和测试统一使用桌面项目目录，开发分支为`feat/react-agent`。

```bash
cd /Users/idc/Desktop/petroleum_rto_agent
.venv/bin/rto-chat
```

首次重建环境时执行`uv sync --locked --extra domain-model --group dev`。启动后用`/help`或`/model`核对新版命令；原有DMX密钥保留在桌面项目源码目录。

## Windows接续开发

拉取`feat/react-agent`分支，在仓库根目录阅读[当前状态与下一步](docs/STATUS_REACT_REBUILD.md)和[HYSYS稳态替换规划](docs/simulation/01_HYSYS稳态仿真接入规划.md)。先用Python 3.12重建开发环境：`uv sync --locked --extra domain-model --group dev`；本次交付保留原CDU实现，新稳态接口及Windows兼容仍待开发，当前交互CLI不能直接视为Windows可运行。

Windows继续使用已有HYSYS目录，本次Git交付不包含Mac桌面的`hysys_moni`模型或脚本。规划和核对报告中的Mac绝对路径仅记录来源，接入时绑定Windows实际路径。密钥需在Windows本机独立配置，会话库、运行副本、原始资料和本机配置不随Git同步；固定快照与正式配置仍按规划纳入各自模块，不将全部JSON或模型扩展名一概忽略。

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
| Assistant | `src/petroleum_rto/assistant/` | LangChain工具循环、本机会话恢复、模型切换、公共审批与固定M2/M4节点 |

命令行入口为`cdu-mini`、`rto-offline`和`rto-chat`。
