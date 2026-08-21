# M7 正式性能基线

> 本报告由 `scripts/benchmark_m7.py` 从统一运行入口生成；对应机器可读结果为 `m7_performance_baseline_v0.1.0.json`。

## 结论与测量边界

- 生成时间（UTC）：`2026-08-17T20:08:57Z`
- 基线状态：`success`；4 个代表场景均完成 3 次严格校验运行。
- 计时区间仅为 `execute(load_preset(preset_id))`，不包含运行包 JSONL/JSON/manifest 写盘、报告序列化或结果指纹计算。
- 内存值是 `tracemalloc` 观测到的 Python 分配峰值，不是进程 RSS，可能不包含原生库分配。
- 这是当前机器的报告型基线，不设置脱离环境的跨机器硬性能阈值。
- M6 项是包内选定的泵循环一停运便携复现场景，不等同于重跑完整 source-closed M6 矩阵。

## 运行环境

| 字段 | 值 |
| --- | --- |
| `distribution_version` | `0.1.0` |
| `git_commit` | `unavailable` |
| `git_dirty` | `unavailable` |
| `machine` | `unknown` |
| `operating_system` | `Windows` |
| `os_release` | `10` |
| `python_full_version` | `3.12.13 | packaged by Anaconda, Inc. | (main, Jul  9 2026, 14:26:47) [MSC v.1942 64 bit (AMD64)]` |
| `python_implementation` | `CPython` |
| `python_version` | `3.12.13` |
| `installed_source_tree_sha256` | `1003a7b2893f90cf823829d273a5e5ea9681d79199a506ac2569fc380767c867` |
| `environment_fingerprint` | `17acec035d263ee2adf2be011d8fff2019230a33fde07b27e4c833b7923e722d` |

## 三次运行汇总

| 层级 | 预设 | 状态 | 样本数 | 步长 / s | Wall 中位 / s | Wall 最大 / s | Python 峰值中位 / MiB | Python 峰值最大 / MiB |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| M2 | `steady-baseline` | `success` | 0 | — | 0.170119 | 0.178343 | 0.276 | 0.352 |
| M3 | `open-loop-feed-step` | `success` | 7201 | 1.0 | 136.638510 | 138.141520 | 196.117 | 196.119 |
| M4 | `closed-loop-feed-step` | `success` | 7201 | 1.0 | 143.770409 | 143.987700 | 245.963 | 245.968 |
| M6 | `m6-abnormal-pump-trip` | `limited` | 601 | 1.0 | 22.626794 | 22.708880 | 26.585 | 26.589 |

## 单次测量明细

| 层级 | 重复 | Wall / s | Python 峰值 / bytes | Python 峰值 / MiB | 结果指纹 |
| --- | ---: | ---: | ---: | ---: | --- |
| M2 | 1 | 0.178343 | 369268 | 0.352 | `4f2ac7766bad361f2981c2a9e795791e23f7857f2a82593330436f31883b1b2d` |
| M2 | 2 | 0.162096 | 289347 | 0.276 | `4f2ac7766bad361f2981c2a9e795791e23f7857f2a82593330436f31883b1b2d` |
| M2 | 3 | 0.170119 | 284584 | 0.271 | `4f2ac7766bad361f2981c2a9e795791e23f7857f2a82593330436f31883b1b2d` |
| M3 | 1 | 135.604205 | 205645619 | 196.119 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M3 | 2 | 138.141520 | 205643742 | 196.117 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M3 | 3 | 136.638510 | 205642592 | 196.116 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M4 | 1 | 143.226564 | 257916085 | 245.968 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M4 | 2 | 143.987700 | 257911302 | 245.963 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M4 | 3 | 143.770409 | 257910233 | 245.962 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M6 | 1 | 22.708880 | 27880761 | 26.589 | `ddcbd7b9d1e29dd786aa8f45888b897efcadc4bf259b4a8d14d59c6dfe536dd4` |
| M6 | 2 | 22.470261 | 27876658 | 26.585 | `ddcbd7b9d1e29dd786aa8f45888b897efcadc4bf259b4a8d14d59c6dfe536dd4` |
| M6 | 3 | 22.626794 | 27876589 | 26.585 | `ddcbd7b9d1e29dd786aa8f45888b897efcadc4bf259b4a8d14d59c6dfe536dd4` |

## 完整性

- JSON schema：`m7.performance-baseline.schema-v1`
- 基线版本：`m7-performance-baseline-v0.1.0`
- Runtime 版本：`cdu-mini-runtime-v0.1.0`
- 报告自身指纹：`714194bf9d203be6956758b65c30f6482e5342351be6d571ae7f6bb7a51c4f7c`

只有 JSON 去除 `report_fingerprint` 字段后的项目规范指纹与上述值一致时，本报告才完整。
