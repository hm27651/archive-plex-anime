# 动画媒体归档自动化 Skill

用于在 Windows 上整理动画 TV 与 Movie 媒体，覆盖字幕与字体、MKV 封装、Movie 原盘音轨洗版、字幕 ZIP 累计归档、NAS 写入、Plex 维护表和最终清理。

当前由同一规则核心提供 CLI、独立 Skill 和 Hub 适配能力：四个原有工作流作为预置保留，也可按需选择公开能力并自动补齐依赖。CLI/Skill 保留 KDocs；Hub 入口不展示、不检查、不执行 KDocs。可用 `python scripts/workflow.py capabilities --entrypoint cli --branch tv` 查看能力目录。

工具名称、官方下载信息、固定制品与能力 ID 统一来自 `toolchain/manifest.json`。第一阶段 CLI 只查看、检查、验证已有路径和导出投影，不会自动下载或安装工具：

```powershell
python scripts/workflow.py tools list --entrypoint cli --json
python scripts/workflow.py tools check --entrypoint hub --tool ffmpeg --json
python scripts/workflow.py tools use-path mediainfo "D:\Tools\MediaInfo.exe" --json
python scripts/workflow.py tools export --entrypoint hub --output "D:\Temp\archive-tools-hub.json" --json
```

Hub 投影自动排除 KDocs 和内部字体转换工具；CLI 与独立 Skill 仍可按需使用 KDocs。

新会话请先阅读：

- [需求基线](docs/requirements.md)：产品目标、长期规则、执行边界和验收口径；
- [Skill 入口](SKILL.md)：当前可执行工作流和强制规则；
- `references/`：TV、Movie、字幕、元数据、环境和具体步骤规则。

## 版本边界

本仓库是可跨机配置的公开项目，不包含本机凭据、运行缓存或私有媒体资料。本机实际执行版位于 `%CODEX_HOME%\skills\archive-plex-anime`；发布版与安装版应通过明确的比较、测试、打包和安装流程同步，不能直接互相覆盖。

执行实际媒体任务时，以当前已安装 Skill、当前本机配置和用户本次确认的决定为准。
