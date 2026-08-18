# CDU Mini Loop M7 数据字典

> 文档状态：M7 已完成。单次与批处理字段已按最终严格契约逐项核对；发布证据与可信度边界
> 以[当前实施状态](STATUS.md)和[发布检查清单](M7_RELEASE_CHECKLIST.md)为准。

## 1. 通用约定

| 约定 | 含义 |
| --- | --- |
| 字符编码 | JSON/JSONL 均为 UTF-8 |
| 数值 | 必须有限；拒绝 `NaN`、`Infinity`，布尔值不能冒充数值 |
| 时间 | 过程时间使用秒 `s`；墙钟时间使用带 UTC 偏移的 ISO-8601 字符串 |
| 温度 | 绝对温度 `K` |
| 压力 | `Pa`；字段名含 `_pa`。案例中的 DCS 表压假设在进入模型前转换为绝压 |
| 质量/流量 | `kg`、`kg/s`；字段名分别含 `_kg`、`_kg_s` |
| 热负荷/功率 | `W`；字段名含 `_w` |
| 比值/分数 | 无量纲；通常为 `ratio`、`fraction` 或 `normalized` |
| 指纹/文件哈希 | 小写十六进制 SHA-256，共 64 字符 |
| 标识符 | 只接受受限的字母、数字、点、下划线和连字符；拒绝 `/`、`\` 和 `..` 路径穿越 |
| 来源 | 现场原始资料、M5 混合来源与 M2/M3/M4/M6 合成结果必须分别标识 |

运行契约版本当前为：

- `schema_version = "1.0.0"`；
- `request_version = "cdu-mini-run-request-v0.1.0"`；
- `custom_input_version = "cdu-mini-custom-input-v0.1.0"`；
- `runtime_version = "cdu-mini-runtime-v0.1.0"`；
- `manifest_version = "cdu-mini-run-manifest-v0.1.0"`。

## 2. `RunRequest`：单次运行请求

用户编写的请求文件采用稀疏覆盖语义，唯一必填字段是`preset_id`。可选字段为`parameters`、
`overrides`、`initial_state`、`scenario`、`metadata`、`random_seed`、`run_id`和`requested_at_utc`；
也允许显式填写下表中的版本和`run_type`，但必须与preset一致。未知字段一律拒绝。

稀疏文件加载后会先与固定preset确定性合并，再生成下表所示完整、严格、不可变的`RunRequest`。
运行目录中的`request.json`始终保存这个完整规范请求，而不是原始稀疏文本，因此reader仍按全部合同字段重载。

| 字段 | 类型 | 单位 | 必填 | 来源与含义 |
| --- | --- | --- | --- | --- |
| `schema_version` | string | — | 是 | 公共 schema 版本，必须与运行时一致 |
| `request_version` | string | — | 是 | 请求契约版本 |
| `preset_id` | string | — | 是 | 五个固定预设之一；来自固定注册表，不扫描文件系统 |
| `run_type` | enum | — | 是 | 四类运行之一，必须与预设一致 |
| `random_seed` | integer | — | 是 | 非负随机种子；当前模型为确定性，manifest 同时记录是否实际使用随机性 |
| `parameters` | object<string, number> | 由键定义 | 是 | 受控工况/进料参数；只接受第2.2节白名单，空映射表示继承模板 |
| `overrides` | object<string, number> | 由键定义 | 是 | 受控模型参数覆盖；只接受切割点及动态参数白名单 |
| `initial_state` | object<string, number> | 名义比例 | 否 | 三个动态质量库存初值；仅M3/M4可用，省略或空映射表示全部为1 |
| `scenario` | RuntimeScenarioRequest/null | 混合 | 否 | 动态时长、输出步长和事件覆盖；`null`表示完整继承预设场景 |
| `metadata` | object<string, string> | — | 是 | 请求语义元数据；会进入请求指纹，因此修改它会改变确定性请求身份 |
| `run_id` | string/null | — | 否 | 输出目录身份；不得含路径；不进入物理请求指纹 |
| `requested_at_utc` | string/null | — | 否 | 带时区的请求时间；不进入物理请求指纹 |
| `request_fingerprint` | SHA-256 | — | 序列化时生成 | 对除 `run_id`、`requested_at_utc` 外全部语义字段的规范指纹；读取时交叉校验 |

### 2.1 `run_type` 枚举

| 值 | 引擎来源 | 说明 |
| --- | --- | --- |
| `steady_recycle` | M2 | 稳态物理循环 |
| `open_loop_dynamic` | M3 | 开环动态 |
| `closed_loop_dynamic` | M4 | 七回路合成闭环 |
| `validation_scenario` | M6 | 选定验证场景或结构拒绝 |

### 2.2 受控输入目录

M2/M3/M4 请求只接受以下版本化输入。完整逐项范围可由
`cdu-mini inputs --preset <id> --json`机器读取；M6便携预设不接受这些覆盖。

| 分区 | 输入 ID | 请求单位 | 允许范围 |
| --- | --- | --- | --- |
| `parameters` | `feed.mass_flow_t_h` | t/h | 0.001～2000 |
| `parameters` | `feed.temperature_c` | °C | -100～500 |
| `parameters` | `feed.pressure_mpa_g` | MPa(g) | -0.1～10 |
| `parameters` | `feed.salt_mass_flow_kg_s` | kg/s | 0～10 |
| `parameters` | `feed.mass_fraction.<component>`，其中component为`light_ends/naphtha/kerosene/light_diesel/heavy_diesel/residue/water` | 1 | 0～1；最终七项必须合计为1 |
| `parameters` | `operating.flash_temperature_c`、`operating.furnace_outlet_temperature_c` | °C | 分别-100～500、-100～370 |
| `parameters` | `operating.tower_top_temperature_c`、`operating.condenser_temperature_c`、`operating.ambient_temperature_c` | °C | 分别-100～350、-100～250、-100～100 |
| `parameters` | `operating.flash_pressure_mpa_g`、`operating.tower_top_pressure_mpa_g` | MPa(g) | 分别-0.1～5、-0.1～3 |
| `parameters` | `operation.wash_water_ratio`、`operation.reflux_ratio` | 1 | 分别0～0.5、0～5 |
| `parameters` | `operation.pump_around_1_duty_mw`、`..._2_...`、`..._3_...` | MW | 0～200 |
| `overrides` | `column.cut_3_temperature_c`、`column.cut_4_temperature_c` | °C | -100～500；还必须满足模型切割点递增约束 |
| `overrides` | `dynamic.furnace_time_constant_s`、`preheater_time_constant_s`、`condenser_time_constant_s`、`tower_temperature_time_constant_s`、`actuator_time_constant_s`、`sensor_time_constant_s`、`flash_residence_time_s`、`reflux_drum_residence_time_s`、`tower_bottom_residence_time_s` | s | 0.001～1000000；仅M3/M4 |
| `overrides` | `dynamic.top_gas_volume_m3` | m³ | 0.001～1000000；仅M3/M4 |
| `initial_state` | `inventory.flash_drum_ratio`、`inventory.reflux_drum_ratio`、`inventory.tower_bottom_ratio` | 名义比例 | 0.1～10；同时缩放该容器全部组分、盐和对应库存测量初值 |

显示单位会在预览阶段确定性转换为SI：t/h→kg/s、°C→K、MPa(g)→Pa(a)、MW→W。
`parameters`、`overrides`与`initial_state`不能混放；同一个未知键不会按名称猜测或自动映射。

### 2.3 `RuntimeScenarioRequest`与运行事件

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `duration_s` | number/null | 正数；空值继承模板时长 |
| `time_step_s` | number/null | 正数；空值继承模板输出步长，且时长必须是步长整数倍 |
| `events` | array<RuntimeInputEvent>/null | `null`继承模板事件，空数组明确取消模板事件 |

`RuntimeInputEvent`字段为`time_s`、`target`、`value`、`value_basis`和可选`duration_s`。

- M3 `target`必须是11个执行量之一：`fresh_feed_flow_kg_s`、`flash_liquid_outflow_kg_s`、
  `gasoline_draw_kg_s`、`reflux_flow_kg_s`、`residue_draw_kg_s`、`top_gas_vent_kg_s`、
  `furnace_fuel_duty_w`、`condenser_cooling_duty_w`、`pump_around_1_duty_w`、
  `pump_around_2_duty_w`、`pump_around_3_duty_w`；`value_basis`可为`absolute`或`nominal_ratio`，
  `duration_s`用于临时事件，空值表示持续到场景结束。
- M4 `target`必须为七个回路ID之一加`.setpoint_ratio`：`feed_flow`、`flash_inventory`、
  `furnace_temperature`、`top_pressure`、`reflux_inventory`、`bottom_inventory`、`top_temperature`；
  `value_basis`固定为`setpoint_ratio`，不接受持续时间字段。

### 2.4 运行前预览与确认

`preview(request)`只解析包内资源、白名单覆盖、单位、组成、场景、初态和指纹，不调用模型求解器。
其结果保存请求值、模板值、SI值、有效模型/案例/场景摘要、基础/有效对象指纹、
`execution_input_fingerprint`和`preview_fingerprint`。普通CLI执行`run --request`时先展示该预览，
接收`y/yes/是/确认`后自动把同一指纹作为`expected_preview_fingerprint`传回；取消不会调用求解器或
创建运行目录。`--json/--quiet`等非交互模式及Python API必须显式传回指纹。任何语义字段变化都会令
旧确认失效，固定`run --preset`仍可直接运行。

## 3. `ExecutionPayload`：公共结果

在内存中，轨迹、事件和错误位于 `ExecutionPayload`。写入运行目录后，`result.json` 中这三项为空数组，
并由 `externalized` 记录数量；完整内容分别保存在 `timeseries.jsonl`、`events.jsonl` 和 `error.json`。
`read_run()` 会核对数量和哈希后重建完整对象。轨迹由一次顺序 JSONL 适配器交给结果合同，
不先用 `read_text().splitlines()` 展开为第二份完整列表；最终重建的 `ExecutionPayload` 仍持有一份不可变轨迹。

| 字段 | 类型 | 单位 | 来源与含义 |
| --- | --- | --- | --- |
| `schema_version` | string | — | 公共 schema 版本 |
| `runtime_version` | string | — | M7 运行时契约版本 |
| `preset_id` | string | — | 原请求预设 ID |
| `run_type` | enum | — | 原请求运行类型 |
| `runtime_status` | enum | — | 五态公共结果 |
| `request_fingerprint` | SHA-256 | — | 与请求绑定；结果不得属于另一请求 |
| `engine_status` | string | — | M2/M3/M4/M6 底层状态；结构拒绝通常为 `not_called` |
| `raw_result_type` | string | — | 底层结果类型或规范化异常类型 |
| `summary` | JSON object | 混合 | 层级专属摘要；M2 为循环结果，M3/M4 为守恒和场景摘要，M6 为场景验证摘要 |
| `timeseries` | array<object> | 见第 6 节 | 动态端点序列；M2 和结构拒绝通常为空 |
| `events` | array<EventRecord> | — | 有序命令、设定值、故障、保护和失败事件 |
| `errors` | array<ErrorRecord> | — | 有序结构化错误；`success`/`limited` 必须为空 |
| `versions` | object<string, string> | — | 软件、模型、参数集、案例、M5 覆盖、控制器、场景、验证等版本 |
| `source_fingerprints` | object<string, SHA-256> | — | 包内资源、M5/M6证据、有效对象和底层输入指纹 |
| `effective_input_fingerprint` | SHA-256 | — | 基础输入、M5 有效覆盖、场景及请求组合后的确定性输入身份 |
| `synthetic` | boolean | — | 当前 M2/M3/M4/M6 运行结果均为 `true` |
| `data_origin` | string | — | `M2_steady_model_prediction`、M3/M4 场景来源或 `M6_synthetic_validation` |
| `claim_scope` | string | — | M2/M3/M4 为 `engineering_simulation_only`；M6 为 `engineering_validation_only` |
| `failure_stage` | string/null | — | 失败、拒绝或未收敛发生阶段；非失败状态必须为 `null` |
| `failure_reason` | string/null | — | 人可读失败原因；非失败状态必须为 `null` |
| `failure_time_s` | number/null | s | 动态失败的过程时刻；稳态或求解前拒绝可为空 |
| `last_valid` | object/null | 混合 | 失败前最后有效流程或样本证据；不能解释成成功终值 |
| `duration_s` | number/null | s | 动态请求时长；M2 必须为空 |
| `time_step_s` | number/null | s | 动态名义输出步长；M2 必须为空 |
| `diagnostics` | object | 混合 | 样本数、完成时刻、收敛、适用域和引擎诊断 |
| `result_fingerprint` | SHA-256 | — | 对以上完整确定性结果（含轨迹、事件、错误）的规范指纹；轨迹部分逐样本增量计算，指纹与一次完整规范 JSON 序列化等价 |
| `externalized` | object | 计数 | 仅落盘 `result.json` 中存在，包含 `timeseries_count`、`event_count`、`error_count` |

### 3.1 `versions` 必要谱系

对 `success` 和 `limited` 结果，运行清单要求 `versions` 至少包含：

- `software_version`、`model_version`、`model_config_version`；
- `base_parameter_set_version`、`derived_parameter_set_version`、`parameter_set_version`；
- `base_case_version`、`derived_case_version`、`case_version`；
- `m5_overlay_version`、`control_version`、`scenario_version`；
- `analysis_basis_version`、`validation_version`。

`scenario_version` 对不使用场景的入口可为 `not_applicable`，但字段不能缺失。

### 3.2 公共状态

| 状态 | 错误要求 | 含义与处理 |
| --- | --- | --- |
| `success` | `errors=[]`，失败字段为空 | 该层数值执行和验收通过；仍受模型卡限制 |
| `limited` | `errors=[]`，失败字段为空 | 结果只在受限/代理域内解释；不能删去标签后当作普通成功 |
| `rejected` | 至少一个错误，含阶段和原因 | 求解前结构性拒绝；检查输入与适用域，不应当作收敛问题重试 |
| `not_converged` | 至少一个错误，含阶段和原因 | 迭代没有收敛；可检查初值、输入和最后有效证据 |
| `failed` | 至少一个错误，含阶段和原因 | 执行、守恒、契约或运行时失败；保留原目录后另建尝试 |

## 4. `EventRecord`：事件记录

`events.jsonl` 每行一个对象，`sequence` 必须从 0 连续递增。

| 字段 | 类型 | 单位 | 来源与含义 |
| --- | --- | --- | --- |
| `sequence` | integer | — | 本次运行中的稳定顺序号 |
| `time_s` | number/null | s | 过程事件时刻；无过程时钟的失败/拒绝可为空 |
| `event_type` | string | — | 如 `command_change`、`setpoint_change`、`fault_command`、保护转换或 `execution_failure` |
| `source` | string | — | 如 `M3_scenario`、`M4_scenario`、`M6_scenario`、`M6_protection`、`M7_runtime` |
| `stage` | string | — | 如 `M3_open_loop`、`M4_closed_loop`、`M6_portable`、失败阶段 |
| `message` | string | — | 人可读事件说明 |
| `details` | JSON object | 混合 | 原始配置事件、保护事件或错误类型等机器证据 |

事件记录用于解释发生了什么，不等同于现场 DCS/SIS 事件日志。
M3/M4 在底层失败时只发布截至 `completed_time_s` 已实际发生的配置事件；位于未来的配置事件不伪装成
已执行事件，而是放入 `diagnostics.unexecuted_configured_events`。

## 5. `ErrorRecord`：错误记录

`error.json` 的根对象为 `{"errors": [...]}`。数组中每项字段如下：

| 字段 | 类型 | 单位 | 来源与含义 |
| --- | --- | --- | --- |
| `sequence` | integer | — | 从 0 连续递增的错误顺序号 |
| `error_type` | string | — | 规范化错误类型，如 `NotConverged`、`StructuralRejection` |
| `stage` | string | — | 出错阶段，如请求预检、资源加载、模型执行或适用域预检 |
| `message` | string | — | 原始、非空错误说明 |
| `time_s` | number/null | s | 动态失败时刻；无过程时钟时为空 |
| `last_valid` | object/null | 混合 | 失败前最后有效证据 |
| `retryable` | boolean | — | 运行时对“是否可能通过新尝试重跑”的分类；不代表自动重试许可 |
| `details` | object | 混合 | 迭代数、是否调用求解器等附加证据 |

## 6. `timeseries.jsonl`：动态样本

每行是一个完整端点样本。字段可能较大，使用 JSONL 是为了逐行读取；不要用文本编辑器一次加载大型轨迹。

### 6.1 M3 开环及 M6 动态场景的植物样本

| 字段 | 单位 | 含义 |
| --- | --- | --- |
| `time_s` | s | 从场景起点计的过程时间 |
| `state.liquid_inventories.*.component_masses_kg` | kg | 闪蒸罐、回流罐、塔底中七组分的质量库存 |
| `state.liquid_inventories.*.salt_mass_kg` | kg | 各液相库存中的独立盐质量 |
| `state.liquid_inventories.*.total_mass_kg` | kg | 组分质量之和；是质量库存，不是真实几何液位 |
| `state.liquid_inventories.*.mass_fractions` | 1 | 库存质量分数 |
| `state.top_gas_component_masses_kg` | kg | 塔顶气相七组分库存 |
| `state.thermal_states.*_temperature_k` | K | 炉出口、塔顶及三侧线温度状态 |
| `state.thermal_states.preheater_duty_w` | W | 预热器热负荷状态 |
| `state.actuator_states.*_flow_kg_s` | kg/s | 流量执行机构实际状态 |
| `state.actuator_states.*_duty_w` | W | 炉、冷凝器和三路循环取热执行状态 |
| `state.sensor_states.*_temperature_k` | K | 温度传感器状态 |
| `state.sensor_states.tower_top_pressure_pa` | Pa(abs) | 塔顶绝压传感器状态 |
| `state.sensor_states.*_inventory_kg` | kg | 库存传感器状态；不是几何液位 |
| `commands` | kg/s 或 W | 11 个执行命令；单位由键后缀决定 |
| `evaluation` | 混合 | 当步流股、产品、质量代理、热量和模型诊断；全部是合成计算值 |
| `cumulative_component_in_kg` | kg | 各组分累计进入量 |
| `cumulative_component_out_kg` | kg | 各组分累计离开量 |
| `component_balance_residuals_kg` | kg | 各组分累计衡算残差 |
| `cumulative_mass_in_kg` | kg | 累计总质量进入量 |
| `cumulative_mass_out_kg` | kg | 累计总质量离开量 |
| `cumulative_salt_in_kg` / `cumulative_salt_out_kg` | kg | 累计盐进/出 |
| `mass_balance_residual_kg` | kg | 累计总质量残差 |
| `salt_balance_residual_kg` | kg | 累计盐残差 |
| `instantaneous_mass_residual_kg_s` | kg/s | 瞬时总质量残差 |
| `instantaneous_max_component_residual_kg_s` | kg/s | 瞬时最大逐组分绝对残差 |
| `instantaneous_salt_residual_kg_s` | kg/s | 瞬时盐残差 |

### 6.2 M4 闭环样本

M4 每行最外层为：

| 字段 | 单位 | 含义 |
| --- | --- | --- |
| `time_s` | s | 当前端点时刻 |
| `plant` | 见 6.1 | 完整 M3 植物样本 |
| `controls.<loop_id>` | 混合 | 七个回路各自的控制记录 |

每个控制记录包含目标/斜坡设定值、当前和决策时刻过程值、归一化误差、比例/积分/前馈项、
未约束/幅值受限/最终归一化输出、物理输出、`automatic`/`manual` 模式以及幅值、速率和饱和标志。
过程值和物理输出的单位由控制配置中的变量决定；所有带 `_normalized`、`error_normalized` 的字段无量纲。

## 7. `RunManifest`：运行清单

`manifest.json` 是运行目录最后发布的完成标志。没有它的目录是 `incomplete`，不得当作有效运行读取。

| 字段 | 类型 | 单位 | 来源与含义 |
| --- | --- | --- | --- |
| `schema_version` | string | — | 公共 schema 版本 |
| `manifest_version` | string | — | 运行清单版本 |
| `artifact_state` | string | — | 已发布清单固定为 `complete` |
| `run_id` | string | — | 必须与目录名一致 |
| `runtime_status` | string | — | 公共五态之一，复制自结果 |
| `engine_status` | string | — | 底层 M2/M3/M4/M6 引擎状态，必须与结果完全一致 |
| `domain_status` | enum | — | `not_applicable`、`passed`、`limited` 或 `rejected`；从结果诊断派生并交叉核对 |
| `request_fingerprint` | SHA-256 | — | 请求语义身份 |
| `result_fingerprint` | SHA-256 | — | 完整确定性结果身份 |
| `effective_input_fingerprint` | SHA-256 | — | 有效输入身份 |
| `installed_source_tree_sha256` | SHA-256 | — | 已安装 `petroleum_rto` Python/JSON 树按相对路径与字节计算的强制指纹 |
| `versions` | object<string, string> | — | 与结果相同的版本谱系；非失败结果必须满足第 3.1 节必要键 |
| `source_fingerprints` | object<string, SHA-256> | — | 与结果相同的源指纹；非失败结果至少覆盖 11 包内资源、M5 pipeline、M6 正式结果和三个有效对象指纹 |
| `environment` | object<string, string> | — | 分发版本、Python 实现/完整版本、操作系统、版本、机器架构及辅助 Git 字段 |
| `started_at_utc` / `finished_at_utc` | string | — | 带 UTC 偏移的开始/结束墙钟时间；不进入物理结果指纹 |
| `wall_time_s` | number | s | 从统一 API 开始到运行文件完成写出、manifest 发布前的墙钟耗时 |
| `random_seed` | integer | — | 请求随机种子 |
| `randomness_used` | boolean | — | 当前确定性运行应为 `false` |
| `synthetic` | boolean | — | 结果来源标识 |
| `data_origin` | string | — | 结果来源层 |
| `claim_scope` | string | — | 结果允许声明的范围 |
| `artifacts` | object<string, ArtifactDescriptor> | — | 所有请求、输入、结果、轨迹、事件和错误文件的描述符 |
| `manifest_fingerprint` | SHA-256 | — | 清单除自身指纹外所有字段的规范指纹 |

`environment.git_commit` 和 `environment.git_dirty` 始终存在。安装包无法可靠发现 Git 信息时，两者均写为
`unavailable`；这是显式的不可用语义，不是空字符串，也不代替强制的 `installed_source_tree_sha256`。

### 7.1 `environment`

| 字段 | 含义 |
| --- | --- |
| `distribution_version` | 已安装 `petroleum-rto-cdu-model` 分发版本；源码直接运行且未安装时为 `not-installed` |
| `git_commit` / `git_dirty` | 辅助 Git 来源；当前运行时不探测仓库，固定明确记为 `unavailable` |
| `python_implementation` | Python 实现名，如 `CPython` |
| `python_version` | Python 精简版本 |
| `python_full_version` | 完整解释器版本字符串的首行 |
| `operating_system` / `os_release` | 操作系统名与发布版本 |
| `machine` | 机器架构 |

### 7.2 `ArtifactDescriptor`

| 字段 | 类型/单位 | 含义 |
| --- | --- | --- |
| `path` | 运行目录内相对路径 | 必须留在本次运行目录内 |
| `size_bytes` | integer / byte | 文件精确字节数 |
| `sha256` | SHA-256 | 文件字节哈希 |
| `media_type` | string | `application/json` 或 `application/x-ndjson` 等 |
| `schema` | string | 对应请求、运行时、JSONL 或源资源契约 |

## 8. 包内输入资源

每个运行目录的 `inputs/` 保存以下 11 项已校验包内资源副本；点号在文件名中写成双下划线：

| 资源 ID | 类型 | 仓库来源 |
| --- | --- | --- |
| `model.base` | 基础模型 | `configs/models/cdu_mini_v0.1.0.json` |
| `catalog.components` | 七组分目录 | `configs/models/components_v0.1.0.json` |
| `case.base` | 基础案例 | `configs/cases/case_20260604.json` |
| `control.pi` | 七回路控制配置 | `configs/controllers/cdu_pi_v0.1.0.json` |
| `scenario.open_loop.baseline` | M3 基准场景 | `configs/scenarios/open_loop_baseline_v0.1.0.json` |
| `scenario.open_loop.feed_step` | M3 进料阶跃 | `configs/scenarios/open_loop_feed_step_v0.1.0.json` |
| `scenario.closed_loop.baseline` | M4 基准场景 | `configs/scenarios/closed_loop_baseline_v0.1.0.json` |
| `scenario.closed_loop.feed_step` | M4 进料设定值阶跃 | `configs/scenarios/closed_loop_feed_step_v0.1.0.json` |
| `validation.m6` | M6 配置 | `configs/validation/m6_validation_v0.1.0.json` |
| `validation.m6_manifest` | M6 正式套件清单 | `reports/modeling/m6_validation_manifest_v0.1.0.json` |
| `overlay.m5` | M5 严格运行覆盖层 | `configs/runtime/m5_runtime_overlay_v0.1.0.json` |

`overlay.m5` 的声明范围固定为 `case_alignment_only`。它不是一个完整 `ModelConfig`，只能在检查基础版本、
基线值、M5 manifest/产物哈希和对象指纹后应用四个白名单值。

运行 writer 只接受按上表顺序带齐的 11 项字节，并要求它们同时与已安装包、artifact descriptor
及 `ExecutionPayload.source_fingerprints` 一致。`read_run()` 重读时重复这一交叉核对，因此仅修改输入并重签
manifest 不能把非包内资源伪装成有效运行。

## 9. 批处理关键字段与证据

本节字段已与 `BatchRequest`、追加式事件和 `batch_manifest.json` 实现核对。

### 9.1 `BatchRequest`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schema_version` | string | 必须为 `1.0.0` |
| `request_version` | string | 必须为 `cdu-mini-batch-request-v0.1.0` |
| `batch_id` | string | 批目录名；拒绝路径和 `..` |
| `items` | array<RunRequest> | 非空有序单次请求；每项按第 2 节严格加载 |
| `batch_fingerprint` | SHA-256 | 序列化时生成；覆盖版本、`batch_id` 与有序 item 请求指纹 |

`retry_failed` 是 `execute_batch`/`resume_batch` 的执行参数，不属于批请求语义。

### 9.2 追加式批事件

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `sequence` | integer | 从0连续递增 |
| `event_type` | string | `batch_started`、`item_started`、`item_completed`、`item_exception`、`item_skipped`、`batch_resumed`或`batch_finished` |
| `item_index` | integer/null | 原批请求中的0起始顺序 |
| `attempt_number` | integer/null | 单项尝试序号，只增不覆盖 |
| `runtime_status` | string/null | 完成/跳过时的单项或批状态 |
| `message` | string | 开始、完成、跳过或异常判定的人可读证据 |

### 9.3 `batch_manifest.json`

| 字段 | 含义 |
| --- | --- |
| `schema_version` / `manifest_version` | 公共 schema 与 `cdu-mini-batch-manifest-v0.1.0` |
| `artifact_state` | 发布后固定 `complete` |
| `batch_id` / `batch_status` | 批身份与 `success|limited|failed` |
| `batch_fingerprint` | 与 `request.json` 语义指纹一致 |
| `item_count` / `completed_items` | 请求数和已有有效run manifest的数量 |
| `request_artifact` / `events_artifact` | 路径、字节数与SHA-256 |
| `items` | 每项 index、request/result/run-manifest 指纹、run 相对路径和状态 |
| `manifest_fingerprint` | 清单除自身指纹外字段的规范指纹 |

### 9.4 批目录语义

- 批事件文件是追加式证据，记录开始、单项开始/结束/失败、恢复和重试；
- 最终 batch manifest 最后发布；没有有效 manifest 的批目录不能视为完整批次；
- 恢复前先核对原批请求指纹；
- manifest 和文件哈希有效的完成项直接跳过；
- 无 manifest 的单项使用新 attempt 重跑；
- 既有失败默认保留，显式 `retry_failed` 才允许新 attempt；
- 任意恢复都不得覆盖旧成功、旧失败或旧不完整证据。
- 已完成批次恢复前，当前 `batch_manifest.json` 先移入
  `history/manifest-<manifest_fingerprint>.json`，然后才追加恢复事件；
- 如果恢复再次中断，当前清单保持缺失以表示批次未完成，历史清单仍按文件名指纹和事件前缀哈希严格校验；
- 公共 `read_batch()` 只重建带有效当前清单的完整批次，并校验请求、事件、历史清单、item 顺序、attempt 布局、run reader 结果及所有交叉指纹。

## 10. 运行目录文件

| 路径 | 内容 | 有效性规则 |
| --- | --- | --- |
| `request.json` | 严格请求 | 请求指纹必须匹配 manifest |
| `inputs/*.json` | 11 项原始包内资源副本 | 每项大小、SHA-256、包内字节和结果源指纹必须交叉一致 |
| `result.json` | 公共结果头、摘要、版本、来源和外置计数 | 结果指纹需在重建完整数组后验证 |
| `timeseries.jsonl` | 一行一个动态样本 | 行数必须匹配 `externalized.timeseries_count` |
| `events.jsonl` | 一行一个事件 | 行数必须匹配 `externalized.event_count` |
| `error.json` | `errors` 数组 | 数量必须匹配 `externalized.error_count` |
| `manifest.json` | 最后发布的运行清单 | 缺失即 `incomplete`；reader 先核对所有列出文件 |

reader 还要求目录文件集与固定合同精确相等：多出孤立文件、少任一 artifact，或遗留 `.stage`/`.tmp`
都会被拒绝。它会再核对 manifest 与 request/result 的身份、随机种子、公共/底层/适用域状态、版本、
来源指纹、有效输入、合成标志和声明范围。

## 11. 关联文档

- [M7 模型卡](M7_MODEL_CARD.md)
- [M7 使用说明](M7_USAGE.md)
- [M7 发布检查清单](M7_RELEASE_CHECKLIST.md)
- [M7 封装与可复现交付口径](06_M7_封装与可复现交付口径.md)
