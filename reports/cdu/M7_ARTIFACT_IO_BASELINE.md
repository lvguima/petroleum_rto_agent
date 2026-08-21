# M7 完整 2 h Artifact I/O 内存基线

> 本报告由 `scripts/benchmark_m7_artifacts.py` 从固定 M3/M4 2 h 预设生成；机器可读结果为 `m7_artifact_io_baseline_v0.1.0.json`。

## 结论与边界

- 生成时间（UTC）：`2026-08-17T20:26:19Z`
- 两个场景各执行 2 次完整运行包写出和严格读回；每次均核对 7201 个样本、7200 s / 1 s 网格、结果指纹和 11 项输入。
- 模型 `execute` 在计时和内存测量区间外完成；写出前也已加载 11 项包内输入。
- 读回前已删除原始完整 payload、writer 记录和输入字节映射，并显式执行垃圾回收。
- 内存值为 `tracemalloc` 观测的 Python 分配峰值，不是进程 RSS，可能不含原生库分配。
- 这是当前机器的报告型基线，不设置脱离环境的跨机器硬性能阈值。

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

## 两次运行汇总

| 层级 | 预设 | 操作 | Wall 中位 / s | Wall 最大 / s | Python 峰值中位 / MiB | Python 峰值最大 / MiB |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| M3 | `open-loop-feed-step` | `write_run` | 18.743557 | 19.606566 | 2.076 | 2.077 |
| M3 | `open-loop-feed-step` | `read_run` | 23.744730 | 24.837697 | 168.740 | 168.742 |
| M4 | `closed-loop-feed-step` | `write_run` | 28.882083 | 28.967897 | 2.082 | 2.082 |
| M4 | `closed-loop-feed-step` | `read_run` | 33.742089 | 34.565862 | 224.514 | 224.514 |

## 单次测量明细

| 层级 | 重复 | 操作 | Wall / s | Python 峰值 / bytes | Python 峰值 / MiB | 结果指纹 |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| M3 | 1 | `write_run` | 17.880549 | 2177693 | 2.077 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M3 | 1 | `read_run` | 22.651762 | 176938896 | 168.742 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M3 | 2 | `write_run` | 19.606566 | 2175025 | 2.074 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M3 | 2 | `read_run` | 24.837697 | 176934984 | 168.738 | `3a2d5d0ebd5a82a25efcfe6ff99b47e903e92a1ed470e63f0c526ad1765b1feb` |
| M4 | 1 | `write_run` | 28.796270 | 2183088 | 2.082 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M4 | 1 | `read_run` | 34.565862 | 235419782 | 224.514 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M4 | 2 | `write_run` | 28.967897 | 2183217 | 2.082 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |
| M4 | 2 | `read_run` | 32.918317 | 235419343 | 224.513 | `8872d7e52afed27057d163cbf50657fbcc8f44863361b1bfff1445cd2e6fec8b` |

## 完整性

- JSON schema：`m7.artifact-io-baseline.schema-v1`
- 基线版本：`m7-artifact-io-baseline-v0.1.0`
- Runtime 版本：`cdu-mini-runtime-v0.1.0`
- 报告自身指纹：`979ed299fa9c18df8994c314d26dcc3f506ffb26b965988fb9ff94714499a8d5`

只有 JSON 去除 `report_fingerprint` 后的项目规范指纹与上述值一致时，本报告才完整。
