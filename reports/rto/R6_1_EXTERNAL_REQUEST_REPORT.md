# R6.1外部结构化请求验证报告

_验证日期：2026-08-19 · 输入来源：人工JSON示例 · 声明范围：合成工程仿真_

---

## ✅ 结论

RTO现已能够直接读取严格、版本化的外部JSON，并将其确定性绑定为内部`OperatingContextV1`、`OptimizationIntentV1`和`OptimizationProblemV1`。同一格式允许`source_type=domain-model`，可作为后续垂域模型的输出合同。

400 t/h新案例完成了真实CDU M2/M4流程并生成一个单点策略草稿；严格重载没有新增仿真。草稿未审批、未发布，不具有现场执行权限。

## 📥 输入与问题

| 对象 | 标识或结果 |
| --- | --- |
| 外部请求 | `user-defined-feed-400-v1` |
| 请求指纹 | `d8bd20f116de882a…` |
| 进料上下文 | `400.0 t/h`，`point`覆盖 |
| 内部问题 | `problem-54712dcc1665a0b0` |
| 目标 | 最小化单位进料炉燃料热负荷代理 |
| 决策 | 炉出口温度目标、塔顶压力目标 |

无求解`validate-request`成功，输出`solver_called=false`。输入解析会拒绝未知字段、重复JSON键、非有限数值、布尔数值冒充、过期基础上下文引用、未实现目标和越可信域采样锚点。

## 📊 真实流程结果

| 项目 | 结果 |
| --- | ---: |
| Workflow | `offline-rto-18776d9263c19ce9` |
| 首次物理执行 | M2 30次；M4 4次 |
| 严格重载 | M2 0次；M4 0次 |
| 基准目标 | `188.568757 MJ/t` |
| 候选目标 | `184.182840 MJ/t` |
| 合成相对改善 | `2.3259%` |
| 炉出口目标 | `626.35 K（353.2 ℃）` |
| 塔顶压力目标 | `152325 Pa(a)（0.051 MPa(g)）` |
| 策略状态 | `strategy-69fc8b123a3b7d8d-r1`，`draft` |

外部请求引用随workflow保存。检查自定义workflow时必须再次提供同一JSON；请求ID或内容指纹不同会被严格拒绝。

## 🧪 验证门禁

- RTO专项`55`项通过
- CDU/RTO单元`608`项通过
- CDU/RTO集成与回归`97`项通过
- 全测试收集`705`项
- 全仓Ruff通过，`119`个源码/脚本文件strict mypy通过
- `54`个RTO源码/测试文件格式通过
- wheel仓库外安装、包内固定配置解析和`pip check`通过

wheel位于`dist/r6-1-final/petroleum_rto_cdu_model-0.1.0-py3-none-any.whl`，SHA-256为`d3ddbbaedf35d8fcb301f9ef71b003413a75491d78f88624bb0379482da61ea5`。

机器可读结果见[r6_1_external_request_summary_v1.json](r6_1_external_request_summary_v1.json)。输入格式和命令见[RTO离线运行与策略库使用说明](../../docs/rto/04_RTO离线运行与策略库使用说明.md)。

## ⚠️ 当前限制

- 自然语言本身仍不是机器事实；垂域模型必须输出严格JSON。
- V1只开放`minimize-specific-furnace-energy-v1`目标和既有两变量目录。
- 外部上下文只能引用受信基础上下文并覆盖已允许的进料字段，不能注入模型内部路径。
- 本次结果只覆盖400 t/h单点，不代表连续区间或现场适用域。
- `execution_scope=offline_simulation_only`、`control_authority=none`、`field_validated=false`、`dcs_write_capability=false`。
