# M6 工程验证审计报告

> 本报告只声明合成工程验证通过，不构成现场动态精度、跨原油能力或SIS验证。

## 发布结论

- 验证状态：`success`
- 完成门禁：`true`
- 结果指纹：`76c8e86262f96e517c76083677500621bcf777e3e7d2a6e3dd84b4a94e3370ba`
- 声明范围：`engineering_validation_only`
- 数据来源：`M6_synthetic_validation`

## 完成门禁

| 门禁 | 结果 |
| --- | --- |
| `scenario_matrix` | 通过 |
| `applicability_domain` | 通过 |
| `uncertainty_propagation` | 通过 |
| `protection_logic` | 通过 |
| `conservation` | 通过 |
| `deterministic_reproduction` | 通过 |

## 场景矩阵

| 场景 | 执行层 | 实际/预期 | 验证结论 | 求解器 | 适用域 | 保护事件 |
| --- | --- | --- | --- | --- | --- | ---: |
| `limited_condenser_cooling_decline_10` | `M3_open_loop` | `limited` / `limited` | `passed` | 已调用 | `limited` | 0 |
| `limited_feed_drop_30` | `M3_open_loop` | `limited` / `limited` | `passed` | 已调用 | `limited` | 2 |
| `limited_furnace_duty_decline_10` | `M3_open_loop` | `limited` / `limited` | `passed` | 已调用 | `limited` | 0 |
| `limited_furnace_fuel_saturation` | `M3_open_loop` | `limited` / `limited` | `passed` | 已调用 | `limited` | 0 |
| `limited_furnace_temperature_sensor_bias` | `M6_supervisory` | `limited` / `limited` | `passed` | 未调用 | `limited` | 0 |
| `limited_furnace_temperature_sensor_freeze` | `M6_supervisory` | `limited` / `limited` | `passed` | 未调用 | `limited` | 2 |
| `limited_pump_around_1_trip` | `M3_open_loop` | `limited` / `limited` | `passed` | 已调用 | `limited` | 2 |
| `limited_residue_draw_valve_stuck` | `M6_supervisory` | `limited` / `limited` | `passed` | 未调用 | `limited` | 0 |
| `normal_crude_heavier_2` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_crude_lighter_2` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_feed_minus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_feed_plus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_feed_temperature_minus_5k` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_feed_temperature_plus_5k` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_m4_feed_setpoint_minus_5` | `M4_closed_loop` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_m4_feed_setpoint_plus_5` | `M4_closed_loop` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_pump_around_1_plus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_pump_around_2_plus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_pump_around_3_plus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `normal_reflux_plus_5` | `M2_steady` | `passed` / `passed` | `passed` | 已调用 | `passed` | 0 |
| `rejected_dynamic_feed_water_pulse` | `structural_rejection` | `rejected` / `rejected` | `passed` | 未调用 | `rejected` | 0 |
| `rejected_stripping_steam_request` | `structural_rejection` | `rejected` / `rejected` | `passed` | 未调用 | `rejected` | 0 |

## 灵敏度与不确定度

| Plan | 输入 | 输出 | 灵敏度 | 区间语义 | 来源 | 未量化来源 |
| --- | --- | --- | --- | --- | --- | --- |
| `m6_steady_local_envelope_v0.1.0` | `feed_load_ratio, crude_lightness_shift_fraction, feed_temperature_offset_k, flash_temperature_offset_k, wash_water_ratio_factor, column_cut_3_offset_k, column_cut_4_offset_k` | `product_flow_kg_s.gasoline, product_flow_kg_s.kerosene, product_flow_kg_s.light_diesel, product_flow_kg_s.heavy_diesel, product_flow_kg_s.residue, product_yield_fraction.gasoline, product_yield_fraction.kerosene, product_yield_fraction.light_diesel, product_yield_fraction.heavy_diesel, product_yield_fraction.residue, energy.furnace_fuel_duty_w, energy.actual_recovered_duty_w, energy.potential_recovered_duty_w, energy.pump_around_removed_duty_w, quality.gasoline.t90_k_proxy, quality.light_diesel.t90_k_proxy, quality.heavy_diesel.t90_k_proxy, quality.residue.density_kg_m3_proxy` | `success` | `deterministic_engineering_envelope` | `M2_steady_model_prediction, M6_synthetic_validation` | `crude_structure_error, field_time_alignment_error, fuel_heating_value_proxy, nonlinear_interactions, parameter_correlation` |
| `m6_dynamic_lag_envelope_v0.1.0` | `actuator_time_constant_ratio, sensor_time_constant_ratio` | `maximum.furnace_outlet_temperature_k, maximum.tower_top_pressure_pa, maximum_abs_inventory_deviation.flash_drum, maximum_abs_inventory_deviation.reflux_drum, maximum_abs_inventory_deviation.tower_bottom, final_inventory_ratio.flash_drum, final_inventory_ratio.reflux_drum, final_inventory_ratio.tower_bottom, final.tower_top_pressure_pa, tracking_iae.actuator.fresh_feed_flow_kg_s, tracking_iae.sensor.flash_drum_inventory_kg, response_t63_s.actuator.fresh_feed_flow_kg_s, response_t63_s.sensor.flash_drum_inventory_kg` | `success` | `deterministic_engineering_envelope` | `M3_open_loop_simulation, M6_synthetic_validation` | `actuator_channel_correlation, feed_step_initial_condition_dependence, field_dynamic_identification_gap, nonlinear_interactions, sensor_channel_correlation` |

## 保护事件与控制器跟踪

| 规则 | 帧数 | 事件数 | 触发时刻 | 跟踪证据 |
| --- | ---: | ---: | --- | --- |
| `low_furnace_feed` | 7 | 6 | `11.0` | `low_furnace_feed.furnace_temperature` |
| `high_furnace_temperature` | 7 | 6 | `11.0` | `high_furnace_temperature.furnace_temperature` |
| `high_tower_top_pressure` | 7 | 6 | `16.0` | `high_tower_top_pressure.top_pressure` |
| `pump_around_1_invalid` | 7 | 6 | `3.0` | `pump_around_1_invalid.feed_flow, pump_around_1_invalid.furnace_temperature` |
| `furnace_temperature_measurement_invalid` | 7 | 6 | `6.0` | `furnace_temperature_measurement_invalid.furnace_temperature` |
| `high_flash_inventory` | 5 | 4 | `31.0` | `high_flash_inventory.flash_inventory` |
| `low_flash_inventory` | 5 | 4 | `31.0` | `low_flash_inventory.flash_inventory` |
| `high_reflux_inventory` | 5 | 4 | `31.0` | `high_reflux_inventory.reflux_inventory` |
| `low_reflux_inventory` | 5 | 4 | `31.0` | `low_reflux_inventory.reflux_inventory` |
| `high_bottom_inventory` | 5 | 4 | `31.0` | `high_bottom_inventory.bottom_inventory` |
| `low_bottom_inventory` | 5 | 4 | `31.0` | `low_bottom_inventory.bottom_inventory` |

## 来源边界

| 来源 | 分类 |
| --- | --- |
| `source_traced_field_observation_catalog` | `source_evidence` |
| `M2_steady_model_prediction` | `synthetic_prediction` |
| `M3_open_loop_simulation` | `synthetic_simulation` |
| `M4_closed_loop_simulation` | `synthetic_simulation` |
| `M6_synthetic_validation` | `synthetic_validation` |

## 限制与禁止声明

- `engineering_validation_only`：全部模型数值均为合成工程验证证据，不构成现场动态精度或跨原油验证。
- `local_first_order_envelope`：灵敏度和不确定度仅为固定参考点的一阶工程包络，不是概率置信区间。
- `single_case_m5_basis`：有效基准继承M5单案例、弱时间对齐和六伪组分工程初值限制。
- `synthetic_protection_not_sis`：保护状态机只验证模型行为，不代表现场SIS设定、硬件或安全认证。
- `mixed_source_boundary`：现场观测目录仅作来源证据；M2、M3、M4和M6结果仍为模型预测或合成仿真。
- `limited_scenarios`：以下场景只形成受限工程验证结论。（`limited_condenser_cooling_decline_10`、`limited_feed_drop_30`、`limited_furnace_duty_decline_10`、`limited_furnace_fuel_saturation`、`limited_furnace_temperature_sensor_bias`、`limited_furnace_temperature_sensor_freeze`、`limited_pump_around_1_trip`、`limited_residue_draw_valve_stuck`）
- `structurally_rejected_scenarios`：以下请求在求解前按模型结构明确拒绝。（`rejected_dynamic_feed_water_pulse`、`rejected_stripping_steam_request`）

## 文件交叉引用

- 完整证据 SHA-256：`e27624ff14a294d20e8bd3446ee2aaf0f95b8cf0d4623c939e5798153f146e96`
- 机器报告 SHA-256：`cb5678451613b8c92edef3ccccad360c0c89311606cd2a5aaaff891894ed150a`
- 四文件套件由manifest最后发布；manifest本身不记录当前时间。
