# CDU Mini Loop M7 使用说明

> 当前可用性：M7基础、M7.1受控输入以及M7.2稀疏JSON/单命令确认均已通过代码、完整场景与隔离wheel门禁。
> 当前候选指纹和验证明细以[实施状态](STATUS.md)为准。

## 1. 使用前必须知道的边界

这是一个离线缩减阶仿真器。普通运行和结果查看不会反复打印用途说明；模型假设、数据来源和限制统一
保存在[M7 模型卡](M7_MODEL_CARD.md)及结果元数据中。

## 2. 选择运行模板

五个预设继续作为可复现模板。M2/M3/M4 三个模型预设允许通过严格白名单创建新的自定义请求；
M6 两个便携验证预设保持冻结，不接受自定义覆盖。

| 预设 | 大约工作量 | 预期公共状态 | 适合做什么 |
| --- | --- | --- | --- |
| `steady-baseline` | 短 | `success` | 检查 M2 稳态回流/循环闭合和安装是否可用 |
| `open-loop-feed-step` | 7201 个样本 | `success` | 查看进料 `+5%` 后的 M3 开环响应 |
| `closed-loop-feed-step` | 7201 个样本 | `success` | 查看七回路对进料设定值 `+5%` 的 M4 合成闭环响应 |
| `m6-abnormal-pump-trip` | 601 个样本 | `limited` | 查看选定循环泵停运、保护动作和受限适用域证据 |
| `m6-structural-rejection` | 无求解轨迹 | `rejected` | 演示不支持汽提蒸汽输入如何在求解前被拒绝 |

`limited` 和 `rejected` 在这里可能是预设的正确结果，不应简单当作软件崩溃。始终结合事件、错误和
适用域信息解释。

## 3. 命令行使用方式

以下命令已由 M7 统一 API 实现并通过定向测试及干净wheel验收；只有在
[发布检查清单](M7_RELEASE_CHECKLIST.md)最终签署`GO`后，才可按正式交付方式使用。

### 3.1 查看预设

```powershell
cdu-mini presets
```

### 3.2 查看可调整输入

先按模板查看可用输入、单位和数值边界：

```powershell
cdu-mini inputs --preset steady-baseline
cdu-mini inputs --preset open-loop-feed-step --json
```

受控目录分成三类：

- `parameters`：进料流量、温度、压力、盐流量、七组分质量分数，案例温压，洗水比、回流比和三路循环取热；
- `overrides`：第三/第四切割温度，以及动态时间常数、停留时间和塔顶气相等效体积；
- `initial_state`：闪蒸罐、回流罐、塔底三个初始质量库存相对名义值的比例。

输入键使用面向用户的显示单位，例如 `feed.mass_flow_t_h` 为 t/h、温度键为 °C、表压力键为
MPa(g)、循环取热为 MW。预览会显示转换后的 kg/s、K、Pa(a)、W 等实际模型输入。

### 3.3 生成请求、预览并确认

先生成一个稀疏、可编辑的请求文件：

```powershell
cdu-mini template --preset open-loop-feed-step --output C:\cdu_requests\my-run.json
```

文件只保留`preset_id`和三个空覆盖分区。只填写关心的量即可；没有填写的版本、运行类型、
随机种子、工况、动态网格和事件全部继承preset。

最简单的使用方式只有一条启动命令：

```powershell
cdu-mini run --request C:\cdu_requests\my-run.json --output C:\cdu_runs
```

该命令会先列出请求值到SI值的转换、完整进料和组成、案例温压、洗水/回流/循环取热、切割点、
动态参数、三库存初值、动态网格与事件，然后询问：

`确认按以上实际输入运行？输入 y/yes/是/确认 继续 [N]:`

只有输入确认词才会启动求解；其他输入会取消，且不会创建运行目录。程序会把刚显示的预览指纹
自动传入稳定运行API，因此不需要人工复制指纹。

需要先独立复核或用于非交互脚本时，仍可显式分两步：

```powershell
cdu-mini preview --request C:\cdu_requests\my-run.json
cdu-mini run --request C:\cdu_requests\my-run.json `
  --confirm-preview <预览确认指纹> --output C:\cdu_runs
```

任何自定义请求最终都必须使用与当前解析结果完全一致的预览指纹；交互命令自动完成这一步，
显式预览后又修改请求时旧指纹会被拒绝。Python API 使用相同的显式流程：

```python
resolved = preview(request)
record = run(
    request,
    output_root=Path("runs"),
    expected_preview_fingerprint=resolved.preview_fingerprint,
)
```

### 3.4 运行最小稳态案例

```powershell
cdu-mini run --preset steady-baseline --output C:\cdu_runs
```

命令应新建一个唯一子目录，而不是把文件直接堆在 `C:\cdu_runs`。输出中的运行目录路径是随后
查看和复核的入口。

### 3.5 运行动态、闭环或异常场景

```powershell
cdu-mini run --preset open-loop-feed-step --output C:\cdu_runs
cdu-mini run --preset closed-loop-feed-step --output C:\cdu_runs
cdu-mini run --preset m6-abnormal-pump-trip --output C:\cdu_runs
cdu-mini run --preset m6-structural-rejection --output C:\cdu_runs
```

2 h 动态场景会产生较大的 `timeseries.jsonl`。运行时会逐样本写出并增量计算结果指纹；
`read_run()` 也以一次顺序适配器重建轨迹，避免先生成第二份完整原始列表。但重建后的 record 仍在
内存中保留一份完整轨迹。先用 `inspect` 看摘要；需要分析轨迹时再逐样本处理，不要用普通文本编辑器
一次加载全部样本。

### 3.6 自定义请求示例

请求文件只要求`preset_id`，其余字段均可省略。例如只调整进料、回流比和第三切割点：

```json
{
  "preset_id": "steady-baseline",
  "parameters": {
    "feed.mass_flow_t_h": 360.0,
    "operation.reflux_ratio": 0.50
  },
  "overrides": {
    "column.cut_3_temperature_c": 300.0
  }
}
```

一条命令预览、确认并运行：

```powershell
cdu-mini run --request C:\cdu_requests\my-run.json --output C:\cdu_runs
```

注意：

- `preset_id`是唯一必填字段；`run_type`、合同版本和随机种子省略时从preset继承，显式填写时必须匹配；
- `parameters`、`overrides`、`initial_state`和`metadata`都是局部覆盖，不会清空未写的preset默认值；
- 动态`scenario`也可以只写`duration_s`、`time_step_s`或`events`中的某一项，其他部分继续继承；
- 输入键必须来自对应运行类型的版本化白名单；未知键、错放分区、布尔值、非有限值、越界值和不闭合组成会在求解前拒绝；
- 动态场景可设置`duration_s`、`time_step_s`和事件；M3事件使用11个执行量名并选择`absolute`或
  `nominal_ratio`，M4事件使用`<loop_id>.setpoint_ratio`且值基准固定为`setpoint_ratio`；
- 可选`metadata`会与preset元数据合并并进入`request_fingerprint`，保留的preset元数据不能覆盖；
- `run_id` 只是不可覆盖的目录身份，不改变物理请求指纹；
- 不要在 `run_id` 或任何标识中放路径、`..`、斜杠或反斜杠。

### 3.7 查看并验证结果

```powershell
cdu-mini inspect C:\cdu_runs\<run_id>
cdu-mini inspect C:\cdu_runs\<run_id> --json
```

默认终端摘要会解析并显示关键结果：稳态产品流量/收率、性质与能量；动态温压、三库存、末端产品、
事件和守恒；闭环再增加七回路误差、输出、稳定时间和饱和情况。显示模式为：

- 默认：适合直接阅读的关键结果；
- `--quiet`：只打印状态、模板和目录；
- `--verbose`：在关键结果后追加版本和指纹；
- `--json`：输出同一解析摘要的结构化 JSON，不展开完整轨迹。

`inspect` 必须走同一个严格 reader，而不是只打开 `result.json`。reader 会先检查 `manifest.json`，再核对
固定 artifact/文件集、每个文件的路径、类型、schema、大小和 SHA-256，并要求 11 项输入与已安装包
逐字节一致。最后它交叉核对请求、结果、有效输入、公共/底层/适用域状态、版本、来源指纹和结果指纹。
`inspect --json` 输出的是紧凑摘要，不展开完整 manifest；`domain_status`、`source_fingerprints` 和完整版本/环境请从
`read_run()` 返回的 `record.manifest` 查看。

也可使用模块入口：

```powershell
python -m petroleum_rto.cdu.runtime presets
python -m petroleum_rto.cdu.runtime inspect C:\cdu_runs\<run_id>
```

### 3.8 CLI 退出码

| 命令结果 | 退出码 | 语义 |
| --- | --- | --- |
| `presets` 正常列出，或 `inspect` 严格验证成功 | `0` | 命令成功 |
| `run` 返回 `success`、`limited` 或预期的 `rejected` | `0` | 已完整发布可解释的公共结果，但必须保留原状态 |
| `run` 返回 `not_converged` 或 `failed` | `1` | 运行已发布失败证据 |
| `batch` 返回 `success` 或 `limited` | `0` | 批次完成；`limited` 可包含受限或预期拒绝项 |
| `batch` 返回 `failed` | `1` | 至少一项失败或没有完整证据 |
| 命令参数、请求、reader 验证、路径或文件错误 | `2` | 用法或证据验证错误；详细信息写入标准错误 |

`rejected=0` 只表示稳定入口已按合同完整保存求解前拒绝证据，不表示该输入被模型支持。

## 4. Python API

最终稳定外观为：

```python
from pathlib import Path

from petroleum_rto.cdu.runtime import (
    preview,
    read_run,
    run,
    runtime_request_from_mapping,
)

request = runtime_request_from_mapping(
    {
        "preset_id": "steady-baseline",
        "parameters": {"feed.mass_flow_t_h": 360.0},
    }
)
resolved = preview(request)
written = run(
    request,
    output_root=Path(r"C:\cdu_runs"),
    expected_preview_fingerprint=resolved.preview_fingerprint,
)
record = read_run(written.run_dir)

print(record.payload.runtime_status)
print(record.payload.result_fingerprint)
print(record.manifest.installed_source_tree_sha256)
```

`run()` 的 `output_root` 必须是 `pathlib.Path`。返回值包含：

- `run_dir`：实际运行目录；
- `request`：严格请求；
- `payload`：完整公共结果；
- `manifest`：已验证运行清单。

动态样本可通过 `record.iter_samples()` 迭代。对外部已保存结果，优先先 `read_run()`，不要绕过 reader
直接信任某个 JSON 文件。

顶层 `petroleum_rto.cdu.runtime` 导出 `load_preset`、`preview`、`run`、`read_run`、
`list_runtime_input_specs`、`runtime_request_template`、`runtime_request_from_mapping`、
`RuntimeInputEvent`、`RuntimeScenarioRequest`、
`BatchRequest`、`BatchRecord`、`execute_batch`、`read_batch` 和 `resume_batch`；下游不应依赖带下划线的内部函数。

## 5. 如何读一个运行目录

一个完整目录至少包含：

```text
<run_id>/
├── request.json
├── inputs/
│   └── ... 11 项包内输入副本
├── result.json
├── timeseries.jsonl
├── events.jsonl
├── error.json
└── manifest.json
```

推荐检查顺序：

1. 确认 `manifest.json` 存在；不存在即为 `incomplete`，不要人工补建。
2. 通过 `inspect` 或 `read_run()` 校验，不要只看目录中文件是否齐全。
3. 查看结果的 `runtime_status`、`engine_status`、`synthetic`、`data_origin` 和 `claim_scope`，并从 manifest 查看 `domain_status`。
4. 对 `failed`、`not_converged`、`rejected` 查看 `error.json` 和 `events.jsonl`。
5. 对动态结果核对 `duration_s`、`time_step_s`、样本数和守恒诊断。
6. 保存请求、结果、有效输入、`source_fingerprints` 和源码树指纹，引用结果时一并报告。

`manifest.json` 是最后发布的完成标志。任何列出的文件只要被改动一个字节、大小变化、缺失或移出
运行目录，reader 都应拒绝该目录；多出孤立文件或遗留 `.stage`/`.tmp` 也会被拒绝。即使人工重签
manifest，请求、结果、11 输入、状态、版本和来源指纹之间任一矛盾仍会被拒绝。不要“修复”旧目录；
保留它作审计证据并创建一个新运行。

manifest 中的 `versions.m5_overlay_version` 明确记录 M5 运行覆盖版本。`environment.git_commit` 和
`environment.git_dirty` 也始终存在；安装环境无法可靠获取 Git 信息时，值为 `unavailable`。这两个
辅助字段不替代 `installed_source_tree_sha256`。

## 6. 如何解释状态

| 状态 | 用户动作 |
| --- | --- |
| `success` | 可在模型卡允许范围内解释；仍需保留合成来源和适用边界 |
| `limited` | 只作受限/代理域工程验证；报告中必须显式保留 `limited` |
| `rejected` | 查看适用域/请求预检原因；当前固定预设中的结构拒绝可为预期结果 |
| `not_converged` | 查看失败阶段、迭代诊断和最后有效证据；另建运行，不覆盖旧目录 |
| `failed` | 查看结构化错误与事件；先排除资源、守恒、契约和环境问题 |

即使状态是 `success`，也不表示产品质量、现场液位、阀位、动态时间常数或安全保护已经现场验证。

## 7. 批处理与恢复

M7 的批处理命令为：

```powershell
cdu-mini batch --request C:\cdu_requests\batch.json --output C:\cdu_batches
cdu-mini batch --resume C:\cdu_batches\<batch_id>
cdu-mini batch --resume C:\cdu_batches\<batch_id> --retry-failed
```

批请求严格字段如下；`batch_fingerprint`由程序生成，手写请求可省略：

```json
{
  "schema_version": "1.0.0",
  "request_version": "cdu-mini-batch-request-v0.1.0",
  "batch_id": "engineering-review-001",
  "items": [
    {
      "schema_version": "1.0.0",
      "request_version": "cdu-mini-run-request-v0.1.0",
      "preset_id": "steady-baseline",
      "run_type": "steady_recycle",
      "random_seed": 0,
      "parameters": {},
      "overrides": {},
      "metadata": {"purpose": "offline_engineering_review"}
    },
    {
      "schema_version": "1.0.0",
      "request_version": "cdu-mini-run-request-v0.1.0",
      "preset_id": "m6-structural-rejection",
      "run_type": "validation_scenario",
      "random_seed": 0,
      "parameters": {},
      "overrides": {},
      "metadata": {"purpose": "offline_engineering_review"}
    }
  ]
}
```

批请求按固定顺序包含多个单次请求，顺序与每项语义指纹共同进入
`batch_fingerprint`。`--retry-failed`是本次恢复的执行选项，不是批请求的物理字段。
设计原则如下：

- 一个单项失败不终止后续项；
- 每个单项仍生成自己的不可覆盖运行目录；
- 批事件采用追加式记录，最终 batch manifest 最后发布；
- 恢复前必须核对原批请求指纹，不能用另一份请求接管旧批目录；
- 已有有效 manifest 的完成项直接跳过；
- 无 manifest 的不完整项创建新 attempt 重跑；
- 既有失败默认保留，只有显式 `--retry-failed` 才创建新 attempt；
- 重试永远不能覆盖旧成功、旧失败或旧不完整证据。

对一个已完成批次执行 `resume_batch()` 时，程序会先将当前 `batch_manifest.json` 移入
`history/manifest-<manifest_fingerprint>.json`，再追加恢复事件。如果在此后再次中断，当前清单缺失会
明确表示“未完成”，旧清单仍保存为可校验历史，下一次恢复可继续。

批目录布局为：

```text
<batch_id>/
├── request.json
├── events.jsonl
├── history/
│   └── manifest-<fingerprint>.json
├── items/<index>/attempt-<number>/<run_id>/
└── batch_manifest.json
```

`batch_manifest.json` 是当前批次的最后完成标志；`history/` 只在发生完成后恢复时出现。可通过公共 API
严格读取已完成批次：

```python
from pathlib import Path

from petroleum_rto.cdu.runtime import read_batch, resume_batch

batch_dir = Path(r"C:\cdu_batches\engineering-review-001")
record = read_batch(batch_dir)
print(record.batch_status, record.completed_items)

record = resume_batch(batch_dir, retry_failed=False)  # 仅在需要继续时调用
```

`read_batch()` 只接受带有效当前 `batch_manifest.json` 的完整批次。若恢复在历史化当前清单后中断，
请再次调用 `resume_batch()`，不要人工拷贝历史清单充当当前清单。

## 8. 从 wheel 做仓库外干净环境验收

这一流程已经在当前最终候选上通过，但若后续修改任何安装源码或包内资源，必须从头重建并复验。
它要求使用 Python `>=3.12,<3.13`，并在仓库外创建全新环境，避免源码目录和开发依赖掩盖安装包缺文件的问题。

原M7基础候选的正式性能证据分别保存在[M7 执行性能基线](../../reports/modeling/M7_PERFORMANCE_BASELINE.md)
和[M7 Artifact I/O 基线](../../reports/modeling/M7_ARTIFACT_IO_BASELINE.md)。前者对 M2/M3/M4/M6
四个代表预设各重复 3 次；后者对 M3/M4 完整 2 h 运行包各重复 2 次写出和严格读回。Artifact writer
的 Python 分配峰值约为 `2.1 MiB`；reader 在原始 payload 已释放后只重建最终一份轨迹，M3/M4 峰值
分别约为 `168.7/224.5 MiB`。这些报告绑定原M7源码树`1003a7b2…`，不是M7.1候选的重新计时；
M7.1另以两条完整2 h自定义M3/M4运行证明功能和规模保持可执行。所有这些数值都只是当前机器记录。

### 8.1 从隔离的源码副本构建 sdist 和唯一 wheel

不得从仓库根目录直接执行`pip wheel .`，也不得把新候选写入或从glob选择旧`build/`、`dist/`或
`src/*.egg-info`。当前旧缓存形成于M7完成前，不能证明包含最终runtime。

正式门禁须在全新临时目录中完成：

1. 建立allowlist源码副本，只复制`pyproject.toml`以及`src/petroleum_rto`下的`.py`、`.json`文件；
2. 在副本中调用`setuptools.build_meta.build_sdist(<empty-sdist-dir>)`，并要求目录中恰好一个`.tar.gz`；
3. 离开仓库和源码副本，由该sdist执行
   `pip wheel --no-index --no-cache-dir --no-deps --no-build-isolation --wheel-dir <empty-wheel-dir> <sdist>`；
4. 要求wheel目录恰好一个`.whl`，先测试并解压，再安装；
5. 逐项比较allowlist副本、解压sdist和解压wheel中的Python/JSON路径与字节，核对源码树SHA、
   `runtime/batch.py`、11项资源、console入口、METADATA及禁止文件全集。

本项目环境未安装`build`分发，因此正式流程不依赖`python -m build`；setuptools和pip均以
`--no-build-isolation/--no-index/--no-deps`离线使用。记录唯一sdist和wheel的文件名、大小与SHA-256，
不要记录旧`dist/`中的预验包。

M7.2最终发布记录：sdist为`petroleum_rto_cdu_model-0.1.0.tar.gz`，282639 bytes，SHA-256
`ab769ba17f3e07af743c319cac87feec15617c61d23af3ec89f17bcb9680d149`；wheel为
`petroleum_rto_cdu_model-0.1.0-py3-none-any.whl`，326474 bytes，SHA-256
`eaf14dff98d0afee6a0fd988c0a08bc8e1338dbe37f631edff1288654224d680`。安装后源码树SHA-256为
`fffc3494464ae90037f7d5d8a0a8f2ee832918fe56aff84473b9cc140a3b56c6`。
仓库内持久化副本位于[M7.2 wheel](../../dist/m7-2-final/petroleum_rto_cdu_model-0.1.0-py3-none-any.whl)、
[M7.2 sdist](../../dist/m7-2-final/petroleum_rto_cdu_model-0.1.0.tar.gz)及
[M7.2 SHA-256清单](../../dist/m7-2-final/SHA256SUMS.txt)。`dist/`根目录、`dist/m7-final/`和
`dist/m7-1-final/`分别是旧预验、M7基础及M7.1包，不能代替M7.2候选。

### 8.2 在仓库外创建 Python 3.12 环境

解释器必须是Python`>=3.12,<3.13`。当前机器可由已验证的仓库`.venv` Python 3.12.13创建仓库外
clean venv；这证明wheel不依赖源码路径，但不应把它描述为另一套独立系统Python安装：

```powershell
D:\pyproject\petroleum_rto_agent\.venv\python.exe -m venv C:\temp\cdu-mini-clean
$env:PYTHONPATH = $null
$env:PYTHONHOME = $null
C:\temp\cdu-mini-clean\Scripts\python.exe -m pip install --no-index --no-cache-dir --no-deps C:\temp\cdu-wheel-gate\wheel\petroleum_rto_cdu_model-0.1.0-py3-none-any.whl
```

然后离开仓库目录：

```powershell
Set-Location C:\temp
C:\temp\cdu-mini-clean\Scripts\cdu-mini.exe presets
C:\temp\cdu-mini-clean\Scripts\cdu-mini.exe run --preset steady-baseline --output C:\temp\cdu-clean-runs
```

最后使用同一干净环境的 Python 调用 `read_run()`，验证刚生成目录的 manifest、11 项输入资源和结果指纹。

只有以下条件同时满足，才可勾选干净环境门禁：

- wheel 中包含所有 Python 模块和 11 项 JSON 资源；
- wheel是由本轮唯一sdist构建，且源码树SHA与allowlist副本、解压sdist一致；
- 不依赖当前工作目录、`PYTHONPATH` 或仓库配置文件；
- `petroleum_rto.__file__`位于clean venv的`site-packages`，仓库根和`src`不在`sys.path`；基础解释器
  标准库仍来自上文已披露的仓库`.venv`，但没有editable源码泄漏；
- `cdu-mini` 与 `python -m petroleum_rto.cdu.runtime` 可启动；
- `inputs/template/preview`、单命令交互确认运行以及带预览指纹的非交互`run`可执行；
- 360 t/h与第三切割点300 °C的自定义稳态成功写出 manifest-last 目录；
- `read_run()` 在仓库外成功重载；
- 未安装未声明依赖，`pip check` 无问题。

## 9. 复现与分享结果

分享结果时，至少提供：

- 完整、未修改的运行目录；
- `preset_id`、`run_type`、`runtime_status`；
- `request_fingerprint`、`effective_input_fingerprint`、`result_fingerprint`；
- `engine_status`、`domain_status` 和完整 `source_fingerprints`；
- `installed_source_tree_sha256` 和 `versions`（含 `m5_overlay_version`）；
- `synthetic`、`data_origin`、`claim_scope`；
- 本模型卡版本及任何 `limited`、`rejected` 或失败说明。

不要只分享截图或从轨迹抄出的单个数值。时间、`run_id` 和输出位置可以不同，但相同语义请求、有效
输入和已安装源码应得到相同确定性结果指纹；若不同，应先按篡改、版本或输入漂移问题调查。

## 10. 常见问题

### 没有 `manifest.json`

这是不完整运行。不要手工生成清单，也不要把它和成功结果合并；保留目录，并通过新 run/attempt 重跑。

### reader 报哈希或大小不匹配

至少一个文件已改变、截断或被替换。停止使用该结果，保留原目录用于审计，从原请求新建运行。

### `m6-abnormal-pump-trip` 返回 `limited`

这是该受限异常场景的预期语义，表示工程验证通过但适用范围受限。它不是现场 SIS 通过证明。

### `m6-structural-rejection` 返回 `rejected`

这是预期的求解前拒绝；该模型没有支持汽提蒸汽动态输入，不能通过增加重试次数绕过。

### 可以编辑 `inputs/` 后重算吗

不能编辑既有运行目录里的`inputs/`；那是该次运行的只读证据，修改后严格 reader 会拒绝整个目录。
要改变仿真输入，请从`cdu-mini template`创建新的请求，只修改`cdu-mini inputs`列出的白名单字段，
再用一条`cdu-mini run --request ...`命令预览、确认并生成新的运行目录。

### 自定义输入可以直接放进普通批处理吗

不能由默认批处理入口静默确认。当前自定义单次请求必须由调用者显式提交预览指纹；默认批处理不会
替用户确认自定义输入，因此会把未确认项记录为失败。固定预设仍可正常批量执行。

## 11. 关联文档

- [M7 模型卡](M7_MODEL_CARD.md)
- [M7 数据字典](M7_DATA_DICTIONARY.md)
- [M7 发布检查清单](M7_RELEASE_CHECKLIST.md)
- [M7 封装与可复现交付口径](06_M7_封装与可复现交付口径.md)
