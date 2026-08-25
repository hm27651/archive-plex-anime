# 工具清单与 CLI 实施方案

> 项目：`archive-plex-anime`
>
> 基线：`6d15ce1 test: 隔离公开回归测试配置`
>
> 状态：已完成本地实现，待提交与 Windows/Linux CI。本方案只包含 `archive-plex-anime` 的修改，不包含 Bangumi Media Hub 的代码改造。

## 1. 目标

让 `archive-plex-anime` 成为 BDRip 工具信息的唯一事实来源，并提供工具查看、能力检查、已有路径配置和 Hub 清单导出能力。

本轮完成后：

- 工具名称、用途、主页、下载页面、制品、平台、架构和 SHA-256 只在本项目维护；
- CLI、独立 Skill 和 Hub 使用同一清单的不同入口投影；
- 用户可以查看工具、检查能力并配置已有工具路径；
- Hub 可以获得不含 KDocs 的稳定只读清单；
- 不改变 TV、Movie、字幕、封装、归档、确认和清理规则；
- 不实现自动下载；
- 暂缓的真实媒体集成测试继续保留，但不作为本轮阻塞项。

## 2. 项目边界

### 本项目负责

- 工具清单和 JSON Schema；
- 工具入口可见范围；
- 工具路径与能力检查定义；
- `tools list/check/use-path/export` CLI；
- Hub 专用清单投影；
- 纯 Python 回归测试；
- 正式需求、环境说明、README 和 Skill 说明。

### 本项目不负责

- Bangumi Media Hub 的 API、设置页或前端改造；
- Hub 中现有安装器的迁移；
- Hub 调用 archive 工作流或创建 BDRip 任务；
- ani-rss、WebRip 和 Bangumi 同步；
- Linux 完整工具镜像；
- 第一阶段的 `tools install`；
- 下载或运行真实媒体工具完成全量集成验证。

## 3. 唯一工具清单

新增：

```text
toolchain/
├─ manifest.json
└─ manifest.schema.json
```

`manifest.json` 是唯一允许人工修改的工具发行清单。Skill 发布副本和 Hub 投影必须由它生成，不能分别维护工具 URL、版本或校验值。

### 顶层字段

```text
schema_version
manifest_version
generated_contract_version
tools
```

### 工具字段

```text
tool_id
name
purpose
entrypoints
path_kind
path_setting
executables
project_url
download_page_url
binary_source_url
license
capability_checks
artifacts
```

### 制品字段

```text
version
platform
architecture
artifact_url
filename
sha256
size
archive_format
executable_paths
```

同一工具可以有多个平台制品，但相同工具、平台和架构不得出现冲突项。

## 4. 工具范围与入口

| 工具 | CLI | Skill | Hub | 处理方式 |
|---|---:|---:|---:|---|
| MediaInfo CLI | 是 | 是 | 是 | 外部工具 |
| MKVToolNix | 是 | 是 | 是 | 一组外部工具 |
| FFmpeg / FFprobe | 是 | 是 | 是 | 同一制品、分别检查 |
| assfonts | 是 | 是 | 是 | 外部工具 |
| KDocs CLI | 可选 | 可选 | 否 | 只供维护表能力 |
| otf2ttf | 内部 | 内部 | 不展示 | 推荐作为 Python 内部能力 |

### KDocs 边界

- CLI 和独立 Skill 可以继续保留 `kdocs-tracker`；
- KDocs 的 `entrypoints` 只能包含 `cli`、`skill`；
- Hub 投影、预置和导出结果不得包含 KDocs；
- 过滤由统一清单字段决定，不在多个模块维护额外黑名单。

### otf2ttf 边界

推荐把 OTF/CFF 转换改为本项目内部 Python 能力：

- 不要求用户配置独立可执行路径；
- 不在 Hub 中显示独立工具；
- 仍只在 assfonts 失败且明确命中 OTF/CFF 候选时调用；
- 保持现有失败边界，不得静默跳过字体恢复。

若本轮无法内部化，则继续保留原外部路径，并在清单中限制为 `cli`、`skill` 可见，不能直接删除能力。

## 5. 清单安全规则

- 制品必须固定版本、平台、架构、文件名和 SHA-256；
- 不得在运行时抓取网页“最新版”；
- 不得接受用户输入任意下载 URL；
- `artifact_url` 只能来自仓库内固定清单；
- `project_url` 和 `download_page_url` 只用于展示和手动下载；
- 清单不得包含凭据、本机路径、代理、缓存或安装状态；
- 未完成真实验证的平台不得标记为正式支持；
- 清单校验失败时明确失败，不能回退到隐藏的硬编码工具表。

## 6. 工具管理模块

新增：

```text
scripts/toolchain.py
```

建议职责：

```text
load_manifest
validate_manifest
visible_tools
select_artifact
resolve_configured_paths
check_tool
check_capability
update_tool_path
export_projection
```

该模块只处理工具清单、路径和能力检查，不读取媒体目录，也不参与 archive 媒体计划。

### 清单加载

- 使用相对项目或发布包的稳定路径；
- 支持 Windows 与 Linux；
- 校验 JSON Schema；
- 校验工具 ID、入口、能力 ID 和制品组合唯一；
- 返回稳定、可序列化的数据；
- 加载时不联网、不运行工具、不修改配置。

### 路径解析

- 只接受绝对路径；
- 文件型工具要求路径指向文件；
- 目录型工具要求找到全部必要程序；
- Windows 可识别 `.exe`，输出仍使用规范工具名；
- Linux 路径必须存在且可执行；
- Docker 只能使用容器内已挂载路径；
- 检查失败不得覆盖当前有效配置。

## 7. CLI 设计

在 `scripts/workflow.py` 增加 `tools` 子命令组。

### 查看工具

```text
python scripts/workflow.py tools list --entrypoint cli --json
python scripts/workflow.py tools list --entrypoint skill --json
python scripts/workflow.py tools list --entrypoint hub --json
```

返回：

- 清单版本与当前入口；
- 工具名称、用途和当前来源；
- 实际路径和版本；
- 平台与架构支持；
- 项目主页和下载页面；
- 能力检查列表；
- 是否支持受管安装。

工具缺失时仍必须正常返回项目主页和下载页面。

### 检查工具

```text
python scripts/workflow.py tools check --entrypoint cli --json
python scripts/workflow.py tools check --entrypoint hub --tool ffmpeg --json
```

稳定状态：

```text
ready
missing
not_configured
unsupported_platform
capability_failed
needs_recheck
```

检查失败通过 JSON 返回工具、能力、状态和友好原因，不输出凭据、完整环境变量或无关本机信息。

### 使用已有路径

```text
python scripts/workflow.py tools use-path <tool> <absolute-path> --json
```

流程：

1. 验证绝对路径；
2. 定位必要程序；
3. 执行该工具全部最低能力检查；
4. 检查通过后原子保存配置；
5. 回读并返回最终状态；
6. 任一步失败时保留原配置。

### 导出入口投影

```text
python scripts/workflow.py tools export --entrypoint hub --output <path>
python scripts/workflow.py tools export --entrypoint skill --output <path>
```

Hub 投影必须包含：

```text
schema_version
manifest_version
source_version
source_commit
entrypoint=hub
tools
```

Hub 投影必须：

- 自动排除 KDocs；
- 保留 Hub 需要的说明和固定制品；
- 不包含安装状态、本机路径、媒体规则或任务状态；
- 使用稳定字段顺序和 UTF-8；
- 相同源清单生成完全一致的内容。

## 8. 工具能力检查

### MediaInfo

- 验证版本输出可解析；
- 验证能够读取最小媒体样本并输出 JSON；
- 不把 GUI 程序误判为 CLI。

### MKVToolNix

作为一组工具检查：

```text
mkvmerge
mkvinfo
mkvextract
```

最低能力包括识别 MKV、读取封装信息和提取最小附件。

### FFmpeg / FFprobe

- FFmpeg 完成最小 PCM 解码；
- FFprobe 输出可解析轨道 JSON；
- 两者来自同一制品，但分别报告能力状态。

### assfonts

- 识别版本；
- 对最小 ASS 和受控字体执行子集化；
- 验证输出确实生成且可读取。

### KDocs

- 只在 `entrypoint=cli|skill` 且维护表能力需要时检查；
- `entrypoint=hub` 时不得解析路径、运行命令或显示状态；
- KDocs 缺失不影响未选择维护表能力的任务。

## 9. 配置写入

继续沿用现有单配置模型，不引入 Profile 系统。

要求：

- 保留未修改字段；
- 不写入下载 URL、检查缓存或完整环境；
- 使用临时文件和原子替换；
- 写入前后验证 JSON；
- 空值不能隐式清除当前有效路径；
- `use-path` 只能修改目标工具对应字段。

## 10. 第一阶段不实现自动安装

本轮不新增：

```text
tools install
tools update
tools rollback
```

但清单必须预留下一阶段所需字段：

- Windows x64 固定制品；
- Linux amd64、arm64 制品或完整镜像说明；
- 下载大小和 SHA-256；
- 解压格式与可执行文件相对路径；
- 许可证。

第一阶段 CLI 只展示这些信息，不下载制品。

## 11. 文件修改范围

### 新增文件

```text
toolchain/manifest.json
toolchain/manifest.schema.json
scripts/toolchain.py
scripts/tests/test_toolchain.py
docs/toolchain-manifest-cli-implementation-plan.md
```

### 修改文件

```text
scripts/workflow.py
config.example.json
README.md
SKILL.md
references/environment.md
docs/requirements.md
.github/workflows/quality.yml
```

如果 otf2ttf 在本轮内部化，只修改实际字体恢复调用位置和对应测试，不借此重构无关字幕流程。

## 12. 测试方案

### 清单测试

- Schema 校验；
- 工具 ID 唯一；
- 制品平台与架构组合唯一；
- 所有受管制品都有 SHA-256；
- URL 字段类型和协议受限；
- Hub 投影不含 KDocs；
- 相同输入产生确定性输出。

### CLI 测试

- `list` 在工具全部缺失时仍成功；
- `check` 正确分派能力检查；
- 指定单项工具不会检查其他工具；
- 未知工具、入口、平台和 Schema 明确失败；
- JSON 输出稳定且不包含敏感信息；
- `use-path` 拒绝相对路径；
- 检查失败不修改配置；
- 检查成功只修改目标字段；
- 中文、空格和 `&` 路径使用参数数组；
- Hub 入口无法选择或导出 KDocs。

### 暂缓测试

以下测试继续标记为跳过，不作为本轮阻塞项：

- 真实 FFmpeg/FFprobe PCM 分析；
- 真实 assfonts 字体子集化；
- 真实内封字幕和附件提取；
- 真实 Linux amd64/arm64 工具制品；
- 完整 BDRip 媒体任务。

暂缓测试必须显示明确 `skipped` 原因，不能报告为已通过。

## 13. 文档更新

### `docs/requirements.md`

增加：

- 清单是唯一工具事实来源；
- CLI、Skill、Hub 使用入口投影；
- Hub 不包含 KDocs；
- 第一阶段只提供查看、检查、已有路径和导出；
- 自动安装属于后续阶段；
- 未选择的能力不检查对应工具。

### `references/environment.md`

说明新命令、路径写法、Hub 导出方式、KDocs 入口限制，以及缺少工具时的官方下载入口。

### `README.md`

只保留面向用户的简介和命令示例，不展开内部 Schema。

### `SKILL.md`

说明工具信息来自统一清单、媒体任务继续按能力检查工具、Hub 不包含 KDocs，并且查看清单不会自动下载工具。

## 14. 实施顺序

1. 固定清单 Schema 和入口范围；
2. 迁移当前工具说明、URL、制品和 SHA-256；
3. 实现加载、校验和过滤；
4. 实现 `tools list --json`；
5. 实现 `tools check --json`；
6. 实现 `tools use-path` 和原子配置写入；
7. 实现 `tools export --entrypoint hub|skill`；
8. 补齐纯 Python 回归测试；
9. 更新正式需求和使用说明；
10. 执行公开测试、现有完整测试、Ruff 和 `git diff --check`；
11. 逐文件检查提交内容；
12. 提交 GitHub 并等待 Windows/Linux CI；
13. 固定版本或标签，供 Hub 后续导入。

## 15. 验收标准

- `manifest.json` 是唯一人工维护的工具信息来源；
- CLI、Skill 和 Hub 投影使用相同工具与能力 ID；
- Hub 投影完全不包含 KDocs；
- CLI/Skill 仍可按需使用 KDocs；
- `list`、`check`、`use-path`、`export` 返回稳定 JSON；
- 工具缺失时仍能查看主页和官方下载地址；
- 路径检查失败不会损坏配置；
- 未选择的工具不会被检查；
- 第一阶段不会自动下载或安装工具；
- 不修改媒体工作流、确认关卡和清理安全规则；
- 公开测试、现有完整测试、Ruff、Windows/Linux CI 和差异检查通过；
- 暂缓集成测试被准确标记；
- 提交不包含凭据、配置、缓存、数据库、日志或本机构建产物。

## 16. 后续阶段

本方案完成并发布固定版本后，下一任务才由 Bangumi Media Hub 导入生成清单并删除自身手写工具常量，继续保持不调用 archive 媒体工作流。

再后续才考虑：

- `tools install`、更新和回退；
- Linux 完整工具镜像；
- 恢复真实工具和媒体集成测试；
- BDRip 任务执行及状态回传。

## 17. 本轮实施结果

- 已新增唯一工具清单、JSON Schema 和 `scripts/toolchain.py`；MediaInfo、MKVToolNix、FFmpeg/FFprobe、assfonts 的说明、固定 Windows x64 制品和 SHA-256 已从 Hub 当前实现迁入清单。
- 已实现 `tools list/check/use-path/export`，错误包含稳定 `code`；检查结果使用 `ready`、`missing`、`not_configured`、`unsupported_platform`、`capability_failed`、`needs_recheck`。
- `use-path` 只接受绝对路径，先执行完整最低能力检查，再原子更新目标工具字段；回读或复检失败时按原字节恢复配置。
- Hub 投影沿用当前 Hub 可消费的 `id`、`download_url`、`capabilities` 和 `artifacts` 形态，并自动排除 KDocs 与 otf2ttf；CLI/Skill 投影继续保留二者。
- otf2ttf 本轮未内部化，继续作为 CLI/Skill 外部工具，未改动字幕恢复算法。
- 已新增 21 项纯 Python 工具链回归测试；当前完整本地结果为 306 项通过、5 项真实工具或媒体集成测试按原计划跳过，Ruff 与差异检查通过。
- Bangumi Media Hub 工作区仅用于只读兼容核对，没有修改；其常量迁移和 API/UI 适配仍按本方案第 16 节在 archive 固定版本发布后进行。
