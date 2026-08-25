# 动画媒体归档自动化 Skill

用于在 Windows 上整理动画 TV 与 Movie 媒体，覆盖字幕与字体、MKV 封装、Movie 原盘音轨洗版、字幕 ZIP 累计归档、NAS 写入、Plex 维护表和最终清理。

当前由同一规则核心提供 CLI、独立 Skill 和 Hub 适配能力：四个原有工作流作为预置保留，也可按需选择公开能力并自动补齐依赖。CLI/Skill 保留 KDocs；Hub 入口不展示、不检查、不执行 KDocs。可用 `python scripts/workflow.py capabilities --entrypoint cli --branch tv` 查看能力目录。

新会话请先阅读：

- [需求基线](docs/requirements.md)：产品目标、长期规则、执行边界和验收口径；
- [Skill 入口](SKILL.md)：当前可执行工作流和强制规则；
- `references/`：TV、Movie、字幕、元数据、环境和具体步骤规则。

## 版本边界

本仓库是可跨机配置的公开项目，不包含本机凭据、运行缓存或私有媒体资料。本机实际执行版位于 `%CODEX_HOME%\skills\archive-plex-anime`；发布版与安装版应通过明确的比较、测试、打包和安装流程同步，不能直接互相覆盖。

执行实际媒体任务时，以当前已安装 Skill、当前本机配置和用户本次确认的决定为准。
