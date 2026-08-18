# CDU Mini Loop M7 发布检查清单

> 当前结论：**M7基础发布、M7.1和M7.2均为GO。**
> M7.2候选固定为`dist/m7-2-final/`，旧同名wheel不能替代。
> 清单快照日期为 2026-08-18；进度的唯一事实来源是[当前实施状态](STATUS.md)。

## 1. 使用规则

- `[x]`：已经有可复核的仓库文件和验证结果；
- `[ ]`：未执行、未完成、结果未知或需要在最终代码上重跑；
- 任何失败项不得通过删除失败记录改成通过；
- wheel、干净环境、长场景、批恢复和全仓库门禁必须基于同一最终候选代码；
- 本清单全部强制项勾选、状态文档同步且独立终验无阻塞后，才可把 M7 标记为完成。

## 2. 前置里程碑与范围

- [x] M0～M6 已按实施方案完成并在状态文档中保存验证证据。
- [x] M7 正式口径已冻结四类运行、五态结果、包内资源、manifest-last、批恢复和 wheel 门禁。
- [x] 当前授权未扩展到策略库、代理模型、优化器、智能体、DCS/SIS/LIMS 接口或现场控制。
- [x] 文档明确 M5 只有 `case_alignment_only`，M6 保护不是 SIS。
- [x] 文档明确六伪组分低可信、无现场动态验证、库存代理和工程区间非统计置信区间。
- [x] 文档禁止现场闭环、SIS、安全完整性证明、产品质量保证、操作许可和在线优化。
- [x] M5源验证回归与独立终验确认`base_files/`未修改；runtime源码扫描未见网络、DCS/SIS/LIMS、优化器或智能体等超授权实现。

## 3. 稳定请求、结果与预设契约

- [x] `RunRequest`、`EventRecord`、`ErrorRecord`、`ExecutionPayload` 为严格不可变契约。
- [x] 运行类型固定为 `steady_recycle`、`open_loop_dynamic`、`closed_loop_dynamic`、
  `validation_scenario`。
- [x] 公共状态固定为 `success`、`limited`、`rejected`、`not_converged`、`failed`。
- [x] 请求指纹排除 `run_id` 和墙钟时间，并包含语义 metadata。
- [x] 成功/受限结果不得携带错误；拒绝/未收敛/失败必须保存错误、阶段和原因。
- [x] 五个固定预设已注册；未知或路径式 ID 不通过目录扫描兜底。
- [x] M2/M3/M4只接受版本化受控输入白名单；M6便携预设和未知/错放/越界输入在求解前拒绝。
- [x] 终审返修后全部 M7 单元测试 122 项通过，覆盖契约、资源、执行、artifact、批处理、CLI 和性能报告契约。
- [x] `success`/`limited` 不能配底层失败状态；完整结果指纹以逐样本增量方式计算且与规范 JSON 指纹等价。
- [x] 在最终候选代码上重跑请求/结果/事件/错误的未知字段、布尔数值、非有限值、非法枚举、
  指纹伪造和 path traversal 全部反例。

### 3.1 M7.1受控自定义接口

- [x] `RunRequest`已加入可选强类型`scenario`和`initial_state`，默认空请求保持原预设请求/有效输入指纹。
- [x] 参数目录冻结输入ID、分区、显示单位、SI单位、数值范围和适用运行类型；组成闭合及模型交叉约束有反例。
- [x] 自定义稳态、M3事件/库存初值及M4自定义工况/库存初值已经走统一executor和严格artifact reader。
- [x] `preview()`不调用M2/M3/M4求解器，返回解析后的有效输入与确认指纹；自定义`run()`缺失或使用过期指纹会拒绝。
- [x] CLI提供`inputs`、`template`、`preview`、`--confirm-preview`以及默认/quiet/verbose/JSON四类结果显示。
- [x] 默认批处理不会静默替用户确认自定义输入；未显式确认的自定义item保留失败证据。
- [x] 最终增强代码上的147项M7单测、5项M7集成、相关ruff、format和strict mypy已通过。
- [x] 最终增强源码634项全仓库pytest、全仓库ruff、77文件strict mypy和pip check已通过。
- [x] M3/M4完整2小时自定义场景各7201样本，预览确认、结果摘要、写出和严格重载均已验证。
- [x] 新sdist/wheel和仓库外Python 3.12安装已验证自定义inputs/preview/confirm run/inspect及公共导出。

### 3.2 M7.2稀疏请求与单命令确认

- [x] 用户JSON只强制`preset_id`，省略字段继承preset；局部map和动态scenario子字段按严格规则合并。
- [x] 稀疏文件仍归一化为完整`RunRequest`后进入请求指纹、artifact和严格reader。
- [x] 普通`run --request`先打印完整实际输入，确认后自动提交同一预览指纹；取消不创建运行目录。
- [x] `--json/--quiet`及Python API等非交互路径仍要求显式指纹，不静默确认。
- [x] 59项定向测试、全部M7单元154项、五入口集成5项及相关ruff/strict mypy通过。
- [x] M7.2最终全仓库640项pytest、ruff、77文件strict mypy和pip check通过。
- [x] M7.2隔离sdist/wheel及仓库外3.12单命令交互运行和strict reader通过。

## 4. 包内资源与 M5/M6 来源闭合

- [x] 11 项基础模型、案例、组分、场景、控制、M5 覆盖、M6 配置/清单已作为包内资源注册。
- [x] 11 项包内资源与仓库版本化源文件的逐字节 SHA-256 一致性已通过定向验证。
- [x] 包内 loader 验证基础版本、对象类型、配置兼容性和资源哈希。
- [x] M5 覆盖层只应用闪蒸温度、洗水比和两个切割温度四个白名单值，不原地修改基础对象。
- [x] M5 manifest、四项正式产物、pipeline、有效模型/案例/组分对象和 M6 basis 指纹交叉绑定。
- [x] M5 覆盖层强制 `case_alignment_only`，且其 metadata 禁止解释为现场动态验证。
- [x] M6 便携场景绑定正式 M6 配置、manifest 和正式结果指纹。
- [x] 每次运行发布必须按固定顺序带齐 11 项资源，字节、artifact SHA-256 和 `source_fingerprints` 同时绑定已安装包。
- [x] 从唯一wheel安装后的仓库外环境加载全部11项资源，bundle、有效对象和M5/M6来源链复核通过。

## 5. 统一执行器与单次 API

- [x] 五个固定预设已经由统一 executor 真实执行到预期状态：
  M2 `success`；M3/M4 `success` 且各 7201 样本；M6 泵跳 `limited` 且有 60 s 故障、
  62 s 保护触发；汽提蒸汽请求 `rejected` 且求解器未调用。
- [x] M2 `steady-baseline` 已通过单次 `run()` 写出全部包内输入并由 `read_run()` 重载。
- [x] `run_id` 和请求时间变化不改变相同物理请求/结果指纹的定向反例已通过。
- [x] 顶层 `petroleum_rto.cdu.runtime` 正式导出 `load_preset`、`run`、`read_run`、`BatchRequest`、
  `execute_batch`、`read_batch` 和 `resume_batch`，导出集已与当前公共接口核对。
- [x] M3 `open-loop-feed-step` 通过公开 `run()` 完整写出并由 `read_run()` 重载。
- [x] M4 `closed-loop-feed-step` 通过公开 `run()` 完整写出并由 `read_run()` 重载。
- [x] M6 `m6-abnormal-pump-trip` 通过公开 `run()` 完整写出并由 `read_run()` 重载。
- [x] M6 `m6-structural-rejection` 通过公开 `run()` 保存拒绝证据并由 `read_run()` 重载。
- [x] 上述统一入口复现的请求、有效输入、结果、版本和来源指纹与直接 executor 证据一致。
- [x] 最终候选重复运行确定性测试通过，差异只允许出现在 run ID、路径和墙钟字段。

## 6. 运行目录、manifest-last 与防篡改

- [x] 运行目录包含请求、输入副本、结果、轨迹、事件、错误和最后发布的 manifest。
- [x] manifest 记录 `runtime_status`、`engine_status`、`domain_status`、`source_fingerprints`、完整版本谱系、环境、
  已安装源码树 SHA-256 和每个产物的路径、大小、哈希。
- [x] 版本谱系显式含 `m5_overlay_version`；Git commit/dirty 无法可靠发现时写为 `unavailable`。
- [x] 无 manifest 目录按 `incomplete` 拒绝；修改已列文件任一字节会被 reader 拒绝。
- [x] 已有 run ID 不覆盖；失败/拒绝证据可保存且不会替换旧成功。
- [x] manifest-last、round-trip、tamper 和不覆盖定向测试已记录为 3 项通过，相关 ruff/mypy 通过。
- [x] 定向反例已覆盖 manifest 重签状态矛盾、路径穿越、缺少输入、多余/暂存文件、JSONL 计数与
  一次读取、请求/结果/11 输入/版本/来源指纹错绑。
- [x] 验证写出中途失败不会留下可被 reader 当成完整成功的 manifest。
- [x] 验证 2 h 动态轨迹按 JSONL 写出/读取时的峰值内存；M3/M4各2次，writer峰值约`2.1 MiB`，
  释放原payload后的reader峰值约`168.7/224.5 MiB`，未保留不必要的第二份完整轨迹。

## 7. 批处理、失败隔离与恢复

- [x] `BatchRequest` 严格字段、稳定 item 顺序和 batch request fingerprint 已实现并测试。
- [x] 单项失败不终止后续项，且每项保存自己的运行状态和证据。
- [x] 批事件为追加式记录，最终 batch manifest 最后发布。
- [x] 恢复前严格核对原批请求指纹；不一致请求被拒绝。
- [x] manifest/hash 有效的已完成项恢复时跳过。
- [x] 无 manifest 的不完整项创建新 attempt，不覆盖旧目录。
- [x] 既有失败默认保留；只有显式 `retry_failed` 才创建新 attempt。
- [x] 已完成清单在追加恢复事件前先历史化；恢复再次中断后可继续，旧成功、旧失败、旧不完整和旧清单仍可审计。
- [x] 公共 `read_batch()` 严格校验请求/事件文件、历史清单、item 计数/顺序、attempt 路径、run manifest 和所有交叉指纹。

## 8. CLI 与模块入口

- [x] `cdu-mini presets` 列出五个固定预设并返回成功退出码。
- [x] `cdu-mini run --preset ... --output ...` 只调用统一 Python API。
- [x] `cdu-mini run --request ... --output ...` 严格加载请求并拒绝未知字段/路径穿越。
- [x] `cdu-mini batch --request ... --output ...` 支持顺序批运行与失败隔离。
- [x] `cdu-mini batch --resume ...` 支持指纹核对和不可覆盖恢复。
- [x] `cdu-mini inspect <run-dir>` 和 `--json` 通过严格 reader 输出摘要。
- [x] `python -m petroleum_rto.cdu.runtime` 与 `cdu-mini` 行为一致。
- [x] CLI 成功、`limited`、`rejected`、`not_converged`、`failed` 和参数错误的退出码/显示契约已测试。
- [x] CLI 不包含第二套物理执行逻辑。

## 9. 性能基线

- [x] 性能记录包含机器/OS、Python、分发包、源码树和输入环境指纹。
- [x] M2 基准重复 3 次，记录 wall time、Python 分配峰值、样本数和数值步长。
- [x] M3 2 h `+5%` 进料重复 3 次，报告中位数和最大值。
- [x] M4 2 h `+5%` 设定值重复 3 次，报告中位数和最大值。
- [x] 选定 M6 异常场景重复 3 次，报告中位数和最大值。
- [x] 性能报告只作环境相关基线，不设置脱离机器环境的过紧硬阈值。
- [x] 性能报告与实际 preset、样本数、步长、结果状态和指纹一致。

## 10. wheel 与仓库外干净环境

- [x] 最终候选重新确认`cdu-mini = petroleum_rto.cdu.runtime.cli:main`及11项JSON package-data。
- [x] allowlist源码副本生成唯一sdist，再由sdist生成唯一wheel；wheel为
  `petroleum_rto_cdu_model-0.1.0-py3-none-any.whl`，325430 bytes，SHA-256=`4ca79569d4…`。
- [x] wheel包含75个Python模块文件和11项JSON资源，与allowlist/sdist逐字节一致；不含测试、文档、原始资料、缓存或秘密文件。
- [x] 在仓库外由Python 3.12.13新建clean venv。
- [x] 使用`--no-index --no-cache-dir --no-deps`从唯一wheel安装成功，`PYTHONPATH/PYTHONHOME`已清空。
- [x] 干净环境`pip check`通过，分发版本为`0.1.0`。
- [x] 干净环境`cdu-mini inputs/preview`和模块入口正常，公共preview/event/scenario/batch API可导入。
- [x] 干净环境以预览指纹确认360 t/h+第三切割点300 °C自定义稳态，成功生成含11项输入副本的manifest-last目录。
- [x] 干净环境调用`read_run()`成功重载；结果fp=`6508fab8…`、manifest fp=`af4da729…`。
- [x] 工作目录位于仓库外，仓库根和`src`不在`sys.path`且包来自clean venv；基础解释器标准库来自已披露的仓库`.venv`，安装源码树SHA=`87e1ae8a…`，排除editable源码泄漏。
- [x] M7.2唯一wheel为326474 bytes、SHA=`eaf14dff…`，75 Python+11 JSON与allowlist/sdist逐字节一致；clean源码树SHA=`fffc3494…`。
- [x] M7.2 clean环境用只含360 t/h进料和第三切割点300 °C的稀疏JSON，经一条命令预览、`yes`确认、稳态success并由strict reader核对11输入；结果/manifest fp=`3c526d93…/271f794f…`。

## 11. 代码质量与全量回归

- [x] 最终全仓库`pytest -q`共634项在413.85 s全部通过，无失败、skip或xfail摘要。
- [x] 最终全仓库`ruff check .`通过；未把额外`ruff format --check`的95文件机械重排扩大为发布末期代码改写。
- [x] 最终75个安装源码及2个正式benchmark脚本，共77文件strict mypy零问题。
- [x] 最终`pip check`通过，公共单次/批处理API导入烟雾正常。
- [x] `git diff --check`退出0，无空白错误；仅实施方案、STATUS和pyproject报告已披露的LF→CRLF提示。
- [x] 同一全仓库回归覆盖M0～M6正式冻结测试，既有物理结果和正式产物未被M7改变。
- [x] M7.2最终全仓库640项pytest在414.97 s通过；ruff、77文件strict mypy、pip check及相关format门禁通过。
- [x] 独立终验覆盖错误成功、tamper、path traversal、批恢复、wheel资源、clean环境与授权边界，最终结论`GO`、无剩余阻塞。

## 12. 文档与一致性

- [x] [M7 模型卡](M7_MODEL_CARD.md)披露用途、层级、来源、适用域、已知限制和禁止用途。
- [x] [M7 数据字典](M7_DATA_DICTIONARY.md)覆盖请求、结果、事件、错误、时间序列、manifest 和批语义。
- [x] [M7 使用说明](M7_USAGE.md)覆盖固定预设、受控输入、预览确认、结果摘要、Python API、CLI、运行目录、reader、批恢复和干净wheel流程。
- [x] 本发布清单在全部强制证据和独立`GO`形成后才将M7由进行中更新为已完成。
- [x] 四份 M7 文档及正式口径各只有一个 H1，全部本地 Markdown 链接存在，且无行尾空白；
  本轮文档空白自检也无错误。
- [x] 数据字典中的批请求、事件、当前/历史 manifest、item 和 attempt 语义已与实际契约核对。
- [x] 使用说明中的 CLI 参数、退出码和示例已与当前 parser/入口核对。
- [x] 模型卡、使用说明和发布清单已链接并准确区分正式执行性能与artifact I/O基线的测量边界。
- [x] wheel干净环境通过后，模型卡和使用说明已同步为“wheel已验、M7仍待全量与终验”。
- [x] `STATUS.md`顶部快照、M7执行记录、风险、授权边界、下一步和状态变更记录已同步最终证据。

## 13. 发布签署

所有强制项已完成：

- [x] 实施负责人复核：主Agent `/root`，2026-08-18；最终源码树SHA-256=`1003a7b2893f90cf823829d273a5e5ea9681d79199a506ac2569fc380767c867`。
- [x] 独立审查复核：`/root/m7_independent_audit`，2026-08-18；结论“GO，无剩余正式阻塞”。
- [x] wheel复核：SHA-256=`6a39de4779425c464f5f1f2300fdf1dbbbc84c11ac234ca792c0480c9a91cb58`；clean运行manifest fp=`41cefd16b6e0ad7783646edbd578b43ada7a528d79fb831b8a4e365e9d1f2259`。
- [x] 文档复核：模型卡、数据字典、使用说明、两份性能报告、发布清单和状态文档一致。
- [x] 最终发布决定：`GO`。

M7.1增量签署：

- [x] 实施复核：主Agent `/root`，2026-08-18；源码树SHA-256=`87e1ae8a4c404d0835a4d1ad71908f57d94f10c5c0038d9472b7ac62294637d0`。
- [x] wheel复核：SHA-256=`4ca79569d4c7d075ab90453ec7d55b56039c35c202c9419320577e8ba1b27329`；clean自定义运行manifest fp=`af4da72921eec5acac00059000edb37824abac95d302b17c53e4f28ab20af769`。
- [x] M7.1文档与STATUS最终结构、链接、空白和事实一致性自检完成。
- [x] M7.1增量发布决定：`GO`。

M7.2增量签署：

- [x] M7.2文档与STATUS最终结构、链接、空白和事实一致性自检完成。
- [x] M7.2 wheel、clean运行与结果重载复核完成；wheel SHA=`eaf14dff98d0afee6a0fd988c0a08bc8e1338dbe37f631edff1288654224d680`，clean manifest fp=`271f794f3dfe9cbc0a481d7f0ed36c3cb77138e646a652f1c3dde40ddaea14db`。
- [x] M7.2增量发布决定：`GO`。

当前M7.2发布决定为`GO`。唯一交付候选位于`dist/m7-2-final/`，wheel完整SHA-256为
`eaf14dff98d0afee6a0fd988c0a08bc8e1338dbe37f631edff1288654224d680`。

## 14. 关联文档

- [M7 模型卡](M7_MODEL_CARD.md)
- [M7 数据字典](M7_DATA_DICTIONARY.md)
- [M7 使用说明](M7_USAGE.md)
- [M7 封装与可复现交付口径](06_M7_封装与可复现交付口径.md)
- [M7 正式执行性能基线](../../reports/modeling/M7_PERFORMANCE_BASELINE.md)
- [M7 完整 2 h Artifact I/O 基线](../../reports/modeling/M7_ARTIFACT_IO_BASELINE.md)
- [CDU Mini Loop 实施方案](01_CDU_Mini_Loop实施方案.md)
