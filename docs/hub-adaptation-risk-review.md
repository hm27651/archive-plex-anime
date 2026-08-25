# Hub 适配风险修复交接

> 审查基线：`9bc9c63 feat: 支持预置与按需能力选择`
>
> 状态：已修复，待提交与远端 CI 验证。本文同时记录修复结果和剩余交付项。

## 1. 范围与已确认决定

本次只检查 `archive-plex-anime` 的 Hub 入口能力投影、按需能力解析和清理安全边界。

KDocs 的产品决定已经确认：

- Bangumi Media Hub 版本不需要 KDocs；
- Hub 能力目录、配置、工具检查、任务计划和执行均不得出现 `kdocs-tracker`；
- CLI 与独立 Skill 可以继续把 KDocs 作为可选能力保留；
- 因此“CLI/Skill 仍包含 KDocs”不属于本次风险，也不需要在本轮删除。

当前代码继续保持 Hub 目录隐藏 KDocs，并且 Hub 前检不会主动检查 KDocs CLI。本轮已完成两项运行风险修复，并补齐公开测试与可复现静态检查配置；提交和远端 CI 尚未执行。

## 2. P1：没有完成视频入库也能删除源任务目录

### 当前行为

任务目录同时是源目录和工作目录，`cleanup` 最终会删除整个任务目录。

当前能力解析把以下三项都视为足以支持清理的“最终能力”：

```text
video-delivery
subtitle-delivery
kdocs-tracker
```

因此以下自定义选择可以无错误解析：

```text
kdocs-tracker + cleanup
subtitle-delivery + cleanup
```

实际解析结果分别为：

```text
inspect → review → finalize → cleanup
inspect → package → review → finalize → cleanup
```

第一种情况只更新维护表，第二种情况只归档字幕 ZIP；两种情况都没有把视频安全写入媒体库，但清理步骤仍可删除包含源视频的整个任务目录。

### 根因

- `scripts/capabilities.py` 中 `FINAL_CAPABILITIES` 同时包含视频、字幕 ZIP 和 KDocs；
- `resolve_capabilities()` 只检查 `cleanup` 是否与任意最终能力同时存在；
- `scripts/steps/cleanup.py` 只验证当前计划中已有输出的检查点；
- 当计划没有视频输出时，清理步骤不会要求视频检查点，最终仍执行目录删除。

### 修复要求

必须同时增加解析层和执行层两道保护。

#### 能力解析层

推荐把 `video-delivery` 设置为 `cleanup` 的必要依赖：

```text
cleanup requires inspect + video-delivery
```

期望行为：

- 用户选择 `cleanup` 时自动补齐 `video-delivery`；
- 如果当前媒体计划无法生成视频入库任务，返回明确的 `CAPABILITY_UNAVAILABLE` 或专用安全错误；
- `subtitle-delivery` 或 `kdocs-tracker` 单独存在时不能满足清理条件；
- Hub 因为不暴露 KDocs，不需要 KDocs 特殊分支。

#### 清理执行层

执行 `shutil.rmtree()` 前必须再次验证：

1. `final_sinks` 明确包含 `video`；
2. `finalPreparation.final.video` 至少包含一个实际视频任务；
3. 每个视频任务都有属于当前批次的完成检查点；
4. 源文件和目标文件大小仍与检查点一致；
5. 视频目标位于任务目录外；
6. 任一条件不满足时返回 `FAILED`，并保留任务目录。

不能只依赖前端、Hub 参数或能力解析结果，因为旧状态、损坏状态或直接调用步骤都可能绕过上一层。

### 必测场景

- `kdocs-tracker + cleanup`：不能在没有视频入库时进入清理；
- `subtitle-delivery + cleanup`：不能在没有视频入库时进入清理；
- `cleanup` 单独选择：自动补齐视频入库，或明确拒绝；
- `video-delivery + cleanup`：视频入库检查点完整时允许清理；
- 视频目标缺失、大小变化或检查点属于旧批次：拒绝清理；
- 清理被拒绝后，任务目录及源视频仍然存在；
- TV、Movie、`complete-archive`、`replacement` 和 `archive-only` 均执行相同安全门槛。

### 验收标准

任何会删除任务目录的流程，都必须能够证明当前任务中的所有源视频已经安全写入任务目录外的正式媒体目标。字幕 ZIP 或 KDocs 成功不能替代视频入库证明。

## 3. P2：未选择“元数据识别”仍会执行元数据模块

### 当前行为

`metadata` 已作为独立公开能力出现在能力目录中，但前检仍无条件调用 `metadata_inspector()`。

因此 Hub 即使只选择以下本地能力：

```text
subtitle
remux
subtitle-package
```

仍可能访问 TMDB/TVDB，或者因为元数据凭据、网络和匹配问题产生额外等待或阻塞。

这会造成两类体验问题：

- 用户没有选择元数据能力，却发生了额外联网；
- 本来可以离线完成的字幕或 remux 任务，被元数据错误阻塞。

### 根因

- `selected_capability` 目前只参与工具检查；
- 元数据缓存键会计算 `metadata_enabled`，但无论是否选择 `metadata`，最终都会调用 `metadata_inspector()`；
- 能力目录与实际执行行为不一致。

### 推荐语义

公开能力应当决定真实行为：

- 选择 `metadata`：执行现有 TMDB/TVDB 识别和缓存逻辑；
- 未选择 `metadata`：不构造元数据客户端、不读取凭据、不发起网络请求，返回稳定的 `OFF` 结果；
- 本地字幕、remux 和打包不应自动依赖元数据；
- 如果视频入库、字幕归档或维护表确实需要作品标题，必须在能力依赖中显式自动补齐 `metadata`，或者要求用户提供已经确认的标题，不能靠前检暗中执行。

为保持默认预置行为不变，四个现有预置仍可继续包含 `metadata`。变化只影响明确使用自定义能力选择的任务。

### 修复要求

1. 从 `selected_capability` 计算 `metadata_selected`；
2. 只有 `metadata_selected` 为真时执行 `metadata_inspector()`；
3. 未选择时写入稳定、脱敏的 `OFF` 结果，不进行网络和凭据读取；
4. 元数据组件缓存键必须包含是否选择元数据，切换选择后不能复用错误缓存；
5. `available_capabilities()` 只有在元数据确实执行且结果可用时才报告 `metadata` 可用；
6. 若最终输出依赖元数据，应由 `resolve_capabilities()` 自动补齐，并在 `auto_added_capabilities` 中向 Hub 说明；
7. 继续保持确认后不重新联网的现有边界。

### 必测场景

- Hub 自定义 `remux`：`metadata_inspector` 调用次数为 0；
- Hub 自定义 `subtitle`：不访问 TMDB/TVDB；
- 显式选择 `metadata`：执行一次元数据识别；
- 从未选择切换为选择：元数据组件重新执行；
- 从选择切换为未选择：元数据状态变为 `OFF`，不复用旧匹配结果；
- 默认四个预置的现有元数据行为保持不变；
- 如果最终输出自动补齐元数据，状态中准确记录 `auto_added_capabilities`；
- 元数据关闭时，纯本地任务在没有 API 凭据和网络的环境中仍可完成。

### 验收标准

能力目录中未选择 `metadata` 时，运行日志、网络调用和测试桩都必须证明元数据客户端没有被调用。

## 4. P3：Hub 适配测试没有进入 GitHub

### 当前状态

本机存在 `scripts/tests/test_capabilities.py`，并覆盖：

- Hub 目录不暴露 KDocs；
- Hub 工具检查不要求 KDocs；
- 未选择的最终输出被过滤；
- 重新初始化选择会清空旧进度与确认；
- 能力依赖和非法组合。

但整个 `scripts/tests/` 当前被 `.git/info/exclude` 排除，所以这些测试没有进入提交 `9bc9c63`，GitHub 无法独立复现本次 Hub 适配验证。

### 风险

- 其他机器或新会话只能获得实现代码，拿不到对应回归测试；
- 后续修改能力解析、清理或 KDocs 投影时容易发生静默回归；
- GitHub CI 无法执行本次新增测试；
- 本机“280 项测试通过”不能作为远端发布包可复现的证明。

### 推荐处理

至少把与本次公开能力和安全边界直接相关的测试纳入 Git：

```text
scripts/tests/test_capabilities.py
清理安全新增测试
元数据门控新增测试
Hub 入口端到端最小流程测试
```

如果项目仍希望把真实媒体样本、机器路径或大型集成夹具留在本地，可以继续忽略这些私有夹具，但不应连纯 Python 规则测试一起排除。

提交前确认测试文件不包含：

- 本机绝对媒体路径；
- Token、API Key 或密码；
- 真实数据库、日志和缓存；
- 大型媒体样本；
- 当前机器专用配置。

### 验收标准

从 GitHub 全新克隆后，无需本机私有资料即可执行能力解析、Hub 投影、清理拒绝和元数据门控测试。

## 5. P4：静态检查环境不可复现

### 当前状态

审查环境已经完成：

- 280 项本地测试通过；
- 5 项真实工具或媒体路径集成测试跳过；
- `git diff --check` 通过；
- Python 模块可编译；
- GitHub `origin/main` 与本地提交一致。

但当前环境没有 Ruff，无法执行静态检查。

### 推荐处理

- 在开发依赖或文档中固定 Ruff 版本；
- 提供统一检查命令；
- 在 GitHub CI 中执行同一命令；
- CI 使用 `PYTHONDONTWRITEBYTECODE=1` 和 `PYTHONUTF8=1`；
- 检查结束后不得把 `.pyc`、`__pycache__` 或运行缓存打入提交和发布包。

建议最小验证命令：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
python -B -m unittest discover -s scripts/tests -p 'test_*.py'
ruff check --no-cache scripts
git diff --check
```

### 验收标准

Windows 与 Linux 的干净环境可以使用文档中的固定命令得到一致结果，并且检查过程不会污染工作区。

## 6. 不属于本轮修复范围

- 不删除 CLI 或独立 Skill 的可选 KDocs 能力；
- 不让 Hub 显示、检查或执行 KDocs；
- 不把 archive 媒体整理规则复制进 Bangumi Media Hub；
- 不在当前 Bangumi Media Hub 中创建 BDRip 任务；
- 不实现工具下载清单或自动安装器；
- 不更新当前安装在 Codex 中的独立 Skill；
- 不部署或执行真实媒体清理。

## 7. 建议修复顺序

1. 先修复 `cleanup` 必须具有视频入库证明的 P1 风险；
2. 增加清理执行层的防御性检查和数据保留测试；
3. 修复元数据能力门控和缓存失效；
4. 增加 Hub 自定义能力的离线测试；
5. 将必要的纯 Python 回归测试纳入 Git；
6. 补齐 Ruff 与干净环境验证；
7. 重新执行全部测试并逐文件检查提交内容；
8. 修复提交合并到 GitHub 后，再考虑由 Bangumi Media Hub 消费该适配层。

## 8. 完成检查表

- [x] `cleanup` 不能由字幕 ZIP 或 KDocs 单独解锁；
- [x] 清理前必须存在当前批次的完整视频入库检查点；
- [x] 清理拒绝时源目录和源视频保持不变；
- [x] 未选择 `metadata` 时不会构造客户端或发起网络请求；
- [x] 元数据选择变化会正确失效缓存；
- [x] 默认预置行为保持不变；
- [x] Hub 能力目录仍不包含 KDocs；
- [x] Hub 工具检查和执行仍不触碰 KDocs；
- [ ] 关键 Hub 适配测试已提交 Git（当前已准备纳入本次待提交变更）；
- [ ] 全新克隆与远端 CI 已验证纯 Python 回归测试；
- [x] 本机 Ruff、完整测试和 `git diff --check` 通过；
- [x] 待提交范围不含凭据、缓存、数据库、日志或本机构建产物。

## 9. 本轮修复结果

- P1：`cleanup` 的能力依赖固定为 `inspect + video-delivery`；可用性过滤后若视频入库不可用，会移除清理并返回 `CLEANUP_VIDEO_DELIVERY_REQUIRED`。执行清理前再次要求 `final_sinks` 包含视频、最终计划含实际视频任务，并验证当前批次检查点和源/目标边界。
- P2：只有选择 `metadata` 才读取凭据并执行元数据识别；未选择时写入稳定 `OFF / CAPABILITY_NOT_SELECTED`，选择切换会失效缓存。关闭元数据的自定义最终输出必须显式提供标题。
- P3：能力、元数据增量前检和轻量工作流三份纯 Python 回归测试准备纳入 Git；继续排除真实媒体样本和本机私有夹具。
- P4：新增固定 Ruff 版本、最小正确性规则以及 Windows/Linux GitHub Actions。当前本机结果为 285 项通过、5 项跳过，Ruff 与差异空白检查通过。
- 版本：`STATE_SCHEMA=8`、`RULES_VERSION=19`、`BACKEND_CACHE_SCHEMA=19`、`PRESET_VERSION=2`，工作流修订为 `2026-08-25-hub-risk-fixes-v1`。
