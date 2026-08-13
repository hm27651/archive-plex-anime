# 通用工作流

## 1. 状态机与步骤选择

统一状态机：

```text
init → inspect → approve-preflight
→ [movie-audio] → [subtitle] → [remux] → [package]
→ review → approve-final → finalize → cleanup
```

`media_plan.py` 根据任务模式调度平级的 `tv_plan.py` / `movie_plan.py`。`workflow.py` 只管理步骤、两次确认和恢复，不解析字幕、轨道或库位。

- `complete-archive`：选择所有适用本地步骤及 review/finalize/cleanup；
- `replacement`：由分支计划器选择普通洗版或 Movie 原盘音轨步骤；
- `archive-only`：由 TV/Movie 各自生成直接入库计划，不选择 remux；
- `local-only`：只选择用户要求的 `movie-audio`、`subtitle`、`remux`、`package` 及必要依赖，`inspect` 自动执行；不写 NAS、维护表或删除目录。`review` 会同时准备最终目标，因此不属于 local-only；显式请求时返回 `LOCAL_STEP_UNSUPPORTED`。

完整任务必须有可执行计划；空计划不得进入 review。

## 2. inspect、非破坏性前检与前置确认

任务提交即授权非破坏性前检：

1. 规范化目录并按完整边界确定 TV/Movie；
2. 确定任务模式和适用步骤；
3. 快速检查这些步骤实际需要的工具；
4. 枚举外挂 ASS/SSA、字幕组和所需字体；
5. 仅读取视频封装轨道、章节和附件信息；
6. 按分支生成命名、音轨、字幕和章节预期；
7. 预读维护表并扫描 Anime1/2/3 或 Movie1/2/3 的作品根目录，确定唯一库位和新建/洗版方式；
8. Movie 原盘音轨洗版额外完成匹配与 3–5 点 PCM 采样。

前检允许写入 `.archive-state.json` 和 `.archive-temp/execution-cache.json` 以支持确认与恢复，但不改动源媒体，不生成正式 ASS/MKV/ZIP，也不做视频哈希、字体转换、NAS/字幕归档写入、维护表写入或整库媒体深检。内封 ASS/字体临时提取只使用附件 ID、轨道 ID 和允许的字体扩展名生成 `.archive-temp` 内部路径；MKV 原附件名只作元数据，不得参与输出路径。

首次 `inspect` 执行上述完整前检。再次执行 `inspect --rerun` 时，先用“规范化路径、大小、修改时间”及相关工具/配置的轻量签名自动判定变化域，再按以下依赖复检；无变化的有效结果直接从同一个执行缓存复用，Movie PCM 只复用匹配唯一、同步成功且至少有 3 个有效采样点的结果，最终计划与前置确认摘要始终重新生成：

- 视频变化：只重新读取变化文件的封装信息；涉及内封字幕时重查内封轨与附件；Movie 原盘音轨洗版只重跑对应分段的 PCM；
- ASS/SSA 变化：重查变化字幕及字体需求，再重查字体可用性；
- 工作目录字体、`fonts.json` 或 `fc-subs.db` 变化：只重查字体可用性；
- 标题或库位相关配置变化：只重查维护表/NAS 库位；Movie 标题变化同时快速刷新字幕 ZIP 引用，不重跑 PCM；
- Movie 压制版或原盘源变化：只重跑对应 `cdN`；其他分段继续复用；
- 仅字幕顺序、默认项、压制组、章节等计划决定变化：只重建计划。
- 元数据查询词、ID、语言、季序、代理、凭据存在性或本地 MKV/MP4、字幕相对路径列表变化：只重查元数据；MKA 变化不失效元数据，API 建议标题变化时同时重查库位。

复检仍会快速确认所选能力需要的工具是否存在；用户要求“只检查字体”等范围不覆盖自动变化检测。若同时发现媒体变化，必须按依赖复检。组件缓存缺失、损坏、版本不兼容，或成功结果所依赖的临时提取文件丢失时，自动回退完整前检；不增加状态文件、公开步骤或确认关卡。前置确认以后发生输入、环境或目标变化仍按执行边界返回 `FAILED`，不得静默复检。

前置确认一次性展示：分支、模式、实际库位、新建/洗版、压制组（TV 可按季指定）、TV 季集或 Movie 名称、字幕组/语言/顺序/默认项、保留和删除音轨、章节、字体问题、Movie 原盘映射与固定偏移、选中步骤。

用户决定使用 UTF-8 `--decisions-stdin` 合并。常用键包括 `title`、`metadata`、`release_group`、`release_group_by_season`、TV 的 `subtitle_order_by_season`、兼容/ Movie 使用的 `subtitle_order`、Movie 的 `default_subtitle`、`embedded_subtitle_names`、`video_keep`、`audio_keep`、`keep_chapters`、`episode_map`、`disc_source`、`video_source` 和 `movie_audio_pairs`。轨道和集数决定统一以工作目录相对路径为键：

```json
{
  "video_keep": {"S1/source.mkv": "video-track-key"},
  "audio_keep": {"S1/source.mkv": ["audio-track-key"], "S1/source.mka": ["audio-track-key"]},
  "episode_map": {"OVA/source.mkv": "S00E01"}
}
```

Movie 的 `subtitle_order` 必须是全部实际字幕组的完整、唯一排列。TV 优先使用 `subtitle_order_by_season`，各值必须是对应季全部实际字幕组的完整、唯一排列；未指定季按 TV 继承规则解析，旧全局 `subtitle_order` 仍兼容。多视频轨同样在第一次确认中用 `video_keep` 选定唯一轨道。TV 任务如不同季压制组不同，使用 `release_group_by_season`，键为 `S0`、`S1` 等，且必须覆盖所有实际季。

## 3. subtitle

字体优先级：

```text
工作目录字体文件 → `paths.assfontsDatabase/fonts.json`
→ `paths.fallbackFontDatabase`
→ 仍缺失则 NEEDS_USER
```

前检只扫描工作目录字体；主力字体库通过 assfonts 全局数据库查询，完整字体库通过 `fc-subs.db` 查询，禁止遍历这两个大型目录或启动 `FontLoaderSub.exe`。多个同内部名字体不形成用户候选决定；备用索引缺失、无效、未命中或字体完全缺失时列出字体并返回 `NEEDS_USER`，用户更新索引或补齐字体后使用 `inspect --rerun`。

工作目录或完整字体索引命中的新字体在确认后批量导入 `paths.primaryFonts`。无需导入且全局映射完整时不重建；存在导入时，每任务只在全局数据库副本上增量执行一次 `assfonts -f <主力字体库> -b -d <临时数据库>`，验证路径/face 唯一性及所需内部名后原子替换正式 `fonts.json`。命令不得显式加入系统字体目录，因为 assfonts 会自动加载系统字体。

执行顺序：

```text
只用 `-d <全局数据库>` 直接运行 assfonts，不传 `-f`
→ 成功且有有效 [Fonts] 产物：完成
→ 失败或产物无效：从日志精确定位报错字体（支持两字中文名）
→ 从工作目录、全局数据库和 `fc-subs.db` 一次性建立候选索引并去除内容相同文件
→ 候选按层级、同层 TTF 优先、路径升序排列，每字体最多 8 个
→ 恢复命令只增加选中字体临时目录；选中 OTF/CFF 时才转换，只重跑失败组
→ 无法定位字体或重试失败：FAILED
```

assfonts 成功产出可读字幕与有效字体区段即视为成功，不做逐字体附件证明。最终 ASS 写 UTF-8-SIG；输入兼容 UTF-8-SIG、UTF-8、GB18030。

TV 字幕组并行输出到 `Sx/字幕组`；Movie 字幕平铺到根目录。标准目标已存在时使用 ` (1)`、` (2)`。实际编号同步更新 remux 输入和 ZIP 条目。

## 4. remux 与 package

MKVToolNix 严格串行封装。主媒体轨道按前检生成的精确轨道映射选择；MKA、外挂 ASS 等辅助输入使用限定参数。TV/Movie 都删除 PGS；每条预期轨道必须完整声明类型、名称、语言、默认、强制及适用的声道信息，并由 mkvmerge 参数显式设置。章节和附件同样写入精确预期；静态契约在执行前拒绝字段不全或重复目标。

一次 remux 调用按整批跟踪产物：任何剧集失败时，回收本轮已生成的 MKV 及 `.archive-temp/remux` 临时文件，不留下可见的半批次结果；成功重跑后，只清理执行缓存中有记录、仍位于任务目录内且轻量签名未变化的上一轮 remux 产物。为覆盖进程被强制中断的情况，每个 MKV 落位前只写一个轻量 `attempt.json` 清单，正常成功或失败立即删除；下次 remux 先按签名回收中断残留再删除清单。源视频、签名已变化文件和无关编号文件始终保留。因此用户验收时正常只看到源文件和本轮最终 MKV；失败后仍从整批开头重跑，不提供逐文件恢复。

ZIP 使用存储模式并将最终中文条目统一写为 UTF-8；读取未设置 UTF-8 标记的本机旧归档时按 GB18030 兼容解码。TV 条目为 `Sx/字幕组/文件.ass`；Movie 条目平铺。MKV 与 ZIP 标准名冲突同样使用 Windows 编号。字幕归档目标已存在时，package 先读取中央目录并在工作目录生成完整合并 ZIP：旧独有条目保留，本次规范化同路径条目优先；绝对路径、`..`、空路径段及 NFKC/大小写等价重复条目直接失败。`archive-only` 的预制 ZIP 同样先进入 package 合并，不直接覆盖归档目标。

完整流程不在 remux/package 后重复深检；统一留给 review。local-only 不选择 review，直接在各本地步骤执行相同验收。

## 5. review、finalize 与 cleanup

`review`：

- 每个 MKV 只运行一次 `mkvmerge -J`；
- 比较轨道数量、顺序、名称、语言、默认/强制、章节、附件和 PGS 删除；
- 每个 ZIP 只检查一次 CRC 和实际条目；
- 合并 ZIP 验收的是旧归档与本次字幕合并后的完整条目；中央目录签名随最终批次绑定；
- 使用前检已确定的库位和维护表快照生成最终目标，不重新检索库位；
- TV 从 `作品/季/文件`、Movie 从 `作品/文件` 的目标层级生成作品根摘要，不从名称后缀、首字母或点号猜测；
- 所有 NAS 视频和字幕 ZIP 目标必须位于任务目录外，否则不得进入最终确认；
- 将规范化最终动作、各本地产物的路径/大小/修改时间轻量签名及维护表计划计算为完整 SHA-256；短 `batchId` 仅为其前 24 位；
- 返回简洁产物计数、warning 和最终写入摘要。

用户确认本地产物与最终动作后，`finalize` 并行执行：

```text
NAS 视频写入 | 可选字幕 ZIP 写入 | Plex 维护表更新
```

`approve-final` 将完整批次摘要写入最小状态；`finalize` 根据当前最终动作重新计算摘要，同时核对已批准摘要和短 `batchId`，任一变化都在写入前 `FAILED`。

视频和 ZIP 正常按以下方式写入：

```text
复查本地源轻量签名 → 复制到目标同目录批次 .part
→ 比较源/.part 大小 → os.replace 落位
→ 比较源/正式目标大小 → 写完成检查点
```

若 `os.replace`/同目录落位失败但批次、源和目标边界仍有效，则先写精确到 batchId、源、目标和大小的 `IN_PROGRESS`，再从本地源直接覆盖正式目标，完成后只比大小并返回 `WARNING`。直接覆盖成功后，当前库位剩余文件可在本批次内直接使用同一回退，避免重复上传；失败时不写完成检查点、不清理任务目录。`create` 重试只允许覆盖与本批次 `IN_PROGRESS` 完全匹配的半文件，其他同名目标仍视为外部变化。

每个成功项立即写包含源/目标大小的最小检查点；重跑只在检查点和当前目标大小都有效时跳过。没有 ZIP 的合法任务不要求 ZIP 检查点。任一分项失败均保留任务目录。

写入边界：

- `create` 在生成最终摘要时要求目标不存在，并在实际复制前再次轻量确认；若期间出现同名目标则停止，避免覆盖他人新建内容；
- `replace` 在最终摘要生成时确认原目标存在，用户最终确认后直接覆盖，不做整文件哈希或额外复检；
- 合并字幕 ZIP 在 package 时记录目标中央目录的“规范路径、CRC32、大小”摘要；review 与实际提交前各快速比较一次，目标发生实质变化时以 `ZIP_MERGE_BASE_CHANGED` 零写入失败并要求重新合并；
- 维护表按 KDocs 上限分块；开始或重试时将实时目标列与“已完成块为本批次预期、未完成块为前检快照”的各合法前缀匹配，每块成功立即保存进度并从首个未完成块续传；无法匹配任何合法前缀才以 `TRACKER_CONCURRENT_CHANGE` 停止继续写入并失败；
- 最终视频、ZIP 和维护表仍并行执行，上述检查不增加确认关卡。

全部选中写入成功后，`cleanup` 再次确认所有必要目标位于任务目录外、存在且源/目标/检查点大小一致，才可删除整个任务目录。

## 6. 状态、恢复与错误

`.archive-state.json` 只保存 schema/rules_version、任务、分支、选中/完成步骤、用户决定、两次确认、已批准完整批次摘要、简洁最终目标、直接覆盖尝试和最终写入/维护表分块检查点。当前契约为 `STATE_SCHEMA=6`、`RULES_VERSION=17`；执行缓存为 `BACKEND_CACHE_SCHEMA=17`、工作流修订 `2026-08-13-metadata-title-extraction`，旧状态不迁移。

`.archive-temp/execution-cache.json` 保存详细计划、实际编号路径和执行结果。

- 缓存存在：复用已记录的实际编号路径和完成步骤；
- 缓存丢失：重新前检；标准名及 ` (数字)` 文件不被当作原始输入；失效最早受影响步骤；不猜测已有编号产物，重做时使用下一个编号；
- schema 或 rules_version 不匹配：`FAILED: task state contract mismatch`。

执行缓存属于当前工作目录，且 JSON、schema、工作流版本和 inspect 阶段状态有效时，inspect 的 `NEEDS_USER` 仍保存前检检查点；工具、媒体读取或缓存失败不保存。

对外错误：

- `NEEDS_USER`：前置确认仍缺决定或 `archive-only` 含 PGS；
- `FAILED`：确认后输入/环境/目标变化，或执行失败；
- `WARNING`：产物合格的非阻塞信息。

## 7. 编码与性能

- 中文路径使用 Unicode 参数和 `pathlib.Path`；
- JSON stdin/stdout、日志、Markdown、KDocs 使用 UTF-8；
- 外部命令使用参数数组，不使用 `shell=True`；
- 字幕组并行，MKV 串行，最终三类写入并行；
- assfonts 成功时不检查 otf2ttf；
- 普通视频不计算整文件哈希。
