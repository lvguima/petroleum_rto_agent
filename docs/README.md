# 项目文档导航

_更新日期：2026-08-21 · 本目录保存项目级公共文档，并按模块分离CDU、RTO与DMX垂域对话资料。_

---

## 📋 公共入口

| 文档 | 用途 |
| --- | --- |
| [实施状态](STATUS.md) | 当前进度、授权、门禁和下一步的唯一事实来源 |
| [目录与模块边界](architecture/01_项目目录与模块边界.md) | 工作区层次、依赖方向和路径兼容规则 |
| [项目建设方案](project/1322建设方案0802.docx) | 项目级原始建设方案 |

## 📚 模块文档

### CDU机理仿真

- [机理模型综合说明](cdu/01_CDU_Mini_Loop机理模型综合说明.md)
- [常压蒸馏工艺及数据分析](cdu/01_常压蒸馏工艺及数据分析.md)
- [历史建模文档索引](cdu/archive/README.md)

### RTO与策略库

- [RTO系统综合说明](rto/01_RTO系统综合说明.md)
- [垂域模型与统一RTO通信协议](rto/02_垂域模型与RTO通信协议.md)
- [RTO离线运行与策略库使用说明](rto/04_RTO离线运行与策略库使用说明.md)

### DMX垂域模型

- [DMX垂域模型与对话入口](domain_model/01_聚合式垂域模型综合说明.md)
- [垂域模型与统一RTO通信协议](rto/02_垂域模型与RTO通信协议.md)
- [D2.2 DeepSeek与RTO真实联动测试报告](../reports/domain_model/D2_2_DEEPSEEK_RTO_LINKAGE_REPORT.md)

普通使用先阅读综合说明：当前只提供可编辑的DeepSeek Chat配置、内存多轮CLI和用户显式触发的RTO结果解释。严格通信协议继续定义自然语言意图进入`ProblemBuilder`前的合同，但旧供应商发现、评测和多协议设施不再是普通Chat主线。真实DeepSeek短对话及一次单目标动态RTO结果解释联动已完成，实时状态以[实施状态](STATUS.md)为准。

### RTO历史与证据

- [RTO历史文档索引](rto/archive/README.md)
- [R6离线RTO与策略草案验证报告](../reports/rto/R6_OFFLINE_RTO_REPORT.md)
- [R6.1外部结构化请求验证报告](../reports/rto/R6_1_EXTERNAL_REQUEST_REPORT.md)
- [R11多目标离线RTO验证报告](../reports/rto/R11_MULTI_OBJECTIVE_OFFLINE_REPORT.md)

## 🔗 事实优先级

1. 可执行源码、版本化配置、自动测试和正式证据
2. [实施状态](STATUS.md)
3. 对应模块的现行主文档
4. 历史归档和讨论稿
