---
name: archive-plex-anime
description: Use when inspecting, organizing, subtitling, remuxing, packaging, archiving, replacing, cleaning, or tracking anime TV and Movie media for Plex, Emby, Jellyfin, or compatible media libraries on Windows, including Movie BDMV M2TS/MKV original-disc audio replacement and cdN stacked movies.
---

# 动画媒体库归档

## 路由与模式

把规范化绝对路径按配置中的完整目录边界路由：

- `paths.workRoot` 及其子目录 → TV；Anime 是 Plex 的 TV 库名；
- `paths.movieWorkRoot` 及其子目录 → Movie。

路径只决定分支。任务模式由用户要求决定：

- `complete-archive`：默认完整归档；
- `replacement`：TV/Movie 洗版；
- `archive-only`：用户明确要求原样入库、不重新封装；
- `local-only`：只执行指定的 `movie-audio`、`subtitle`、`remux`、`package` 本地能力及必要依赖；`inspect` 自动执行。

`archive-only` 是任务模式，不是完整流程中的一个步骤。

`local-only` 不选择 `review`：各本地步骤当场执行与 review 相同的产物验收，显式请求 `review` 返回 `LOCAL_STEP_UNSUPPORTED`。`review` 只用于需要准备最终 NAS、字幕归档和维护表动作的任务。

四个模式也是版本化预置工作流。用户要求按需执行时，改用自定义能力选择：`inspect`、`metadata`、`movie-audio`、`subtitle`、`remux`、`subtitle-package`、`video-delivery`、`subtitle-delivery`、`kdocs-tracker`、`cleanup`。只选择公开能力，不直接选择 `prepare-fonts`、`subset`、`rename`、`review` 或 `finalize`；统一解析器负责依赖、冲突、实际可用性、内部步骤和最终输出。预置与自定义选择不混用；自定义选择不含最终输出能力时自动按 `local-only` 规划。`cleanup` 必须自动补齐 `video-delivery`，字幕 ZIP 或维护表不能单独解锁清理。

四个预置继续默认选择 `metadata`。自定义选择未包含 `metadata` 时必须完全离线，不读取元数据凭据、不构造客户端、不访问 TMDB/TVDB；若同时选择最终输出，必须用 `decisions.title` 提供已确认标题。

CLI 与本独立 Skill 保留 KDocs。作为 Hub 入口运行时必须使用 `--entrypoint hub`，能力目录、前检、配置和执行均不得出现 `kdocs-tracker`；不得由 Hub 自行补回维护表动作。

## 执行工作流

先读取 [references/workflow.md](references/workflow.md)，再按分支读取：

- TV：[references/tv-rules.md](references/tv-rules.md)
- Movie：[references/movie-rules.md](references/movie-rules.md)
- Movie 原盘音轨洗版：再读 [references/movie-audio-rules.md](references/movie-audio-rules.md)
- 配置和命令模板：按需读 [references/environment.md](references/environment.md)
- TMDB/TVDB 识别、季集整理或元数据前检：[references/metadata-rules.md](references/metadata-rules.md)

统一状态机：

```text
init → inspect → 前置确认
→ [movie-audio] → [subtitle] → [remux] → [package]
→ review → 本地产物与最终写入合并确认
→ finalize → cleanup
```

方括号步骤由统一计划器按任务与媒体实际情况选择。正常完整任务只暂停两次：前置确认，以及本地产物验收后的最终确认。确认后发现输入、环境或目标变化时返回 `FAILED`，不临时增加第三次确认。

查看能力目录：`python scripts/workflow.py capabilities --entrypoint cli|skill|hub --branch tv|movie`。自定义初始化使用 `init ... --capabilities remux,subtitle-package`；解析结果写入状态中的请求能力、自动补齐能力、内部步骤和实际最终输出。重新初始化不同选择会清空旧完成步骤和确认。

## 强制规则

- 用户给出的媒体目录同时是源目录和工作目录；不创建独立工作目录。
- 用户可检查的 ASS、MKV、ZIP 直接输出在原目录。标准名冲突时统一使用 Windows 编号：`文件 (1).ext`、`文件 (2).ext`。
- remux 整批失败时删除本轮产生的 MKV 与 remux 临时文件；成功重跑后只删除缓存签名仍匹配的上一轮 remux 产物。源文件、签名已变化文件和无关编号文件不得删除。
- 非破坏性前检允许写最小状态和执行缓存，但不得改动源媒体、生成正式产物、写入 NAS/字幕归档或更新维护表；内封附件只按附件 ID 和允许的字体扩展名提取到 `.archive-temp`，原附件名不得参与输出路径。
- 只允许 `.archive-state.json`、`.archive-temp`、`.archive-logs` 作为隐藏状态、临时和日志内容。
- `scripts/archive_rules.py` 约束路径、层级、标准名、编号和状态版本；TV/Movie 计划器生成确定性计划；步骤不得重新推导媒体规则。
- TV 与 Movie 共用状态机、确认、字体、工具、验收、最终写入和恢复；季集/单文件、字幕解析、音轨和洗版规则留在各自分支。
- Movie 可由一个文件或完整的 `cd1`、`cd2`、`cd3` 等 Plex 堆叠文件组成；视频、字幕、原盘音轨源和最终目标必须按相同 `cdN` 一一对应，不合并时长或去除堆叠标记。
- 最终新封装的 TV/Movie 固定删除 PGS。`archive-only` 检出 PGS 时返回 `NEEDS_USER`，不得自动改模式。
- 无外挂 ASS 时，只有内封 ASS 与所需字体附件齐全才可跳过字幕处理；元数据不规范时仍需 remux。缺字幕或附件时不可跳过。
- Movie 同时保留规范内封 ASS 与加入外挂 ASS 时，使用 `retain_embedded_subtitles`：内封轨及其字体附件直接透传，外挂轨照常子集化；两类字幕统一排序且每个分段只设一条默认轨。
- 非破坏性字体前检按“工作目录文件 → assfonts 全局数据库 → 完整字体库 `fc-subs.db`”查内部名；不得遍历主力或完整字体库。索引缺失、无效或未命中时返回 `NEEDS_USER`，不启动 `FontLoaderSub.exe`。
- 将工作目录或完整字体索引命中的新字体批量导入主力字体库；存在导入时，每任务只在已验证数据库副本上增量执行一次 `assfonts -f <主力字体库> -b`，验证后原子替换正式 `fonts.json`。不得显式加入 Windows Fonts，assfonts 会自动加载系统字体。
- 正常子集化只传全局数据库，不传 `-f`。失败时才按“工作目录 → 全局数据库中的主力字体 → 完整字体索引”定位日志明确指出的字体；同层 TTF 优先、路径稳定排序、每字体最多尝试 8 个去重候选。恢复只用选中字体临时目录加全局数据库；命中 OTF/CFF 时才调用 otf2ttf。无法定位或重试失败则 `FAILED`。
- TV 的 S0 视频及各字幕组分别按 NFKC、不区分大小写的数字自然顺序映射；某集合出现任意显式映射时必须完整映射。数量不一致且没有完整显式规则时返回 `NEEDS_USER`；S1 及以上仍只使用文件名集数。
- TV 视频轨压制组全任务一致时使用 `release_group`；不同季使用 `release_group_by_season` 并完整覆盖实际季，封装和维护表按对应季执行。
- TV 字幕按季独立排序：显式 `subtitle_order_by_season` 优先，否则继承上一个实际季仍出现的字幕组顺序，再考虑更早出现与跨季覆盖；依据相同时在前置确认决定。每集第一条实际字幕是该集唯一默认轨。Movie 继续使用全局字幕排序规则。
- `inspect` 默认使用 TMDB 识别作品、标题和季集，TVDB 仅辅助核对或提供用户指定季序；自动查询只从目录和主 MKV/MP4 文件名提取作品名，排除 MKA、字幕、原盘与附加内容，提取不唯一时在联网前 `NEEDS_USER`。API 只生成建议，前置确认后固化为普通决定，后续步骤不联网；用户显式标题、查询词、ID、季集映射和媒体分支始终优先。
- 字幕组并行，MKV 封装串行；最终视频、可选 ZIP、维护表并行写入。
- TV/Movie 字幕归档 ZIP 为累计归档：目标已存在时，本地产物必须先合并旧 ZIP 与本次字幕，保留旧独有条目、以本次条目替换规范化同路径冲突，并在 review 验收完整合并 ZIP；最终写入前后用中央目录签名拒绝目标并发变化，不增加确认关卡。
- 普通视频不计算整文件哈希；最终确认绑定规范化最终动作、已验收源文件轻量签名和维护表计划的完整 SHA-256，`finalize` 写入前重新计算并拒绝任何变化。
- NAS/ZIP 正常先复制到目标同目录批次临时文件、比较大小，再用 `os.replace` 落位；替换无效时记录本批次 `IN_PROGRESS` 后直接覆盖正式目标并再次比对大小，同时返回 `WARNING`。同库位后续文件可在当前批次复用直接覆盖回退；失败时不得写完成检查点或清理任务目录。
- 所有 NAS 视频和字幕 ZIP 最终目标必须位于任务目录外；`review` 准备最终批次时拒绝重叠目标，`cleanup` 删除前再次检查。
- `create` 在准备最终批次及实际复制前确认目标仍不存在；只有本批次已记录的直接覆盖半文件可重试。`replace` 经最终确认后直接覆盖。维护表写入前按已完成块与本批次预期、未完成块与前检快照核对；每块成功立即保存进度，真正的外部变化才零写入失败。
- 每条预期轨道必须完整声明名称、语言、默认、强制及适用的声道信息，封装命令显式写入这些 flag；Movie 关闭章节时参数必须作用于主媒体输入，TV 使用外挂 ASS 时不得继承源附件。
- 清理永远最后执行。清理前必须证明当前批次至少一个视频已安全写入任务目录外，并验证全部实际输出检查点；任何最终写入失败或视频证明缺失均保留任务目录。
- 中文路径、ASS、JSON、日志和 KDocs stdin 使用 UTF-8；外部程序使用参数数组，不用 `shell=True`。

## 计划、状态与后端

计划只列本次选中的工作流步骤，不向用户展开逐文件命令或产物对象。保留最小 `.archive-state.json`，并记录 `selection_mode`、`preset`/`preset_version`、`entrypoint`、请求/解析/自动补齐/不可用能力、`selected_steps` 和实际 `final_sinks`；详细计划、实际编号路径和执行结果只写 `.archive-temp/execution-cache.json`。当前契约为 `STATE_SCHEMA=8`、`RULES_VERSION=19`、`BACKEND_CACHE_SCHEMA=19`、预置版本 `2`，旧状态不迁移。属于当前目录且 schema、工作流版本与 inspect 状态有效的前检缓存，即使包含 `NEEDS_USER` 也保存 inspect 检查点；工具、媒体或缓存硬失败不保存。

首次 `inspect` 执行完整前检；`inspect --rerun` 只用路径、大小和修改时间比较视频、字幕、工作目录字体、字体索引、库位输入及 Movie 原盘音轨源，复用未变化的有效结果，只重跑受影响组件。Movie 堆叠片按 `cdN` 独立复用已成功的 PCM 结果；计划摘要始终重新生成。缓存缺失、损坏或版本不兼容时自动回退完整前检，不增加状态文件、公开步骤或确认关卡。

缓存存在时复用已确认实际路径和完成步骤。缓存丢失时不得猜测哪个编号产物属于本任务：重新前检、失效最早受影响的本地步骤，并从下一个可用编号重新生成。

所有步骤通过 `scripts/workflow.py` 和 `scripts/steps/` 调用统一命令入口 `scripts/internal/archive_backend.py`。执行缓存/阶段失效、非破坏性前检、字幕/字体流水线、MKV 封装与验收、字幕 ZIP、最终写入与检查点、轻量签名和公共错误分别由 `manifest.py`、`preflight.py`、`subtitle_pipeline.py`、`remux_pipeline.py`、`subtitle_archive.py`、`final_delivery.py`、`signatures.py`、`errors.py` 实现。MediaInfo、库位、Movie 原盘音轨和维护表算法分别位于 `media_inspection.py`、`library_target.py`、`movie_audio.py`、`tracker.py`；这些模块均无独立工作流入口。Movie 原盘音轨只通过统一 `movie-audio` 后端命令执行。

对外错误只使用 `NEEDS_USER`、`FAILED`、`WARNING`；内部 `DECISION_REQUIRED` 必须在公共边界映射为 `NEEDS_USER`。
