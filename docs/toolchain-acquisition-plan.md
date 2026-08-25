# archive-plex-anime 工具获取与统一清单方案

> 状态：待实施方案，不代表当前 CLI 已具备自动安装能力。
>
> 当前 `docs/requirements.md` 仍要求用户自行安装工具，并明确不引入自动安装器。实施本方案时必须同步更新正式需求基线、配置示例和验收要求。

## 1. 目标

为 `archive-plex-anime` CLI、独立 Skill 和 Bangumi 媒体中心提供同一套工具说明、检测规则和受管安装信息，解决以下问题：

- 用户可以直接看到缺少什么工具、工具有什么用途以及从哪里获取；
- 已有工具的用户可以继续配置绝对路径，不被强制重复下载；
- 支持的平台可以由 CLI 安装固定且经过校验的工具制品；
- Windows 与 Linux 使用同一套能力定义，但采用符合各自部署环境的安装方式；
- CLI、Skill 和媒体中心不再分别维护工具版本、下载地址和校验值；
- 工具下载失败、校验失败或能力检查失败时，不破坏当前可用版本。

本方案只负责 BDRip 工作流依赖的工具获取与验证，不改变 TV、Movie、字幕、封装、归档、确认和清理规则。

## 2. 核心决定

### 2.1 CLI 是唯一事实来源

工具发行清单由 `archive-plex-anime` CLI 项目维护，建议位置：

```text
src/archive_plex_anime/toolchain/manifest.json
```

以下入口使用同一份清单：

```text
archive CLI 的 manifest
        ↓ 发布时生成或复制
CLI 发布包 / Skill 发布包 / 媒体中心构建副本
```

- CLI 项目中的清单是唯一允许人工修改的源文件；
- Skill 需要独立分发时，可以携带发布阶段生成的只读副本；
- Bangumi 媒体中心在构建时固定导入指定 CLI 版本的清单；
- 不允许 Skill 或媒体中心手工维护第二套版本、URL 或 SHA-256；
- 不在运行时联网获取远端清单，避免上游变化直接改变本地行为；
- CI 必须验证生成副本与源清单的版本或摘要一致。

媒体整理规则仍只归 `archive-plex-anime` 所有。媒体中心只消费工具状态和安装信息，不复制归档规则。

### 2.2 URL 分为三类

每项工具必须区分：

- `project_url`：项目主页，用于了解项目和源码；
- `download_page_url`：官方手动下载页面，用于用户自行安装；
- `artifact_url`：CLI 自动安装时使用的固定版本制品。

可选增加：

- `binary_source_url`：第三方可信构建来源，例如 FFmpeg 的 Windows/Linux 构建项目；
- `license_url`：当前固定版本对应的许可证说明。

只有 `artifact_url` 可以用于自动下载，而且必须同时绑定：

- 固定版本；
- 操作系统；
- CPU 架构；
- 文件名和压缩格式；
- 文件大小；
- SHA-256；
- 解压后的可执行文件位置；
- 最低能力检查。

禁止以下行为：

- 运行时抓取网页中的“最新版”链接；
- 根据重定向结果自动信任未知制品；
- 接受用户输入的任意下载 URL；
- 跳过校验直接启用下载文件；
- 因新版本安装失败而覆盖当前可用工具。

## 3. 工具范围

| 工具 | 用途 | 项目主页 | 手动下载页面 | 管理方式 |
|---|---|---|---|---|
| MediaInfo CLI | 读取视频、音频和字幕轨道资料 | <https://github.com/MediaArea/MediaInfo> | <https://mediaarea.net/en/MediaInfo/Download> | 独立制品或用户路径 |
| MKVToolNix | MKV 识别、附件提取、检查和重新封装 | <https://codeberg.org/mbunkus/mkvtoolnix> | <https://mkvtoolnix.download/downloads.html> | 整套安装，检查 `mkvmerge`、`mkvinfo`、`mkvextract` |
| FFmpeg / FFprobe | 原盘音轨读取、PCM 解码和轨道检查 | <https://ffmpeg.org/> | <https://ffmpeg.org/download.html> | 同一制品安装，分别检查两项能力 |
| FFmpeg 构建来源 | 提供可固定版本的 Windows/Linux 构建 | <https://github.com/BtbN/FFmpeg-Builds> | <https://github.com/BtbN/FFmpeg-Builds/releases> | 仅在清单固定版本、架构和 SHA-256 后使用 |
| assfonts | ASS 字幕字体子集化 | <https://github.com/wyzdwdz/assfonts> | <https://github.com/wyzdwdz/assfonts/releases> | 独立制品或用户路径 |

### 3.1 FFmpeg 与 FFprobe

FFmpeg 和 FFprobe 来自同一个安装制品，不拆成两个下载任务，但必须分别检查：

- FFmpeg 是否能完成 PCM 解码；
- FFprobe 是否能读取媒体轨道并输出可解析 JSON。

### 3.2 otf2ttf

当前工作流只在 assfonts 失败、明确命中 OTF/CFF 字体候选时才需要 `otf2ttf`。CLI 改造必须在以下方案中明确选择一种：

1. 推荐方案：作为 CLI 自身的 Python 字体转换能力随程序发布，不再要求用户单独下载外部可执行文件；
2. 兼容方案：继续作为外部工具，并像其他工具一样进入固定发行清单。

无论选择哪一种，都必须保留原有触发条件和能力测试，不能因为工具整合而静默删除 OTF/CFF 恢复能力。

### 3.3 Python 与 fontTools

- 使用普通 Python 包安装时，Python 和 Python 依赖由 CLI 安装方式负责，不作为媒体工具重复下载；
- 使用独立可执行程序发布时，运行时依赖随 CLI 打包；
- 工具清单只报告运行能力，不为系统安装第二套 Python。

### 3.4 KDocs

KDocs 只从 Hub 版本排除，现有 CLI 和独立 Skill 能力继续保留：

- CLI 源清单和 Skill 发布副本继续描述、配置和检查 KDocs；
- CLI/Skill 选择 `kdocs-tracker` 时继续执行维护表更新，并把结果纳入最终确认与清理条件；
- Hub 生成清单和能力投影必须过滤 KDocs，不展示、不配置、不下载、不检查、不执行；
- Hub 的预置工作流也不得隐式补回 KDocs；
- 三个入口仍使用同一能力 ID 和规则核心，入口可见范围由统一目录决定，不复制媒体规则。

## 4. 清单结构

建议使用版本化 JSON Schema。单项制品至少包含：

```json
{
  "schema_version": 1,
  "manifest_version": "2026.08.1",
  "tools": [
    {
      "tool_id": "ffmpeg",
      "name": "FFmpeg / FFprobe",
      "version": "固定版本",
      "platform": "windows",
      "architecture": "x64",
      "project_url": "https://ffmpeg.org/",
      "download_page_url": "https://ffmpeg.org/download.html",
      "binary_source_url": "https://github.com/BtbN/FFmpeg-Builds/releases",
      "artifact_url": "固定版本制品 URL",
      "filename": "固定文件名",
      "sha256": "固定 SHA-256",
      "size": 0,
      "archive_format": "zip",
      "license": "LGPL-2.1-or-later",
      "executables": {
        "ffmpeg": "相对路径",
        "ffprobe": "相对路径"
      },
      "capability_checks": [
        "pcm_decode",
        "probe_json"
      ]
    }
  ]
}
```

建议完整字段：

```text
schema_version
manifest_version
tool_id
name
purpose
version
platform
architecture
project_url
download_page_url
binary_source_url
artifact_url
filename
sha256
size
archive_format
license
executables
capability_checks
```

同一工具可以有多个平台制品，但能力名称和用户可见用途必须保持一致。

## 5. CLI 设计

### 5.1 查看工具

```text
archive-plex-anime tools list
archive-plex-anime tools list --json
```

输出内容：

- 工具名称和用途；
- 当前来源：受管工具、用户工具或未安装；
- 当前版本和实际路径；
- 当前平台是否支持自动安装；
- 项目主页和官方下载页面；
- 缺少工具时对当前任务的实际影响。

即使工具尚未安装，也必须正常返回项目和下载链接。

### 5.2 检查工具

```text
archive-plex-anime tools check
archive-plex-anime tools check --tool ffmpeg
archive-plex-anime tools check --json
```

检查不能只执行 `--version`，必须验证真实能力：

- MediaInfo：读取最小媒体样本并返回可解析 JSON；
- MKVToolNix：识别 MKV、读取封装信息、提取最小附件；
- FFmpeg：完成最小 PCM 解码；
- FFprobe：读取轨道并返回可解析 JSON；
- assfonts：执行最小字幕字体子集化；
- otf2ttf：若保留该能力，验证一次受控的 OTF/CFF 转换。

工具检查结果使用稳定状态：

```text
ready
missing
not_configured
unsupported_platform
capability_failed
needs_recheck
```

### 5.3 使用已有工具

```text
archive-plex-anime tools use-path <tool> <absolute-path>
```

- 只接受绝对路径；
- 保存前立即执行对应能力检查；
- 目录型工具必须找到该工具组全部必要程序；
- 失败时不覆盖当前有效配置；
- Windows 和 Linux 均支持使用已有工具；
- Docker 中的路径必须是容器内已挂载且可执行的路径。

### 5.4 安装受管工具

```text
archive-plex-anime tools install
archive-plex-anime tools install --tool ffmpeg
```

安装前必须显示：

- 即将安装的工具和固定版本；
- 预计下载量和磁盘占用；
- 安装目录；
- 第三方许可证；
- 当前平台支持情况。

只有用户明确确认后才下载，不因执行 `list`、`check`、`inspect` 或打开设置页而自动下载。

## 6. 平台边界

| 环境 | 首版支持 | 安装方式 |
|---|---:|---|
| Windows x64 | 是 | CLI 下载固定制品到用户数据目录，校验并原子启用 |
| Windows ARM64 | 暂不承诺 | 可使用用户已有工具；制品链完整验证后再开放自动安装 |
| Linux amd64 裸机 | 条件支持 | 有固定制品时安装到用户数据目录，也可使用系统工具路径 |
| Linux arm64 裸机 | 条件支持 | 有固定制品并完成真实验证后开放 |
| Linux Docker 基础镜像 | 不在容器内安装 | 提示切换完整工具镜像或挂载用户工具 |
| Linux Docker 完整工具镜像 | 是 | 工具随镜像构建，运行时只检查能力 |

Docker 环境不得在运行中的容器执行 `apt install` 或临时修改系统目录。容器重建后仍必须获得同一版本的工具链。

完整工具镜像需要固定：

- 镜像标签；
- 镜像摘要；
- 工具清单版本；
- Linux 架构；
- 每项工具实际版本和能力检查结果。

## 7. 下载、校验与回退

受管安装流程固定为：

1. 识别系统和架构；
2. 从内置清单选择固定制品；
3. 下载到临时目录，展示进度并支持有限重试；
4. 校验文件大小和 SHA-256；
5. 解压到新的版本目录；
6. 检查预期可执行文件路径；
7. 执行真实能力测试；
8. 全部通过后原子切换当前版本；
9. 保留上一版本用于回退；
10. 清理失败的临时文件。

以下任一情况发生时必须保留当前版本：

- 下载中断；
- URL 返回意外内容；
- 文件大小不符；
- SHA-256 不符；
- 解压失败；
- 磁盘空间不足；
- 可执行文件缺失；
- 能力测试失败；
- 切换当前版本失败。

允许配置下载代理，但代理只改变网络路径，不能改变制品 URL、版本或校验值。

## 8. 按任务检查与安装

工具清单是全量能力目录，但工作流继续只检查当前任务实际需要的能力：

- 普通媒体识别需要 MediaInfo；
- MKV 检查、附件提取或重新封装需要 MKVToolNix；
- 存在外挂或提取 ASS 且进入字幕处理时需要 assfonts；
- assfonts 失败并命中 OTF/CFF 候选时才需要 otf2ttf 能力；
- Movie 原盘音轨洗版才需要 FFmpeg/FFprobe；
- 不涉及某能力时，不应因其缺失阻塞当前任务。

“安装全部 BDRip 工具”是用户主动选择的便捷操作，不得成为所有任务的强制前置条件。

## 9. 与 Bangumi 媒体中心的连接

媒体中心后续应删除手工维护的工具描述和 Windows 制品常量，改为消费指定 CLI 发布版本生成的清单。

媒体中心可以提供：

- 工具状态展示；
- 项目主页和官方下载入口；
- 用户工具路径配置；
- Windows 受管安装入口；
- Linux 完整镜像指引；
- 安装进度和能力检查结果。

媒体中心不得负责：

- 重新定义 BDRip 工具是否必要；
- 复制归档工作流规则；
- 修改 CLI 的工具能力判断；
- 在运行时从远端获取未经固定的清单；
- 接受任意下载 URL。

## 10. Skill 独立能力

Skill 版本继续作为一项独立能力保留，但工具信息来自同一源：

- CLI 已安装时，Skill 调用 `tools list/check --json` 获取当前状态；
- CLI 未安装但 Skill 作为独立包运行时，读取发布阶段生成的只读清单副本；
- Skill 不自行维护版本、URL、SHA-256 或另一套能力检查；
- Skill 包构建时校验清单版本与 CLI 发布版本一致；
- Skill 的媒体规则和安全确认仍按现有规则执行，不因 CLI 化而降低确认门槛。

## 11. 迁移步骤

建议由改造 `archive-plex-anime` CLI 的会话按以下顺序实施：

1. 建立版本化清单和 JSON Schema；
2. 将现有工具标识、用途、路径字段和能力要求迁入统一模型；
3. 保留 CLI/Skill 的 KDocs 代码、配置、文档和测试，并在 Hub 能力投影中过滤 KDocs；
4. 明确 otf2ttf 的内置依赖或外部工具方案；
5. 实现 `tools list --json`；
6. 实现 `tools check --json` 和真实能力测试；
7. 实现 `tools use-path`，保留用户已有工具模式；
8. 实现 Windows x64 受管安装和失败回退；
9. 完成 Linux amd64、Linux arm64 的裸机与容器验证；
10. 为 Skill 发布包生成只读清单副本；
11. 让 Bangumi 媒体中心在后续改造中消费生成清单；
12. 更新正式需求、配置示例、用户说明和发布校验；
13. 删除旧的重复工具常量和不再使用的配置字段。

不建议先实现下载器再补清单，因为这会让版本、平台、校验和能力模型在代码中继续分散。

## 12. 验收要求

### 清单与来源

- CLI、Skill 和媒体中心展示的工具名称、版本、链接和能力一致；
- 只有 CLI 源清单需要人工维护；
- 所有自动下载制品均固定版本、平台、架构和 SHA-256；
- 工具缺失时仍能查看项目主页和官方下载页面；
- KDocs 在 CLI 与 Skill 的工具、配置和任务路径中保持一致；Hub 生成清单和能力目录中不存在 KDocs。

### 安装与回退

- Windows x64 可以安装完整受管工具链；
- Linux Docker 基础镜像不会在运行时安装系统包；
- Linux 完整镜像包含与清单一致的工具版本；
- 下载、校验、解压、空间或能力检查失败时保留原版本；
- 用户已有工具路径检查失败时不覆盖有效配置；
- 下载代理不会绕过固定 URL 和 SHA-256。

### 真实能力

- MediaInfo 能读取真实媒体资料；
- MKVToolNix 能识别、检查、提取和重新封装 MKV；
- FFmpeg/FFprobe 能完成 PCM 解码和轨道读取；
- assfonts 能在中文路径下完成字幕字体子集化；
- otf2ttf 能力在真实 OTF/CFF 恢复场景中通过；
- 中文路径、空格、`&` 和非 ASCII 文件名均使用参数数组安全调用。

### 平台验证

- Windows x64、Linux amd64、Linux arm64 分别执行真实工具测试；
- 未完成真实验证的平台不标记为正式支持；
- Windows ARM64 在制品链不完整时只显示手动路径模式；
- FFmpeg 与 FFprobe 作为同一制品安装、分别报告能力状态。

## 13. 非目标

本方案不处理：

- ani-rss 下载和 WebRip 整理流程；
- Bangumi 收藏、观看同步或作品映射；
- 新的媒体扫描器；
- BDRip 规则迁入 Bangumi 媒体中心；
- 运行时自动追踪上游最新版；
- 无确认下载全部工具；
- 在 Docker 运行容器内安装系统包；
- Hub 兼容或执行 KDocs（CLI/Skill 继续保留现有 KDocs 能力）。

## 14. 实施完成后的用户体验

用户最终可以按自己的环境选择：

- 只使用 Bangumi/ani-rss/观看同步，不安装 BDRip 工具；
- 使用现有 MediaInfo、MKVToolNix、FFmpeg 和 assfonts 路径；
- 在 Windows 中一次安装受管 BDRip 工具链；
- 在 Linux 中使用完整工具镜像；
- 在缺少单项能力时查看官方来源、补齐工具并重新检查。

系统只在当前任务真正需要某项工具时阻塞，并明确告诉用户缺少什么、如何补齐以及补齐后会完成什么。
