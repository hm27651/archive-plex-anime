# 环境与命令模板

## 单一配置

唯一配置：`%LOCALAPPDATA%\archive-plex-anime\config.json`。不使用 Profile、继承配置或任务级配置。压制组历史位于同目录 `release-groups.json`。元数据凭据不进入配置，只从 `ARCHIVE_TMDB_TOKEN` / `ARCHIVE_TMDB_API_KEY`、`ARCHIVE_TVDB_API_KEY` 和可选 `ARCHIVE_TVDB_PIN` 环境变量读取。

以下路径全部由配置提供，不在 Skill 中固定：

```text
TV 工作根：       paths.workRoot
Movie 工作根：    paths.movieWorkRoot
主力字体库：      paths.primaryFonts
完整字体库：      paths.fallbackFonts
完整字体索引：    paths.fallbackFontDatabase
assfonts 数据库： paths.assfontsDatabase
TV 字幕归档：     paths.subtitleArchiveRoot
Movie 字幕归档：  paths.movieSubtitleArchiveRoot
```

库位由 `storageTargets` 与 `plexLibraries` 映射。Anime1/2/3 和 Movie1/2/3 是维护表与计划器使用的逻辑库位名；实际盘符、NAS 路径和默认新归档库位由用户配置。

工具来自配置的 `tools`：Python、MediaInfo CLI、MKVToolNix (`mkvmerge`/`mkvinfo`)、assfonts、otf2ttf、ffmpeg/ffprobe、KDocs CLI。inspect 按所选能力检查：普通任务不检查 FFmpeg/FFprobe/mkvinfo；它们只用于 Movie 原盘音轨洗版。assfonts 只在本次选择字幕处理且存在外挂/提取 ASS 时要求；otf2ttf 只在 assfonts 实际失败并命中 OTF/CFF 候选后检查；KDocs 只在启用维护表且任务包含最终写入时要求。`paths.fallbackFontDatabase` 指向 `fc-subs.db`；未填时默认使用完整字体库根目录下的同名文件。

`metadata` 默认启用 TMDB 主来源、TVDB 辅助来源及 TMDB 季序。`metadata.proxy` 只应用于这两个 API；空值表示显式直连。API 不是 `tools` 外部可执行文件，不参与工具存在性检查。四个预置默认选择元数据；自定义能力未包含 `metadata` 时完全不读取上述凭据或访问 API。自定义最终输出同时关闭元数据时，必须在决定中提供明确的 `title`。

## 安装与跨机使用

将 Skill 目录复制到 `%CODEX_HOME%\skills\archive-plex-anime`；未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex\skills\archive-plex-anime`。然后由用户自行补齐环境和唯一配置；不提供自动安装器、Profile、路径探测或配置迁移。需准备：

- Python 3 与 `fontTools`；
- MediaInfo CLI、MKVToolNix、assfonts 和 otf2ttf；
- Movie 原盘音轨洗版所需的 FFmpeg/FFprobe；
- 启用 Plex 维护表时所需的 KDocs CLI；
- 默认元数据前检所需的 TMDB 凭据、可选 TVDB 凭据，以及按需配置的代理；
- 主力/完整字体库、`fc-subs.db`、assfonts 数据库、TV/Movie 工作根、字幕归档根、NAS 存储映射及 Plex 库位。

将 [config.example.json](../config.example.json) 的字段填入 `%LOCALAPPDATA%\archive-plex-anime\config.json`。工具和路径可与本机不同，但字段语义、单配置模式和工作流不变；缺少当前任务所需能力时由 inspect 列出，用户自行安装或修正配置后重跑。

## 统一 CLI

以下示例中的 `$python` 取配置 `tools.python`，`$workflow` 指向 `scripts\workflow.py`：

```powershell
# 查看某入口和分支可选择的能力/预置
& $python $workflow capabilities --entrypoint cli --branch tv

# 使用预置工作流
& $python $workflow init --work-dir $workDir --branch tv --task complete-archive --decisions-stdin

# 或使用自定义能力（与预置选择互斥）
& $python $workflow init --work-dir $workDir --branch tv --task complete-archive --capabilities remux,subtitle-package --decisions-stdin
& $python $workflow inspect --work-dir $workDir

# 用户前置确认后
& $python $workflow approve-preflight --work-dir $workDir --decisions-stdin
& $python $workflow subtitle --work-dir $workDir       # 仅当 selected_steps 包含
& $python $workflow movie-audio --work-dir $workDir   # 仅 Movie 原盘洗版
& $python $workflow remux --work-dir $workDir          # 仅当 selected_steps 包含
& $python $workflow package --work-dir $workDir        # 仅当 selected_steps 包含
& $python $workflow review --work-dir $workDir

# 用户确认本地产物及最终动作后
& $python $workflow approve-final --work-dir $workDir
& $python $workflow finalize --work-dir $workDir
& $python $workflow cleanup --work-dir $workDir
```

实际分支使用 `--branch tv` 或 `--branch movie`；模式使用 `complete-archive`、`replacement`、`archive-only`、`local-only`。只执行状态中的 `selected_steps`，不手工补步骤。新调用优先使用公开 `--capabilities`；不含最终输出的自定义选择自动按 local-only 规划。local-only 的兼容 `--steps` 只接受 `movie-audio`、`subtitle`、`remux`、`package`（`inspect` 自动执行）。Hub 调用必须增加 `--entrypoint hub`，其能力目录和任务状态不会包含 KDocs。

`cleanup` 会自动依赖 `video-delivery`，只有字幕 ZIP 或维护表输出时不得执行。未选择 `metadata` 的自定义最终输出必须通过 `--decisions-stdin` 提供 `title`。

用户决定仅支持 `--decisions-stdin`，不支持命令行 JSON 参数或人工计划文件。

## 开发验证

先安装固定版本的开发检查依赖：

```powershell
python -m pip install -r requirements-dev.txt
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
python -B -m unittest discover -s scripts/tests -p 'test_*.py'
ruff check --no-cache scripts
git diff --check
```

GitHub Actions 在 Python 3.11 的 Windows 与 Linux 环境执行相同测试和 Ruff 检查。测试和发布内容不得包含 `.pyc`、`__pycache__`、运行缓存、凭据或本机私有资料。

## PowerShell UTF-8 JSON 模板

使用重定向 stdin 的底层字节流，避免中文和引号被 PowerShell 改写。下例用于前置确认；`init` 时把 `$arguments` 改为对应的 `init ... --decisions-stdin` 参数。

```powershell
$utf8 = [System.Text.UTF8Encoding]::new($false)
$json = $decisions | ConvertTo-Json -Depth 20 -Compress
$bytes = $utf8.GetBytes($json)
$arguments = '"' + $workflow + '" approve-preflight --work-dir "' + $workDir + '" --decisions-stdin'

$start = [System.Diagnostics.ProcessStartInfo]::new()
$start.FileName = $python
$start.Arguments = $arguments
$start.UseShellExecute = $false
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardOutputEncoding = $utf8
$start.StandardErrorEncoding = $utf8

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $start
[void]$process.Start()
$process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
$process.StandardInput.Close()
$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw $stderr }
$stdout
```

`workflow.py` 与内部后端在入口处强制 stdout/stderr 使用 UTF-8；上述 Encoding 设置用于让 PowerShell 按相同编码读取，不依赖本机活动代码页。

通过参数数组或 `ProcessStartInfo` 传路径，不使用 `Invoke-Expression`、`shell=True` 或易破坏 `&`、中文和引号的命令拼接。

## 状态与输出

```text
用户产物：工作目录内标准名或 Windows 编号 ASS/MKV/ZIP
最小状态：工作目录\.archive-state.json
执行缓存：工作目录\.archive-temp\execution-cache.json
日志：    工作目录\.archive-logs
```

所有 JSON、日志和 Markdown 使用 UTF-8；最终 ASS 使用 UTF-8-SIG；子进程输出严格按 UTF-8-SIG、UTF-8、GB18030 解码，失败即停止。
