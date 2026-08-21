# M5 case_20260604 数据协调与基准校正报告

_结论范围：`case_alignment_only`。本报告不构成现场验证、动态验证或可投用控制建议。_

## 证据来源分层

- `field_observations`：来自可追溯现场截图/化验目录，`synthetic=false`；只对这些记录作现场观测声明。
- `m2_latent_priors`：offgas、aqueous、brine 来自有效基线 M2 预测，`synthetic=true`，不是现场测量。
- `m2_predictions`：初始与校正后产品流均为 M2 稳态模型预测，`synthetic=true`。
- `reconciliation`：由现场边界观测与 M2 潜变量先验混合形成，不使用单一顶层 synthetic 标志。

## 结论

- 同屏五油品相对原油的原始表观偏差为 `0.8912%`，低于 `3%` 门槛；该口径不包含洗水、不凝气和水相。
- 完整十流边界协调后残差为 `5.80092e-15 kg/s`。
- 总加权目标由 `100.265202` 降至 `2.614137`，改善 `97.393%`。
- 初始/最终灵敏度秩均为 `2`，条件数分别为 `1.753401` 与 `1.936768`。
- 参数边界命中：`[]`；最终 M2 总流程与逐设备守恒通过。

## 案例操作覆盖

| 项目 | 基线 | M5有效值 | 来源 |
| --- | ---: | ---: | --- |
| 闪蒸温度 | 493.15 K | 473.75 K | TI-1012，弱时间对齐；案例输入而非独立预测 |
| 洗水比 | 0.040000 | 0.046540726 | 18.97/407.60 t/h，跨画面弱对齐；案例输入而非独立预测 |

## 净边界协调

| 物流 | 基础 | 原值/先验 kg/s | 协调值 kg/s | 调整 kg/s | pull |
| --- | --- | ---: | ---: | ---: | ---: |
| fresh_feed | 实测 | 113.138888889 | 113.119044161 | -0.019844727 | -0.017540 |
| wash_water | 实测 | 5.269444444 | 5.268368249 | -0.001076195 | -0.004085 |
| gasoline | 实测 | 17.638888889 | 17.639371241 | 0.000482352 | 0.002735 |
| kerosene | 实测 | 8.830555556 | 8.830854614 | 0.000299059 | 0.002153 |
| light_diesel | 实测 | 6.180555556 | 6.180854614 | 0.000299059 | 0.002153 |
| heavy_diesel | 实测 | 22.891666667 | 22.892479078 | 0.000812412 | 0.003549 |
| residue | 实测 | 56.588888889 | 56.593853482 | 0.004964593 | 0.008773 |
| offgas | M2先验 | 0.375830750 | 0.379706550 | 0.003875800 | 0.007752 |
| aqueous | M2先验 | 0.161361148 | 0.165236948 | 0.003875800 | 0.007752 |
| brine | M2先验 | 5.273856666 | 5.705055884 | 0.431199217 | 0.081762 |

回流和顶循仅保留作内部物流证据，未进入净边界；泵循环没有现场观测，不造假补值。截图末位只表示显示/抄录分辨率，协调权重来自独立版本化的保守工程尺度。

## 校正参数

| 参数 | 初值 K | 校正值 K | 下界 K | 上界 K |
| --- | ---: | ---: | ---: | ---: |
| `column.cut_points_k[2]` | 583.150000 | 571.704687 | 568.150000 | 598.150000 |
| `column.cut_points_k[3]` | 638.150000 | 647.954687 | 623.150000 | 653.150000 |

仅上述两个切割温度进入优化。前两个切割点、分离宽度、回流影响、六伪组分、预热/炉/闪蒸/冷凝参数、质量代理、全部动态和控制参数均冻结。

## 目标与灵敏度

| 指标 | 初始 | 校正后 |
| --- | ---: | ---: |
| 数据失配 | 100.265202 | 0.342866 |
| 正则惩罚 | 0.000000 | 2.271271 |
| 总目标 | 100.265202 | 2.614137 |
| 灵敏度条件数 | 1.753401 | 1.936768 |
| 灵敏度列余弦 | -0.500000 | -0.500000 |

有量纲和归一化 3×2 灵敏度矩阵、奇异值、秩、逐目标误差及完整守恒证据见同名 JSON 报告。

## 未进入固定校正目标的观测（完整披露）

目录共 `19` 条；仅轻柴油、重柴油、渣油 3 条进入固定校正目标，以下 `16` 条全部披露。偏差定义为模型预测减现场观测。

| ID | SI值 | 时间/offset | 来源 | 用途/状态 | 未拟合原因 | 初始M2/偏差 | 校正M2/偏差 |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| `obs-dcs-flash-bottom-temperature-ti-1013-review` | 474.25 K | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172637_40_777.jpg :: screen=flash tower and heat network; tag=TI-1013; display=201.1 degC :: sha256=334cfad53565f60fcef8a6ac4ed2e1f6f37a8f378e010b8c88331edfd93ca563 | diagnostic_reference/reference_only; catalog_exclusion=Review reference only; do not average TI-1013 with the TI-1012 flash-temperature candidate. | Review reference only; do not average TI-1013 with the TI-1012 flash-temperature candidate. | no_compatible_model_output: The current M2 evidence does not expose a compatible observable for this record. | no_compatible_model_output: The current M2 evidence does not expose a compatible observable for this record. |
| `obs-dcs-flash-feed-temperature-ti-1011-review` | 474.65 K | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172637_40_777.jpg :: screen=flash tower and heat network; tag=TI-1011; display=201.5 degC :: sha256=334cfad53565f60fcef8a6ac4ed2e1f6f37a8f378e010b8c88331edfd93ca563 | diagnostic_reference/reference_only; catalog_exclusion=Review reference only; do not average TI-1011 with the TI-1012 flash-temperature candidate. | Review reference only; do not average TI-1011 with the TI-1012 flash-temperature candidate. | no_compatible_model_output: The current M2 evidence does not expose a compatible observable for this record. | no_compatible_model_output: The current M2 evidence does not expose a compatible observable for this record. |
| `obs-dcs-flash-pressure-pi-1002` | 82000 Pa(g) | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172637_40_777.jpg :: screen=flash tower and heat network; tag=PI-1002; display=0.082 MPa gauge :: sha256=334cfad53565f60fcef8a6ac4ed2e1f6f37a8f378e010b8c88331edfd93ca563 | diagnostic_reference/reference_only; catalog_exclusion=Gauge-pressure diagnostic only; not used as a calibration target or net-boundary measurement. | Gauge-pressure diagnostic only; not used as a calibration target or net-boundary measurement. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched gauge-pressure observable; gauge-to-absolute assumptions also require field confirmation. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched gauge-pressure observable; gauge-to-absolute assumptions also require field confirmation. |
| `obs-dcs-flash-temperature-ti-1012` | 473.75 K | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172637_40_777.jpg :: screen=flash tower and heat network; tag=TI-1012; display=200.6 degC :: sha256=334cfad53565f60fcef8a6ac4ed2e1f6f37a8f378e010b8c88331edfd93ca563 | diagnostic_reference/reference_only; catalog_exclusion=Case-coverage reference for the M5 alignment configuration; not a formal calibration target. | TI-1012 defines the case flash-temperature overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: TI-1012 defines the case flash-temperature overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: TI-1012 defines the case flash-temperature overlay; it is a case input, not an independent model prediction. |
| `obs-dcs-fresh-feed-overview` | 113.138888889 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172638_41_777.jpg :: screen=CDU overview; tag=FI-1018; display=407.3 t/h :: sha256=0d9a0edcde5dce17f6468d1c98213acc6476c6047a6097d3d405043e23ef0e33 | data_coordination/candidate; catalog_exclusion=none | Observed net-boundary feed used for case data coordination; it is a case input, not an independent model prediction. | no_compatible_model_output: Observed net-boundary feed used for case data coordination; it is a case input, not an independent model prediction. | no_compatible_model_output: Observed net-boundary feed used for case data coordination; it is a case input, not an independent model prediction. |
| `obs-dcs-furnace-feed-temperature-ti-1110` | 542.95 K | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172634_37_777.jpg :: screen=furnace section; tag=TI-1110; display=269.8 degC :: sha256=79ac8b109ce2f606b6bb0158e2cd776e8a4eb46173a476821de31e131ce37b05 | diagnostic_reference/reference_only; catalog_exclusion=Case-coverage reference only; not a formal calibration target. | Case-coverage reference only; not a formal calibration target. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched temperature observable, so no defensible like-for-like prediction is available. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched temperature observable, so no defensible like-for-like prediction is available. |
| `obs-dcs-gasoline-product` | 17.6388888889 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172638_41_777.jpg :: screen=CDU overview; tag=FI-1012; display=63.5 t/h :: sha256=0d9a0edcde5dce17f6468d1c98213acc6476c6047a6097d3d405043e23ef0e33 | data_coordination/candidate; catalog_exclusion=none | Measured product flow retained for data coordination and out-of-objective diagnosis; the fixed calibration objective uses only light diesel, heavy diesel and residue. | 17.6147092 / -0.0241797118 kg/s | 17.6147092 / -0.0241797118 kg/s |
| `obs-dcs-kerosene-product` | 8.83055555556 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172638_41_777.jpg :: screen=CDU overview; tag=FI-1024; display=31.79 t/h :: sha256=0d9a0edcde5dce17f6468d1c98213acc6476c6047a6097d3d405043e23ef0e33 | data_coordination/candidate; catalog_exclusion=none | Measured product flow retained for data coordination and out-of-objective diagnosis; the fixed calibration objective uses only light diesel, heavy diesel and residue. | 8.71215625 / -0.118399309 kg/s | 8.71215625 / -0.118399309 kg/s |
| `obs-dcs-overhead-reflux-fi-1010` | 17.9722222222 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172636_39_777.jpg :: screen=atmospheric column upper section; tag=FI-1010; display=64.70 t/h :: sha256=50e70adf1070e2b2824044ca03739e247556dcf648cf536974823befc075318e | diagnostic_reference/reference_only; catalog_exclusion=Internal tower overhead reflux; excluded from the net boundary balance and retained only as a diagnostic reference. | Internal tower overhead reflux; excluded from the net boundary balance and retained only as a diagnostic reference. | no_compatible_model_output: The stored M2 evidence exposes net product flows only, not this internal circulation flow; it must not be compared with a net-boundary prediction. | no_compatible_model_output: The stored M2 evidence exposes net product flows only, not this internal circulation flow; it must not be compared with a net-boundary prediction. |
| `obs-dcs-top-circulation-fi-1003` | 89.5194444444 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172638_41_777.jpg :: screen=CDU overview; tag=FIC-1003; display=322.27 t/h :: sha256=0d9a0edcde5dce17f6468d1c98213acc6476c6047a6097d3d405043e23ef0e33 | do_not_use/excluded; catalog_exclusion=FI-1003/FIC-1003 is top pump-around circulation, not tower overhead reflux and not a net boundary stream. | FI-1003/FIC-1003 is top pump-around circulation, not tower overhead reflux and not a net boundary stream. | no_compatible_model_output: The stored M2 evidence exposes net product flows only, not this internal circulation flow; it must not be compared with a net-boundary prediction. | no_compatible_model_output: The stored M2 evidence exposes net product flows only, not this internal circulation flow; it must not be compared with a net-boundary prediction. |
| `obs-dcs-tower-top-pressure-pi-1003` | 51400 Pa(g) | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172636_39_777.jpg :: screen=atmospheric column upper section; tag=PI-1003; display=0.0514 MPa gauge :: sha256=50e70adf1070e2b2824044ca03739e247556dcf648cf536974823befc075318e | diagnostic_reference/reference_only; catalog_exclusion=Gauge-pressure diagnostic only; the model uses an explicitly converted absolute-pressure case assumption and this screenshot value is not a calibration target. | Gauge-pressure diagnostic only; the model uses an explicitly converted absolute-pressure case assumption and this screenshot value is not a calibration target. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched gauge-pressure observable; gauge-to-absolute assumptions also require field confirmation. | no_compatible_model_output: The stored M2 evidence does not expose a location-matched gauge-pressure observable; gauge-to-absolute assumptions also require field confirmation. |
| `obs-dcs-wash-ratio-feed-fi-1018` | 113.222222222 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172637_40_777.jpg :: screen=flash tower and heat network; tag=FI-1018; display=407.60 t/h :: sha256=334cfad53565f60fcef8a6ac4ed2e1f6f37a8f378e010b8c88331edfd93ca563 | diagnostic_reference/reference_only; catalog_exclusion=Reference denominator only for 18.97/407.60 case operating coverage; it is on a different screen from wash water, within an approximately one-minute capture window, and is not the primary net-boundary feed observation. | Reference denominator used only to construct the case wash-ratio overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: Reference denominator used only to construct the case wash-ratio overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: Reference denominator used only to construct the case wash-ratio overlay; it is a case input, not an independent model prediction. |
| `obs-dcs-wash-water-fic-1107` | 5.26944444444 kg/s | 2026-06-04T09:16:00+08:00 (+0 s) | base_files/original_data/延炼300万吨常压装置/300万吨DCS/微信图片_20260617172633_36_777.jpg :: screen=desalter section; tag=FIC-1107; display=18.97 t/h :: sha256=e6a9353bf5d1150a2db5186b7dfcd0e4d884ab53dbdb634ee7dbd0b2fae2d0ed | data_coordination/candidate; catalog_exclusion=none | Observed wash-water inlet used for data coordination and the case wash-ratio overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: Observed wash-water inlet used for data coordination and the case wash-ratio overlay; it is a case input, not an independent model prediction. | no_compatible_model_output: Observed wash-water inlet used for data coordination and the case wash-ratio overlay; it is a case input, not an independent model prediction. |
| `obs-lab-gasoline-final-boiling-20260604` | 440.35 K | 2026-06-04T08:00:00+08:00 (-4560 s) | base_files/original_data/延炼300万吨常压装置/化验数据/直汽--20250701-20260615.xlsx :: xl/worksheets/sheet1.xml!G349; timestamp=B349=2026-06-04 08:00:00 :: sha256=8c1a6f5600442feb9fd89a0b6b94427d79a855d3773a21bebc7bdb4b05a66522 | diagnostic_reference/reference_only; catalog_exclusion=Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. |
| `obs-lab-kerosene-final-boiling-20260604` | 503.15 K | 2026-06-04T08:00:00+08:00 (-4560 s) | base_files/original_data/延炼300万吨常压装置/化验数据/常一线-20250701-20260615.xlsx :: xl/worksheets/sheet1.xml!S254; timestamp=B254=2026-06-04 08:00:00 :: sha256=73d39407f334754c792d47538136d9ff6363006205a19605944e715233f2a6a7 | diagnostic_reference/reference_only; catalog_exclusion=Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. |
| `obs-lab-mixed-diesel-t95-20260604` | 644.15 K | 2026-06-04T08:00:00+08:00 (-4560 s) | base_files/original_data/延炼300万吨常压装置/化验数据/混合柴油-20250701-20260615.xlsx :: xl/worksheets/sheet1.xml!G348; timestamp=B348=2026-06-04 08:00:00 :: sha256=746a2b3f1ae7d255a160a366bef38c461dc488e845457998f8717c6a0cee4547 | diagnostic_reference/reference_only; catalog_exclusion=Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | Diagnostic cut-quality anchor with weak DCS time alignment; not a formal first-pass calibration target. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. | no_compatible_model_output: The current M2 evidence does not expose a matching laboratory FBP/T95 observable under the reported test method. |

## 追溯与限制

- 协调案例 SHA-256：`ab3f4e60c88b4f11d450ca7bcf0dd32c0860ba7e866eace17f0eca35a58bf2e5`
- 参数集 SHA-256：`d837f32c321c5ba7d5fbe82828b0d4b5112c926b57c01460866c93fdef66f816`
- pipeline 结果指纹：`9e7bbda6a4f534008d847c49a42b2ee6526fb7132a5ca5db52a112ccf56941b7`
- 当前只有一套弱时间对齐案例；没有独立验证案例、连续 DCS 或动态参数辨识证据。
- 缺测的 offgas、aqueous、brine 是宽松模型先验，不是现场测量。
- 结果不能外推为现场精度、跨原油能力、在线优化或控制指令。
