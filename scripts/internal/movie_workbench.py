"""Movie source inventory and explicit multi-source, stream-copy plans.

The Hub transports decisions; this module owns media interpretation. No PCM
matching, time correction, or transcoding is performed by this workflow.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from archive_rules import artifact_output_root, movie_video_path, movie_video_relative, temporary_path
from internal.errors import WorkflowError
from internal.media_inspection import read_mkvmerge_json
from internal.signatures import canonical_metadata_digest, file_signature
from internal.subtitle_pipeline import parse_ass_font_requirements

MEDIA_SUFFIXES = {".mkv", ".mka", ".m2ts", ".mp4"}
LANGUAGE_ORDER = {"JASC": 0, "SC": 1, "JATC": 2, "TC": 3}


def source_path(work: Path, relative: str) -> Path:
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in {".", ".."} or ":" in p for p in rel.parts):
        raise WorkflowError("MOVIE_SOURCE_INVALID", "素材必须位于任务目录内。")
    root = work.resolve()
    path = root.joinpath(*rel.parts)
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if not cursor.exists():
            raise WorkflowError("MOVIE_SOURCE_MISSING", f"素材不存在：{relative}")
        if cursor.is_symlink() or (getattr(cursor.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise WorkflowError("MOVIE_SOURCE_INVALID", "素材不能使用符号链接或联接。")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise WorkflowError("MOVIE_SOURCE_MISSING", f"素材不存在或超出任务范围：{relative}")
    return path.resolve()


def signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _tool(config: dict, name: str) -> str:
    from internal.archive_backend import tool_path

    return tool_path(config, name)


def _extract(config: dict, source: Path, mux_id: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = target.with_suffix(".source.json")
    identity = {"file": signature(source), "track": mux_id}
    if target.is_file() and stamp.is_file() and json.loads(stamp.read_text(encoding="utf-8")) == identity:
        return
    completed = subprocess.run(
        [_tool(config, "mkvextract"), str(source), "tracks", f"{mux_id}:{target}"],
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 1} or not target.is_file():
        raise WorkflowError("MOVIE_SUBTITLE_READ_FAILED", f"无法读取内封字幕：{source.name}")
    stamp.write_text(json.dumps(identity), encoding="utf-8")


def _subtitle_labels(name: str) -> tuple[str, str]:
    match = re.search(r"(?:^|[. _-])(JASC|JATC|SC|TC)(?:[. _-])([^./\\]+)", name, re.I)
    return (match.group(1).upper(), match.group(2)) if match else ("", "")


def inspect_sources(work: Path, config: dict, selected_paths: list[str] | None = None) -> dict:
    root = work.resolve()
    if selected_paths is None:
        candidates = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if not name.startswith(".") and path.suffix.lower() in MEDIA_SUFFIXES | {
                    ".ass",
                    ".ssa",
                    ".srt",
                    ".sup",
                }:
                    candidates.append(path.relative_to(root).as_posix())
    else:
        candidates = sorted(set(selected_paths))
    result = []
    issues = []
    with tempfile.TemporaryDirectory(prefix="archive-movie-read-") as temp:
        for relative in candidates:
            path = source_path(root, relative)
            item: dict[str, Any] = {"id": relative, "path": relative, "signature": signature(path), "tracks": []}
            try:
                if path.suffix.lower() in MEDIA_SUFFIXES:
                    raw = read_mkvmerge_json(path, _tool(config, "mkvmerge"))
                    for track in raw.get("tracks", []):
                        props = track.get("properties", {})
                        key = f"uid:{props['uid']}" if props.get("uid") else f"mux:{track['id']}"
                        codec = str(props.get("codec_id") or track.get("codec") or "")
                        codec = {"AC-3": "A_AC3", "E-AC-3": "A_EAC3", "FLAC": "A_FLAC", "DTS": "A_DTS", "TrueHD": "A_TRUEHD", "AAC": "A_AAC"}.get(codec, codec)
                        normalized = {
                            "id": key,
                            "mux_id": track["id"],
                            "type": track["type"],
                            "codec": codec,
                            "codec_name": str(track.get("codec") or codec),
                            "language": props.get("language") or "und",
                            "title": props.get("track_name") or "",
                            "channels": props.get("audio_channels"),
                            "default": bool(props.get("default_track")),
                            "fonts": [],
                        }
                        if track["type"] == "subtitles":
                            normalized["subtitle_type"], normalized["group"] = _subtitle_labels(normalized["title"])
                            if "ASS" in codec.upper() or "SSA" in codec.upper():
                                extracted = Path(temp) / f"{len(result)}-{track['id']}.ass"
                                _extract(config, path, track["id"], extracted)
                                normalized["fonts"] = [
                                    {**f, "sources": [relative]} for f in parse_ass_font_requirements(extracted)
                                ]
                        item["tracks"].append(normalized)
                    item["kind"] = (
                        "video"
                        if path.suffix.lower() != ".mka" and any(t["type"] == "video" for t in item["tracks"])
                        else "audio"
                    )
                    item["chapters"] = bool(raw.get("chapters"))
                else:
                    label, group = _subtitle_labels(path.name)
                    item.update(kind="subtitle", subtitle_type=label, group=group, fonts=[])
                    if path.suffix.lower() in {".ass", ".ssa"}:
                        item["fonts"] = parse_ass_font_requirements(path)
                result.append(item)
            except Exception as exc:
                issues.append(
                    {"code": getattr(exc, "code", "MOVIE_SOURCE_READ_FAILED"), "source": relative, "detail": str(exc)}
                )
    inventory = {
        "schema_version": 1,
        "files": result,
        "issues": issues,
        "subtitle_type_order": list(LANGUAGE_ORDER),
        "read_only": True,
        "media_scanned": True,
        "task_state_changed": False,
    }
    for item in result:
        languages = {t["language"] for t in item["tracks"] if t["type"] == "audio" and t["language"] != "und"}
        item["audio_recommendations"] = {lang: recommend_audio(inventory, item["id"], lang) for lang in languages}
    return inventory


def audio_basis(target: dict, inventory: dict) -> dict:
    files = {f["id"]: f for f in inventory.get("files", [])}
    video = str(target.get("video") or "")
    refs = sorted(
        (str(r.get("source") or ""), str(r.get("track") or ""))
        for r in target.get("audio", [])
        if r.get("source") != video
    )
    return {
        "video": video,
        "video_signature": files.get(video, {}).get("signature"),
        "external": [{"source": s, "track": t, "signature": files.get(s, {}).get("signature")} for s, t in refs],
    }


def recommend_audio(inventory: dict, video: str, language: str = "jpn") -> list[dict]:
    """Recommendations for the selected video only; external matching is explicit."""
    file = next((f for f in inventory.get("files", []) if f["id"] == video), {})
    candidates = [
        t
        for t in file.get("tracks", [])
        if t["type"] == "audio"
        and t["language"] == language
        and not re.search(r"commentary|评论|解说", t["title"], re.I)
    ]
    return [{"source": video, "track": t["id"]} for t in sorted(candidates, key=lambda t: -(t.get("channels") or 0))]


def _safe_label(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or any(c in text for c in '/\\\x00\r\n:*?"<>|'):
        raise WorkflowError("MOVIE_LABEL_INVALID", f"{field}包含无效字符或为空。")
    return text


def build_movie_plan(work: Path, manifest: dict, decisions: dict, completed_steps=None) -> dict:
    """Build one normal remux job per selected video/part, with explicit streams."""
    config = json.loads(Path(manifest["configPath"]).read_text(encoding="utf-8"))
    movie = decisions.get("movie_plan") or {}
    if movie.get("schema_version") != 2:
        raise WorkflowError("MOVIE_PLAN_VERSION_INVALID", "Movie 处理方案版本无效。")
    targets = movie.get("targets") or []
    title = _safe_label(decisions.get("title"), "电影名称")
    release = str(decisions.get("release_group") or "").strip()
    paths = []
    for target in targets:
        paths.append(str(target.get("video") or ""))
        paths.extend(str(r.get("source") or "") for r in [*target.get("audio", []), *target.get("subtitles", [])])
    inventory = inspect_sources(work, config, paths) if paths else {"files": [], "issues": []}
    files = {f["id"]: f for f in inventory["files"]}
    issues = list(inventory["issues"])
    previous_inventory = manifest.get("discovery", {}).get("movieSourceInventory")
    if previous_inventory and {f["id"]: f["signature"] for f in previous_inventory["files"]} != {
        f["id"]: f["signature"] for f in inventory["files"]
    }:
        issues.append(
            {
                "code": "MOVIE_SOURCE_CHANGED",
                "stage_id": "video",
                "detail": "素材在统一检查后发生变化，请重新读取并检查。",
            }
        )
    else:
        manifest["discovery"]["movieSourceInventory"] = inventory
        manifest["discovery"]["movieSourceFiles"] = [
            file_signature(source_path(work, f["id"])) for f in inventory["files"]
        ]
    jobs, finals, groups, renames, entries, subtitle_inventory = [], [], {}, [], [], []
    output_root = artifact_output_root(work)
    seen_targets, seen_parts, seen_videos = set(), set(), set()

    def issue(code, target=None, field="", **details):
        issues.append(
            {
                "code": code,
                "target_id": (target or {}).get("id"),
                "stage_id": "audio"
                if "AUDIO" in code
                else "subtitles"
                if "SUBTITLE" in code or "FONT" in code
                else "video",
                "field": field,
                **details,
            }
        )

    def stream(ref, kind):
        f = files.get(ref.get("source"))
        if not f:
            return None
        t = next((t for t in f["tracks"] if t["id"] == ref.get("track") and t["type"] == kind), None)
        return (f, t) if t else None

    if not targets:
        issue("MOVIE_VIDEO_REQUIRED")
    for target in targets:
        target_id = str(target.get("id") or "")
        part = str(target.get("part") or "").lower()
        video_id = str(target.get("video") or "")
        if (
            not target_id
            or target_id in seen_targets
            or part in seen_parts
            or video_id in seen_videos
            or (part and not re.fullmatch(r"cd[1-9]\d*", part))
            or (len(targets) > 1 and not part)
        ):
            issue("MOVIE_TARGET_CONFLICT", target)
            continue
        seen_targets.add(target_id)
        seen_parts.add(part)
        seen_videos.add(video_id)
        video_file = files.get(video_id, {})
        videos = [t for t in video_file.get("tracks", []) if t["type"] == "video"]
        if video_file.get("kind") != "video" or len(videos) != 1:
            issue("MOVIE_VIDEO_REQUIRED", target)
            continue
        default_audio = target.get("default_audio")
        selected_audio = []
        for ref in target.get("audio", []):
            found = stream(ref, "audio")
            if found is None:
                issue("MOVIE_AUDIO_TRACK_INVALID", target, source=ref.get("source"), track=ref.get("track"))
            else:
                selected_audio.append((ref, *found))
        if not selected_audio:
            issue("MAIN_AUDIO_REQUIRED", target)
        if len({(r["source"], r["track"]) for r, _, _ in selected_audio}) != len(selected_audio):
            issue("MOVIE_AUDIO_DUPLICATE", target)
        if selected_audio and sum(ref == default_audio for ref, _, _ in selected_audio) != 1:
            issue("MOVIE_DEFAULT_AUDIO_REQUIRED", target)
        basis = audio_basis(target, inventory)
        if basis["external"] and target.get("external_confirmation", {}).get("basis") != basis:
            issue("MOVIE_EXTERNAL_AUDIO_CONFIRMATION_REQUIRED", target)

        # One input per chosen stream permits arbitrary final order without
        # relying on source-container track order or coincidental numeric IDs.
        arguments, order, expected = [], [], []
        source = source_path(work, video_id)
        video_track = videos[0]
        vi = str(video_track["mux_id"])
        arguments += [
            "--video-tracks",
            vi,
            "--no-audio",
            "--no-subtitles",
            "--no-attachments",
            "--language",
            f"{vi}:{video_track['language']}",
            "--track-name",
            f"{vi}:{release}",
            "--default-track-flag",
            f"{vi}:yes",
            "--forced-display-flag",
            f"{vi}:no",
        ]
        if not decisions.get("keep_chapters", True):
            arguments.append("--no-chapters")
        arguments.append(str(source))
        order.append(f"0:{vi}")
        expected.append(
            {
                "type": "video",
                "codecId": video_track["codec"],
                "language": video_track["language"],
                "name": release,
                "default": True,
                "forced": False,
            }
        )
        input_index = 1
        for ref, f, t in selected_audio:
            tid = str(t["mux_id"])
            default = ref == default_audio
            arguments += [
                "--no-video",
                "--audio-tracks",
                tid,
                "--no-subtitles",
                "--no-attachments",
                "--no-chapters",
                "--language",
                f"{tid}:{t['language']}",
                "--track-name",
                f"{tid}:{t['title']}",
                "--default-track-flag",
                f"{tid}:{'yes' if default else 'no'}",
                "--forced-display-flag",
                f"{tid}:no",
                str(source_path(work, f["id"])),
            ]
            order.append(f"{input_index}:{tid}")
            input_index += 1
            expected.append(
                {
                    "type": "audio",
                    "codecId": t["codec"],
                    "language": t["language"],
                    "name": t["title"],
                    "channels": t["channels"],
                    "default": default,
                    "forced": False,
                }
            )
        duplicates = {}
        for sub_index, ref in enumerate(target.get("subtitles", [])):
            f = files.get(ref.get("source"), {})
            embedded = bool(ref.get("track"))
            t = (
                next((t for t in f.get("tracks", []) if t["id"] == ref.get("track") and t["type"] == "subtitles"), None)
                if embedded
                else None
            )
            if (embedded and t is None) or (not embedded and f.get("kind") != "subtitle"):
                issue("MOVIE_SUBTITLE_INVALID", target, source=ref.get("source"))
                continue
            label, group = str(ref.get("subtitle_type") or ""), str(ref.get("group") or "").strip()
            if not label or not group:
                issue("MOVIE_SUBTITLE_INFO_REQUIRED", target, source=f["id"])
                continue
            group = _safe_label(group, "字幕组")
            label = _safe_label(label, "字幕类型")
            version = str(ref.get("version") or "").strip()
            if version:
                version = _safe_label(version, "字幕版本")
            dupkey = (embedded, group, label)
            duplicates.setdefault(dupkey, []).append(version)
            suffix = f".{version}" if version else ""
            name = f"{label} {group}" + (f" [{version}]" if version else "")
            path = source_path(work, f["id"])
            is_ass = (embedded and ("ASS" in t["codec"].upper() or "SSA" in t["codec"].upper())) or (
                not embedded and path.suffix.lower() in {".ass", ".ssa"}
            )
            if is_ass:
                identity = canonical_metadata_digest(
                    {"source": f["id"], "track": ref.get("track"), "target": target_id}
                )[:20]
                cache = temporary_path(work, "movie-subtitles", identity + ".ass")
                if embedded:
                    _extract(config, path, t["mux_id"], cache)
                else:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    # Copies live in the task output tree, never alongside input.
                    data = path.read_bytes()
                    if not cache.is_file() or cache.read_bytes() != data:
                        cache.write_bytes(data)
                fonts = parse_ass_font_requirements(cache)
                subtitle_inventory.append(
                    {
                        "file": file_signature(cache),
                        "group": group,
                        "fonts": fonts,
                        "original_source": f["id"],
                        "target_id": target_id,
                    }
                )
                groups.setdefault(f"{group}:{identity}", []).append(str(cache))
                sub_name = (
                    f"{title}{'.' + part if part else ''}.{label}.{group}{suffix}{'.embedded' if embedded else ''}.ass"
                )
                target_path = output_root / sub_name
                renames.append({"source": str(cache.with_suffix(".assfonts.ass")), "target": str(target_path)})
                entries.append({"source": str(target_path), "arcname": sub_name})
                sub_path, tid, codec = target_path, "0", "S_TEXT/ASS"
            else:
                sub_path, tid, codec = path, str(t["mux_id"]) if embedded else "0", t["codec"] if embedded else None
            lang = str(ref.get("language") or (t["language"] if t else "chi"))
            arguments += [
                "--no-video",
                "--no-audio",
                "--no-chapters",
                *([] if is_ass else ["--no-attachments"]),
                "--subtitle-tracks",
                tid,
                "--language",
                f"{tid}:{lang}",
                "--track-name",
                f"{tid}:{name}",
                "--default-track-flag",
                f"{tid}:{'yes' if sub_index == 0 else 'no'}",
                "--forced-display-flag",
                f"{tid}:no",
                str(sub_path),
            ]
            order.append(f"{input_index}:{tid}")
            input_index += 1
            expected.append(
                {
                    "type": "subtitles",
                    "language": lang,
                    "name": name,
                    "default": sub_index == 0,
                    "forced": False,
                    **({"codecId": codec} if codec else {}),
                }
            )
        for key, versions in duplicates.items():
            if len(versions) > 1 and (not all(versions) or len(set(versions)) != len(versions)):
                issue("MOVIE_SUBTITLE_VERSION_REQUIRED", target, group=key[1], subtitle_type=key[2])
        arguments += ["--track-order", ",".join(order)]
        chapters = bool(video_file.get("chapters")) and decisions.get("keep_chapters", True)
        output = movie_video_path(output_root, title, part)
        job = {
            "targetId": target_id,
            "inputFiles": sorted({str(source_path(work, r["source"])) for r in target.get("subtitles", [])}),
            "source": str(source),
            "output": str(output),
            "arguments": arguments,
            "trackSources": [],
            "expectedTracks": expected,
            "expectedChapters": chapters,
            "expectedAttachments": [],
            "allowSelectedCommentary": True,
        }
        jobs.append(job)
        finals.append(
            {
                "source": str(output),
                "relativePath": movie_video_relative(title, part),
                "expectedTracks": expected,
                "expectedChapters": chapters,
                "expectedAttachments": [],
                "allowSelectedCommentary": True,
            }
        )

    # Shared font checks operate on selected ASS only, with precise locations.
    from internal.preflight import selected_font_inventory

    selected_font_inventory(manifest, subtitle_inventory, config, work)
    issues.extend(manifest["discovery"].get("fontIssues", []))
    package = {"output": str(output_root / f"{title}.zip"), "entries": entries} if entries else None
    archive_root = config.get("paths", {}).get("movieSubtitleArchiveRoot")
    final_zip = []
    if package and archive_root:
        destination = str(Path(archive_root) / f"{title}.zip")
        package.update(mergeBase=destination, mergePolicy="preserve-existing-new-wins")
        final_zip = [{"source": package["output"], "destination": destination}]
    plan = {
        "title": title,
        "preferredLibrary": decisions.get("library_target", {}).get("library") or "Movie1",
        "expectedStatus": "Complete BDRip",
        "subtitleGroups": [{"name": k, "inputs": list(dict.fromkeys(v))} for k, v in groups.items()],
        "renameJobs": renames,
        "remuxJobs": jobs,
        "movieAudioPlans": [],
        "package": package,
        "final": {
            "mode": "replace" if manifest.get("taskMode") == "replacement" else "create",
            "video": finals,
            "zip": final_zip,
        },
    }
    plan["movieWorkbench"] = True
    manifest["discovery"]["movie_targets"] = [
        {
            "id": t.get("id"),
            "part": t.get("part"),
            "video": t.get("video"),
            "audio": t.get("audio"),
            "subtitles": t.get("subtitles"),
        }
        for t in targets
    ]
    return {
        "plan": plan,
        "issues": issues,
        "summary": {"movies": len(jobs), "subtitles": len(entries)},
        "release_labels": [],
        "resolved_release_group": release,
    }
