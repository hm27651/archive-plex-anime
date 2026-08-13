# TMDB / TVDB 元数据

## 定位与默认值

元数据是 `inspect` 的默认辅助组件，不是新步骤或第二套工作流。TMDB 是主来源，TVDB 只做外部 ID、标题及用户指定季序的辅助核对。API 只产生标题、作品 ID、季集摘要和 `episode_map` 建议；用户在原有前置确认中接受或修改后，普通 TV/Movie 计划器消费这些决定，后续步骤不联网。

默认配置：`enabled=true`、`mode=auto`、`episodeOrder=tmdb`、`language=zh-CN`。用户可在任务决定的 `metadata` 中覆盖：

```json
{
  "metadata": {
    "mode": "auto",
    "query": "作品名",
    "tmdb_id": 123,
    "tmdb_type": "tv",
    "tvdb_id": 456,
    "language": "zh-CN",
    "episode_order": "tmdb"
  }
}
```

`mode` 可为 `auto`、`required`、`off`。用户明确要求依赖 API 整理时用 `required`；明确不联网时用 `off`。TVDB 季序只在用户要求时使用：`tvdb-official`、`tvdb-dvd`、`tvdb-absolute`、`tvdb-alternate` 或 `tvdb-regional`。

## 匹配与映射

查询优先级为显式 TMDB ID → `metadata.query` → 显式 `title` → 自动作品名。显式查询词和标题不清洗；自动模式只读取目录名及主 `.mkv`/`.mp4` 文件名，不读取媒体内容，也不让 MKA、ASS/SSA、BDMV 原盘目录或菜单、PV、NCOP/NCED、Trailer、Featurette 等附加内容参与作品名提取。

自动提取以多个主视频的共同标题骨架为主、目录名为交叉核对：按 NFKC 清理发布组、季集号、`cdN`、Windows 编号、年份技术边界、分辨率、编码、位深、音轨、来源和版本尾标；合法作品括号不做全局删除。干净目录名可直接采用；脏目录与主视频结果一致时采用共同标题；候选冲突或无法唯一提取时在联网前返回 `METADATA_QUERY_REQUIRED`，由用户在原有第一次确认中指定查询词。缓存和公开摘要只保存最终查询词、`querySource` 与少量 `queryCandidates`。

显式 TMDB ID 直接读取详情；搜索时仅唯一精确标题，或用户年份使精确标题唯一时自动选择。同名、多版本、媒体类型歧义必须在现有前置确认中选择。

标题优先级：用户显式 `title` → 用户确认的 TMDB 中文标题 → 原名 → 工作目录名。路径仍是 TV/Movie 分支的唯一判定依据；API 类型不改变分支。Movie 的 `cdN` 全部绑定同一个作品，不删除堆叠标记。

默认 TMDB 季序只拉取本地实际出现的季。动画各本地季属于不同 TMDB 系列时使用：

```json
{
  "metadata": {
    "season_bindings": {
      "S1": {"tmdb_id": 111, "tmdb_season": 1},
      "S2": {"tmdb_id": 222, "tmdb_season": 1}
    }
  }
}
```

只为文件名中明确存在 `SxxExx`、`第x话/集`、`EPxx`、`Exx`、`#xx` 或常见 `[xx]` 的本地视频/ASS 生成映射建议，且远端必须存在对应季集。仅文件数与 API 集数相同不得盲配；S0/OVA 无唯一编号或名称证据时由用户决定。用户显式 `episode_map` 永远优先。

## 网络与安全

TMDB/TVDB 客户端只使用配置中 `metadata.proxy`，不读取系统代理，也不为失败请求静默直连。示例配置留空表示显式直连；需要代理时由用户填写完整的 HTTP/HTTPS 代理地址。代理变化只失效元数据和受标题影响的库位组件。

凭据只从环境变量读取：

```text
ARCHIVE_TMDB_TOKEN
ARCHIVE_TMDB_API_KEY
ARCHIVE_TVDB_API_KEY
ARCHIVE_TVDB_PIN
```

TMDB Bearer Token 优先于 API key。TVDB 每次有效前检最多登录一次并仅在进程内复用 token。凭据、请求头、完整 URL 查询参数和原始响应不得写入配置、状态、执行缓存或日志；缓存中的代理只保留协议、主机和端口。

`auto` 中 TMDB 临时网络错误只产生 `WARNING`，本地规则继续；认证错误、候选歧义仍为 `NEEDS_USER`。`required` 中任何主来源失败均为 `NEEDS_USER`。TVDB 默认辅助失败只警告；用户选择 TVDB 季序后失败必须停止。HTTP 429/临时 5xx 最多重试两次并遵守短 `Retry-After`。

`inspect --rerun` 使用元数据决定、配置、凭据存在性和本地 MKV/MP4、字幕相对路径列表的签名复用结果；MKA 变化不失效元数据组件，其他输入未变化时不联网。前置确认后不再访问 API。
