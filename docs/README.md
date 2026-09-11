# 项目文档导航

_更新日期：2026-09-11 · 本目录导航现行规范、新阶段实施规划与仍在使用的证据。_

本阶段状态统一记录在[ReAct改造状态与实施规划](STATUS_REACT_REBUILD.md)。旧状态文档作为改造前基线保留，不再追加新阶段记录；Agent主文档描述当前工具循环、持久化、审批、上下文与重试边界；最终兼容验收以状态记录为准。

## 公共入口

| 文档 | 用途 |
| --- | --- |
| [ReAct改造状态与实施规划](STATUS_REACT_REBUILD.md) | 本阶段进度、授权、验证、风险和下一步的唯一状态来源 |
| [改造前状态基线](STATUS.md) | 旧阶段状态与验证记录，保留历史内容，不再追加新阶段状态 |
| [目录与模块边界](architecture/01_项目目录与模块边界.md) | 物理目录、依赖方向和跨模块规则 |
| [领域智能体架构](architecture/02_领域智能体架构与功能设计.md) | 统一工具循环、消息状态与领域边界 |
| [项目建设方案](project/1322建设方案0802.docx) | 项目级原始来源材料 |

## 旧CDU历史资料（实现已退役）

- [机理模型综合说明](cdu/01_CDU_Mini_Loop机理模型综合说明.md)
- [常压蒸馏工艺及数据分析](cdu/01_常压蒸馏工艺及数据分析.md)
- [CDU历史建模资料索引](cdu/archive/README.md)

旧模型说明、原始观测与验收报告原位保留供追溯；CDU代码、运行配置和专用测试已退役，本文所列历史材料不定义当前能力。

## HYSYS稳态仿真替换

- [稳态仿真替换与Agent接入规划](simulation/01_HYSYS稳态仿真接入规划.md)
- [当前连接与状态读取说明](simulation/02_HYSYS连接与状态读取说明.md)
- [来源文件核对记录](../reports/simulation/hysys_inventory_20260911.json)
- [Windows资料与环境接收核对](../reports/simulation/windows_hysys_intake_20260911.json)
- [Windows实际连接与读取证据](../reports/simulation/windows_hysys_read_20260911.json)
- [变量绑定、单位与正式读取验收](../reports/simulation/hysys_binding_qualification_20260911.json)
- [基准恢复与单变量诊断证据](../reports/simulation/hysys_baseline_recovery_20260911.json)
- [T-39正式单点与严格结果重载验收](../reports/simulation/hysys_t39_point_20260911.json)
- [固定顺序复现与物料/组分/能量边界验收](../reports/simulation/hysys_order_boundary_20260911.json)
- [旧模型删除与跨模块影响清单](../reports/simulation/hysys_replacement_scope_20260911.json)

用户已明确删除旧CDU与动态实现，改为HYSYS稳态及优先读取HYSYS工况。当前simulation模块已实测真实属性绑定、快照、内存基准/恢复、T-39单点及独立物料/组分/能量边界。固定ABBA的四点各自通过，但重复输出有差异，顺序验收失败；当前已接入逐候选边界及MV稳态Agent流程，实际验收与重复性诊断进度见STATUS。旧CDU主文档仅保留历史说明。

## RTO与策略库

- [RTO系统综合说明](rto/01_RTO系统综合说明.md)
- [历史D0通信协议（已退役）](rto/02_垂域模型与RTO通信协议.md)
- [历史离线命令与策略说明（已退役）](rto/04_RTO离线运行与策略库使用说明.md)

当前RTO编排24项MV的单项或组合基准/候选稳态比较及严格结果重载；旧D0通信、M2/M4、网格搜索和策略CLI已退役。通信与策略说明仅作为历史资料保留。

## 本地领域Agent

- [本地领域Agent说明](domain_model/01_聚合式垂域模型综合说明.md)
- [Windows依赖与运行基础验证](../reports/domain_model/windows_runtime_20260911.json)
- [历史D0通信协议（已退役）](rto/02_垂域模型与RTO通信协议.md)

Agent使用LangChain原生工具循环，自然语言不经过旧分类入口；装置、工况、结果查询实际回传模型。支持`/model`和`/thinking`，模型切换保留业务记录并重建合法协议上下文。模型只查询、准备或取消方案，真实输入通过公共审批后由单一节点完成HYSYS基准/候选计算及证据比较；当前命令为rto-chat。当前会话、固定方案、摘要和分页保存到本机SQLite，重启只展示并等待明确继续；普通与摘要请求使用有限重试。旧调用链已清理，五个精确模型的真实兼容状态分别记录在项目状态。

## 事实优先级

1. 可检查的源码、版本化配置、自动验证和正式证据；
2. [ReAct改造状态与实施规划](STATUS_REACT_REBUILD.md)；
3. 对应模块的现行主文档；
4. 只用于解释来源的归档材料。
