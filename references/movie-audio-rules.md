# Movie 原盘音轨洗版

## 触发与统一步骤

用户提供已核实的原盘音频源，并要求保留压制版视频轨、改用原盘音轨时启用。原盘源可为 BDMV `STREAM` 下 M2TS，也可为从原盘提取的 MKV；只取其音频，不取视频、字幕、附件或 PGS。

本能力适用于 Movie `complete-archive` 与 `replacement`，由统一计划选择：

```text
inspect/PCM → 前置确认 → movie-audio → [subtitle] → remux → [package]
→ review → 最终确认 → finalize → cleanup
```

单文件使用一组 `video_source`/`disc_source`；Plex 堆叠作品使用 `movie_audio_pairs`，每组完整声明相同的 `cdN`、压制视频源和原盘音频源。不同分段不得交叉取轨。

## 前检匹配与同步

用户提供的原盘源视为已核实，不重新判断正片。每个分段从压制视频源选择 FLAC 参考音轨，按声道数、布局、语言、时长和顺序匹配唯一原盘音轨，再用 FFmpeg/FFprobe 做 3–5 个 PCM 采样点，确定约 50 ms 容差内的一致固定偏移。

无参考、候选歧义、非固定偏移或同分段多参考偏移冲突时停在前检。不转码、变速、拉伸或静默裁切。匹配和 PCM 每任务只在前检执行一次。

原盘源中的全部音轨默认保留，包括 FLAC；只自动剔除名称明确标注 Commentary、评论、解说、无障碍或伴奏的轨道。用户可用按原盘源相对路径作用的 `disc_audio_keep` 覆盖特殊轨选择。音轨按声道数从多到少排列，名称只写声道，第一条默认。

## 执行与产物

确认后，`movie-audio` 为每个分段生成内部中间 MKV：视频只取对应压制源，音频只取对应原盘源并应用已确认固定偏移。远程输入先复制到 `.archive-temp/movie-audio` 并比较大小。

中间封装接受 mkvmerge 返回码 0；返回码 1 仅在有效产物存在时记录为 `WARNING`，其他返回码或缺少产物均为 `FAILED`。随后由 Movie 通用 remux 应用压制组、音轨声道名、分段字幕、章节、附件和 PGS 删除规则。

本地最终 `电影名[.cdN].mkv` 与可选累计字幕 ZIP 经 review 验收后，finalize 写入唯一 Movie 库位。洗版覆盖正式同分段 MKV，新建归档创建完整分段集合；NAS 写入后只比较大小。
