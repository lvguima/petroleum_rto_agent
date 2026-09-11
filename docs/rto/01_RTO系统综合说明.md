# RTO系统综合说明

_更新：2026-09-11。当前提供单项或多项MV的HYSYS稳态比较；执行状态与验证见[STATUS](../STATUS_REACT_REBUILD.md)。_

## 当前执行流程

Agent通过[runtime/steady.py](../../src/petroleum_rto/rto/runtime/steady.py)读取受信工况、准备固定方案，经程序展示和用户确认后串行执行：

1. 保存当前源案例的基准副本。
2. 从同一基准分别计算基准点和候选点，每点采集自己的物料、组分和能量证据。
3. 严格重建比较结果，保存结果并交给Agent解释。
4. 重启或查询已有结果时从保存的证据重建，不重新求解或请求模型。

调控量来自[变量目录](../../configs/simulation/mjh_atm_variables.json)中24项MV，可选择其中一项或多项，指定目标值与目录单位。CV仍只读。当前值不再限定为T-39的156.8℃，也不再限定两个离散目标；没有发布工艺上下限，不通过扫描失收敛来推定范围。当前不做搜索、质量合格判断、收益预测或最优排名。重复性未通过时，eligible_for_optimization保持false。

## 实际模块

| 位置 | 职责 |
| --- | --- |
| [runtime/steady.py](../../src/petroleum_rto/rto/runtime/steady.py) | 严格工况/方案校验、确认内容、串行编排、结果重建 |
| [adapters/hysys_steady.py](../../src/petroleum_rto/rto/adapters/hysys_steady.py) | 绑定HYSYS案例、版本化配置和仿真模块，验证逐点来源 |
| [_file_lock.py](../../src/petroleum_rto/rto/_file_lock.py) | 当前本地运行的单写者保护 |

包初始化不导入其他后端；Agent只消费明确的steady入口，仿真后端对象不进入Agent。仿真物理操作在[simulation模块](../simulation/02_HYSYS连接与状态读取说明.md)内，运行时不依赖LangChain。

## 保存与恢复

新方案为steady-comparison-plan/2.0.0，结果为steady-comparison-result/2.0.0；单点消费hysys-mv-point/1.0.0及同次边界。方案绑定完整观测、排序后的changes、完整变量目录和边界哈希；执行前检查当前目录及捕获目录与确认内容一致。历史T-39方案与结果保留原版本严格读取，旧确认不会扩展为多变量授权。每个结果目录为runs/rto/steady-编号。

准备不求解。只有已展示方案通过程序确认才能执行；修改方案撤销旧确认。完整结果必须通过文件完整性与逐点证据重建；部分目录不会被覆盖或隐式重算，未创建的下一步骤须明确/resume后继续。哈希是完整性检查，不是身份认证。

## 退役内容与历史资料

旧自编CDU、M2/M4评价、旧离线编排、网格求解、策略库和D0通信实现及其专用配置/测试已下线；cdu-mini和rto-offline不再提供。当前入口为rto-chat或python -m petroleum_rto.domain_model。

旧设计保留在[历史综合说明](archive/01_旧CDU离线RTO综合说明.md)、[旧通信协议](02_垂域模型与RTO通信协议.md)和[旧命令/策略说明](04_RTO离线运行与策略库使用说明.md)，仅供追溯。旧资料与验收报告保留，不构成活动能力或运行时兼容承诺。

共享HYSYS进程、COM阻塞缺少硬超时、同目标重复输出差异及质量约束缺失仍是当前限制；所有结果仅属于合成工程仿真。
