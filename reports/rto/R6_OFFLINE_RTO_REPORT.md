# R6 离线RTO与策略草案验证报告

_验证日期：2026-08-19 · 声明范围：合成工程仿真 · 状态：离线草案，未审核、未发布_

---

## ✅ 结论

R6离线主链已用当前CDU M7公共接口完整运行：人工fixture经确定性问题构造、M2搜索、Top-3 M4复核、三个进料锚点评价后，生成一个不可变策略草案。重复`run`与独立`inspect`均从严格证据恢复，没有新增仿真，结果与manifest指纹保持一致。

本报告不批准该草案。`strategy-446ea56cd07f3d61-r1`仍处于`draft`，没有进入approved或published release；授权软件实施不等于离线策略内容审核。

## 📊 运行摘要

| 项目 | 结果 |
| --- | --- |
| Workflow | `offline-rto-fc174fafb89288e5` |
| 优化结果 | `success`，指纹`446ea56cd07f3d61…` |
| 离线结果 | `completed_draft`，指纹`35532d5979d54aec…` |
| Workflow manifest | `751b9438e05e57ef…` |
| 策略草案 | `strategy-446ea56cd07f3d61-r1`，指纹`15375a142004bb14…` |
| 动作 | 炉出口`626.35 K（353.2 °C）`；塔顶压力`152325 Pa(a)（0.051 MPa(g)）` |
| 首次物理执行 | M2 34次；M4 8次 |
| 重复运行 | M2 0次；M4 0次；严格重载相同manifest与结果指纹 |

## 📍 三点采样覆盖

| 进料 | 基准代理（MJ/t） | 候选代理（MJ/t） | 相对改善 | M2 | M4 |
| ---: | ---: | ---: | ---: | --- | --- |
| 386.935 t/h | 188.926272 | 184.540355 | 2.3215% | feasible | feasible |
| 407.300 t/h | 188.378985 | 183.993068 | 2.3282% | feasible | feasible |
| 427.665 t/h | 187.883820 | 183.497903 | 2.3344% | feasible | feasible |

这只是`0.95/1.00/1.05`三个离散进料点的采样覆盖。查询器只在明确锚点及版本化测量容差内命中，不对锚点之间插值，不声称连续区间、安全范围或现场适用域。

策略摘要中的最小规范化裕度包含等式型结构/acceptance门禁，因此通过点可出现数值`0`；它不表示现场安全裕量为零，也不能用来推导设备安全余量。

## 🔄 可恢复性和交付

- Workflow使用固定任务ID、阶段JSON、append-only事件链和manifest-last提交。
- 恢复会严格读取M7 run目录，核对request、effective input、result、manifest、版本和来源指纹。
- StrategyEntry采用精简正文，不内嵌完整评价或时间序列，只保留评价引用、关键效果摘要和依赖引用；完整证据保存在工作流与M7运行产物中。
- 已构建`dist/r6-final/petroleum_rto_cdu_model-0.1.0-py3-none-any.whl`，SHA-256为`187cccbb30c4a9d8192c056b84bbedc2f1a88e2ba10798d758164c7eb785cda4`。
- wheel包含固定RTO V1包内配置和`rto-offline`入口；仓库外Python 3.12环境可在不访问checkout配置的情况下构造相同问题并执行查询，`pip check`通过。

机器可读摘要见[r6_offline_rto_summary_v1.json](r6_offline_rto_summary_v1.json)。完整M7运行轨迹保存在本地忽略目录`runs/rto/offline-rto-fc174fafb89288e5/`，由workflow及各M7 manifest约束，不纳入报告正文。

## ⚠️ 权限和限制

- `execution_scope=offline_simulation_only`
- `control_authority=none`
- `field_validated=false`
- `dcs_write_capability=false`
- 当前目标是单位进料炉燃料热负荷代理，不是完整装置经济目标。
- 当前质量与收率门禁包含缩减模型代理，不构成产品放行。
- 策略发布必须另行执行显式离线审核和release；本次没有代替MOC、SIS、工艺或生产审批。
