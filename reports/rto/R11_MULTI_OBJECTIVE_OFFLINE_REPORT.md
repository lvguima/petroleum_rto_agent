# R11多目标离线RTO与策略草稿验证报告

_验证日期：2026-08-20 · 请求版本：`external-optimization-request-v2` · 声明范围：合成工程仿真_

---

## ✅ 结论

R7～R11多目标主链已经用当前CDU M7公共接口完整运行并通过交付门禁：严格垂域意图经确定性问题构造，完成81点M2配对评价、三目标Pareto分层、显式词典序偏好、Top-5 M4闭环复核、点上下文验证和单动作策略草稿创建。重复`run`、独立`inspect`以及仓库外wheel环境的严格读取均没有新增仿真，result和manifest指纹保持一致。

本报告不批准或发布策略。`strategy-v2-7dacfe7becd2d408-r1`仍为revision-1 `draft`；V2仓储当前刻意不提供审核、发布或查询API。

## 🧭 R7～R11交付结果

| 里程碑 | 已验证交付 |
| --- | --- |
| R7 | 三目标、硬门禁、81点政策、Top-5、词典序偏好及发布门禁版本化 |
| R8 | 严格V2意图/请求合同、能力目录、结构化错误、语义与审计指纹分离 |
| R9 | 三目标配对评价、确定性完整网格、精确非支配层和错误隔离 |
| R10 | Pareto偏好、Top-5完整M4复核、动态回退和独立发布性判定 |
| R11 | 可恢复workflow、严格重放、V2策略草稿、显式CLI、真实运行和wheel交付 |

V1单目标合同、命令和历史workflow继续保留；V2通过`--request-version v2`显式路由，不靠字段猜测版本。

## 📊 正式运行摘要

| 项目 | 结果 |
| --- | --- |
| Workflow | `offline-rto-v2-c69e0e46572af52f` |
| 上下文 | 407.3 t/h点上下文，弱时间对齐fixture |
| 搜索 | 81个候选；81个静态可行；0个工艺不可行；0个评价错误 |
| Pareto | 第一前沿5个候选；完整非支配分层13层 |
| 动态复核 | Top-5全部执行M4且全部可行 |
| 首次物理执行 | M2 81次；M4 6次（1个共享基准＋5个候选） |
| 重复运行 | M2 0次；M4 0次 |
| 独立inspect | M2 0次；M4 0次 |
| 优化结果 | `success`、`selected-publishable` |
| Workflow结果 | `completed_draft` |

### 第一Pareto前沿与动态结果

所有前沿候选的炉出口目标均为`626.35 K（353.2 °C）`，能耗代理均为`183.993068 MJ/t`；塔顶压力提高带来极小的质量代理偏离和有效馏分收率增加。默认偏好先保护质量，因此选择第一行。

| 偏好序 | 塔顶压力 Pa(a) | 表压 MPa(g) | 质量代理偏离 | 有效馏分收率 | M4 | 最大稳定时间 |
| ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 1 | 152325 | 0.0510 | 0 | 0.4919579477 | feasible | 0 s |
| 2 | 152825 | 0.0515 | 0.0000098736 | 0.4919759016 | feasible | 0 s |
| 3 | 153325 | 0.0520 | 0.0000197300 | 0.4919937223 | feasible | 168 s |
| 4 | 153825 | 0.0525 | 0.0000295691 | 0.4920114111 | feasible | 351 s |
| 5 | 154325 | 0.0530 | 0.0000393905 | 0.4920289696 | feasible | 451 s |

这些点是当前离散局部网格和缩减模型上的Pareto结果，不是连续真实装置前沿。

## 🎯 选中动作与三目标结果

| 内容 | 基准 | 候选 | 方向性改善 |
| --- | ---: | ---: | ---: |
| 质量代理最大绝对相对变化 | 0 | 0 | 0；相对改善因零基准而为空 |
| 有价值馏分收率 | 0.4919579477 | 0.4919579477 | 0 |
| 单位进料炉燃料热负荷代理 | 188.378985 MJ/t | 183.993068 MJ/t | 4.385917 MJ/t，约2.3282% |

选中的高层稳态向量为：

- 炉出口温度目标：`626.35 K（353.2 °C）`
- 塔顶压力目标：`152325 Pa(a)（0.051 MPa(g)）`

点锚点的M2和M4均为`feasible`。策略摘要中的最小规范化裕度为`0`，原因是摘要包含等式型结构/acceptance门禁；该值不是现场安全裕量。

## 🔐 策略与证据

| 对象 | 标识或指纹 |
| --- | --- |
| Pareto search | `6f949035c513d66c28d326c0304c11ccaa03fd0865b1066ac391fc94d556c7f5` |
| Preference selection | `313e8812a213dc252c7550a2f14579c2837a71cd589e17f6f493bfe41c82ce44` |
| Dynamic verification | `26ade7c05b0ad4c361f23df9d2194833f16b428191db0772df37b13a9507351b` |
| Optimization result | `7018a54a2c4faa9bd28f6371d88018e521f0c6b7db2d391a9cc6d75f1cafff51` |
| Offline result | `feb0b4fe24187b9d07953572f3395fecc46738ec55cf33974fcb38953a9a393f` |
| Workflow manifest | `662e8ee6e7dd6f7a70694f3b6ec45fa03cc553e116b341042867ad39a9b19a89` |
| Strategy draft | `strategy-v2-7dacfe7becd2d408-r1`，指纹`7dacfe7becd2d408d0543724cd5c14a8c2c66898aa76f226e21457e83c5f7247` |

V2 workflow保存请求、解析意图、问题、完整Pareto评价、偏好、动态复核、优化结果、锚点、策略草稿、append-only事件和manifest-last证据。策略正文只保留单一动作、三目标摘要和证据引用，不嵌入完整Pareto集合或时间序列。

机器可读摘要见[r11_multiobjective_offline_summary_v2.json](r11_multiobjective_offline_summary_v2.json)。完整M7运行证据位于本地忽略目录`runs/rto/offline-rto-v2-c69e0e46572af52f/`，不复制进报告。

## 📦 安装包与回归门禁

| 门禁 | 结果 |
| --- | --- |
| RTO专项测试 | 85项通过 |
| 全仓测试 | 735项通过 |
| Ruff | 全仓通过 |
| Strict mypy | 134个源码/脚本文件通过 |
| RTO格式 | 73个源码/测试文件通过 |
| 空白检查 | `git diff --check`通过 |
| wheel | 468690 bytes，SHA-256 `7a68d404f9eb5e63764446b55f2d6389adf9f2c2b22fbfbd5d3a2f37849bd106` |
| sdist | SHA-256 `dbd6596a195ace6cc9744fef42392022e3a3f40d67f623fdc437b97ba66f3f84` |

候选wheel位于`dist/r11-final/petroleum_rto_cdu_model-0.1.0-py3-none-any.whl`。它由隔离allowlist源码副本经临时sdist构建；源码副本、sdist和wheel中的全部Python/JSON字节一致。仓库外Python 3.12.14环境中：

- 安装包来自`site-packages`，仓库根不在`sys.path`
- `pip check`通过，无项目运行依赖
- V1/V2包内bundle、`rto-offline`和`cdu-mini`入口可用
- V2 capabilities、意图校验、V1/V2请求校验均不调用求解器
- 既有V1 R6 workflow和本轮V2 R11 workflow均由安装包严格读取成功，新增M2/M4执行为零

## ⚠️ 权限和限制

- `execution_scope=offline_simulation_only`
- `control_authority=none`
- `field_validated=false`
- `dcs_write_capability=false`
- 当前质量和收率是缩减模型代理，不构成产品标准。
- 当前能耗目标只是炉燃料热负荷代理，不是完整装置经济目标。
- 本次只验证407.3 t/h一个上下文点，不能表述为连续区间。
- 当前V2策略只能保持`draft`；没有自动审批、发布或现场下装路径。
- 本阶段没有实现垂域模型本体、HYSYS、Redis、消息队列、HTTP或DCS/SIS/LIMS接口。
