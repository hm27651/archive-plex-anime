# Linux 便携工具制品

## 当前状态

`portable-tools-v1.0.2` 已发布 8 个固定制品：MediaInfo CLI、MKVToolNix、FFmpeg/FFprobe、assfonts 分别覆盖 Linux amd64 与 arm64。两种架构都在 GitHub 原生 runner 的全新 `python:3.11-slim-bookworm` 容器内运行了共享能力契约；arm64 不是 QEMU 或仅解压验证。

- Release：[portable-tools-v1.0.2](https://github.com/hm27651/archive-plex-anime/releases/tag/portable-tools-v1.0.2)
- amd64 报告：[capability-report-linux-amd64.json](https://github.com/hm27651/archive-plex-anime/releases/download/portable-tools-v1.0.2/capability-report-linux-amd64.json)
- arm64 报告：[capability-report-linux-arm64.json](https://github.com/hm27651/archive-plex-anime/releases/download/portable-tools-v1.0.2/capability-report-linux-arm64.json)
- 制品索引：[portable-tools-index.json](https://github.com/hm27651/archive-plex-anime/releases/download/portable-tools-v1.0.2/portable-tools-index.json)
- 仓库内 Hub 投影示例：`toolchain/archive-tools-hub.json`（固定到引入清单的 archive 完整提交）

| 工具 | Linux 版本 | 实际验收能力 |
|---|---:|---|
| MediaInfo CLI | 23.04 | 版本身份、媒体 JSON 读取 |
| MKVToolNix | 74.0.0 | 版本身份、MKV 识别、封装检查、附件提取 |
| FFmpeg / FFprobe | 5.1.9-0+deb12u1 | 版本身份、PCM 解码、音轨 JSON 读取 |
| assfonts | 0.7.3 | 版本身份、包内字体数据库构建、ASS 字体子集化 |

能力测试使用包含中文、空格和 `&` 的解压路径，并检查缺失动态库、入口位置和执行权限。清单中的大小与 SHA-256 来自已发布制品，不使用占位值或浮动“最新版”。

## 构建与发布

`.github/workflows/portable-tools-release.yml` 仅响应 `portable-tools-v*` 标签：

1. amd64 使用 `ubuntu-24.04`，arm64 使用 `ubuntu-24.04-arm`；
2. 在 `debian:bookworm-slim` 容器中按 `toolchain/linux-sources.json` 的固定 Debian snapshot、assfonts URL、提交和摘要构建；
3. 复制必要运行库，生成只设置包内环境的相对启动器；
4. 生成 mtime、所有者、顺序和 gzip header 均固定的 `tar.gz`；
5. 执行共享真实能力检查，生成架构报告；
6. 两种架构都通过后才创建不可变 Release。

运行时不会执行 `apt install`、联网补件、写入系统目录、扫描媒体库或创建 BDRip 任务。Hub 后续只负责按清单下载、校验、安全解压、持久化、原子启用和回退。

## 来源与许可证

- MediaInfo、MKVToolNix、FFmpeg/FFprobe 来自固定 Debian Bookworm snapshot；确切 Debian 包版本记录在每个制品的 `.tar.gz.json` 和 Release 索引中。
- assfonts 二进制来自官方 `v0.7.3` Release，固定到提交 `b1659f3cbd45d3eb2a45048ffc48b81a3e5dcfac`，上游摘要记录在 `toolchain/linux-sources.json`。
- 每个制品携带其直接程序和所带运行库的 Debian copyright 或上游 LICENSE/NOTICE。MediaInfo 为 BSD-2-Clause，MKVToolNix 为 GPL-2.0-or-later，assfonts 为 GPL-3.0；FFmpeg 的最终许可取决于构建配置和所带库，使用时应以制品内 Debian copyright 为准。
- KDocs 和 otf2ttf 不进入 Hub 便携制品或 Hub 投影。CLI 与独立 Skill 的现有 KDocs 能力不受影响。

## 与 Hub 的边界

这些制品完成的是 archive 阶段的“工具准备”。它们不会让 Hub 自动开始 BDRip，也不包含 TV、Movie、字幕、NAS、清理、KDocs 或 ani-rss 规则。Hub 必须在后续提交中固定本仓库完整提交并实现下载、校验、安全解压、`/app/data/archive-tools` 持久化、原子启用及失败回退后，才能称为 fnOS 基础版一键安装完成。
