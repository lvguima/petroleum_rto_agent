# Petroleum RTO Agent

当前Agent使用唯一的原生工具循环：除本地命令和整条确认输入外，自然语言交给模型，按需要查询装置身份、工况或已有结果，再把工具结果交回模型继续回答。支持`/model`切换精确模型ID及`/thinking`设置；原始会话、工况快照、固定方案和结果保存到本机SQLite，切换模型及重启后保留。重启只展示恢复摘要，不自动请求模型或计算。

先读取HYSYS工况并准备单项或多项MV调节方案，用户确认后由固定节点保存基准副本、分别计算基准和候选并比较各自物料/能量证据；“只调温度”等修改撤销旧确认资格，修改失败也不能执行旧方案。旧对话按当前模型容量自动摘要，大工具结果保留全文并可分页读取。普通模型及摘要只对结构化临时错误有限重试，不自动换模型。旧自编CDU模型、M2/M4流程和离线RTO命令已退役。

只有`/confirm`或整句“确认”“确认执行”能确认已展示方案（仅忽略首尾空白），三者都由程序通过公共审批节点处理。已批准但未完成的任务，重启展示后须明确输入`/resume`；已有完整阶段严格核对证据后复用。新会话默认使用GPT Sol CDX，可按已配置的应用容量正常请求和摘要；已有会话恢复保存的模型选择。Flash仅使用非思考模式，其他模型遵循各自支持的设置。

模型端点、推理参数及容量依据见[当前Agent说明](docs/domain_model/01_聚合式垂域模型综合说明.md)，实施证据见[状态文档](docs/STATUS_REACT_REBUILD.md)。所有计算结果属于合成工程仿真。

## 本地启动

当前在Windows项目目录继续开发，分支为`feat/react-agent`。本机已安装锁定依赖，使用项目Python启动：

```powershell
Set-Location D:\pyproject\petroleum_rto_agent
.\.venv\python.exe -m petroleum_rto.domain_model
```

本机也可用短入口`.\.venv\Scripts\rto-chat.exe`。更新代码后退出旧进程再启动，并保持HYSYS中的源模型打开。对话示例：

```text
列出当前可调整的控制变量及其数值和单位
将原油入口压力调到180.1 kPa，准备与当前工况比较
改成同时把Water1流量调到2201 kg/h，先展示方案
确认
```

示例目标仅用于说明操作；程序展示所有选定变量的原值和目标值后，才接受下一条确认。未选MV保持原设定，CV用于读取计算结果。

当前`.venv`是Conda形式的Python 3.12环境，解释器直接位于`.venv/python.exe`。后续安装依赖使用下列定向流程，保留现有环境：

```powershell
uv export --frozen --extra domain-model --group dev --no-emit-project --output-file runs/simulation/requirements-windows.txt
uv pip install --python .venv/python.exe --require-hashes --requirements runs/simulation/requirements-windows.txt
uv pip install --python .venv/python.exe --editable . --no-deps --no-build-isolation
```

全新检出且没有既有环境时可用`uv sync --locked --extra domain-model --group dev`；标准Windows虚拟环境的解释器位于`.venv/Scripts/python.exe`，POSIX位于`.venv/bin/python`。启动后用`/help`或`/model`核对命令。本机密钥使用`src/petroleum_rto/domain_model/dmx_api.json`，不随Git同步；没有配置时启动明确报错。

## Windows接续开发

在仓库根目录阅读[当前状态与下一步](docs/STATUS_REACT_REBUILD.md)和[HYSYS稳态替换规划](docs/simulation/01_HYSYS稳态仿真接入规划.md)。Windows会话、文件锁、凭据保护和终端已适配；运行与中断的具体验收范围见状态文档。当前Agent支持24项已绑定MV的单项或组合稳态比较，保存基准副本后分别计算基准和候选；重复性未通过时不做最优排名。真实模型连接需要本机DMXAPI凭据。

用户提供的模型和脚本位于`hysys/`。正式simulation模块支持60变量、73级和20项塔规格的严格快照、内存基准保存/恢复、MV批量写入、单点求解及结果重载，以及独立的物料/组分/能量边界读取。固定ABBA实测发现同目标重复输出有差异，尚不能用于可靠候选排序；命令和限制见[HYSYS连接与状态读取说明](docs/simulation/02_HYSYS连接与状态读取说明.md)。需先打开源模型，程序在同一HYSYS应用中打开独立工作案例；独立新进程尚未成功，已接入全部24项MV的设定接口，优化搜索尚未开放。规划中的Mac路径仅记录历史来源。会话库、运行副本、base_files原始资料和本机配置不随Git同步；固定快照与正式配置仍按各自模块归属管理。

## 项目入口

- [ReAct改造状态与实施规划](docs/STATUS_REACT_REBUILD.md)
- [改造前状态基线](docs/STATUS.md)
- [项目文档导航](docs/README.md)
- [目录与模块边界](docs/architecture/01_项目目录与模块边界.md)
- [领域智能体架构](docs/architecture/02_领域智能体架构与功能设计.md)
- [RTO系统综合说明](docs/rto/01_RTO系统综合说明.md)
- [本地领域Agent说明](docs/domain_model/01_聚合式垂域模型综合说明.md)

## 当前模块

| 模块 | 位置 | 当前职责 |
| --- | --- | --- |
| Simulation | `src/petroleum_rto/simulation/` | HYSYS观测、基准副本、MV计算、恢复与严格证据 |
| RTO | `src/petroleum_rto/rto/` | MV比较的准备、串行执行、结果重建和HYSYS适配 |
| Domain Model | `src/petroleum_rto/domain_model/` | DMX原生Chat/Responses适配、精确模型配置和本地凭据读取 |
| Assistant | `src/petroleum_rto/assistant/` | LangChain工具循环、本机会话恢复、模型切换、公共审批与单一HYSYS稳态比较节点 |

当前命令行入口为`rto-chat`；也可使用上面的Python模块入口。旧CDU说明、数据与验收报告作为历史资料保留，见[文档导航](docs/README.md)。
