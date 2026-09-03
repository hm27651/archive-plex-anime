"""Deterministic Movie branch adapter for the unified archive planner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from archive_rules import (
    artifact_output_root,
    movie_stack_suffix,
    movie_subtitle_path,
    movie_video_path,
    movie_video_relative,
    package_path,
    temporary_path,
)
from plan_common import (
    archive_only_expected_tracks,
    channel_name,
    embedded_track_names,
    is_pgs,
    load_release_history,
    marker,
    relative_media_key,
    resolve_release_group,
    selected_track_keys,
    source_release_labels,
    subtitle_order,
    track_selection_map,
)


SPECIAL_AUDIO_RE = re.compile(r"commentary|评论|解说|audio description|无障碍|伴奏|karaoke", re.IGNORECASE)
MOVIE_LANGUAGES = {"JASC", "JATC", "SC", "TC"}
MOVIE_LANGUAGE_ORDER = {name: index for index, name in enumerate(("JASC", "JATC", "SC", "TC"))}
NUMBERED_STEM_RE = re.compile(r" \([1-9]\d*\)$")
BDMV_PREFIX_RE = re.compile(r"^\[BDMV\]\s*", re.IGNORECASE)


def _stack_key(path: Path) -> str:
    return movie_stack_suffix(NUMBERED_STEM_RE.sub("", path.stem))


def _standard_movie_stem(title: str, stem: str) -> bool:
    return bool(re.fullmatch(rf"{re.escape(title)}(?:\.cd[1-9]\d*)?", NUMBERED_STEM_RE.sub("", stem), re.IGNORECASE))


def _source_sets(items: list[dict[str, Any]], title: str, completed_steps: set[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    candidates = [
        item
        for item in items
        if not ("remux" in completed_steps and _standard_movie_stem(title, Path(item["file"]["path"]).stem))
    ]
    primary: dict[str, list[dict[str, Any]]] = {}
    disc: list[dict[str, Any]] = []
    for item in candidates:
        path = Path(item["file"]["path"])
        if BDMV_PREFIX_RE.match(path.stem):
            disc.append(item)
            continue
        primary.setdefault(_stack_key(path), []).append(item)
    issues = [
        {"code": "MOVIE_VIDEO_UNIQUE_REQUIRED", "stack": stack or "single", "count": len(group)}
        for stack, group in primary.items()
        if len(group) != 1
    ]
    if not primary:
        issues.append({"code": "MOVIE_VIDEO_UNIQUE_REQUIRED", "count": 0})
    if len(primary) > 1 and "" in primary:
        issues.append({"code": "MOVIE_STACK_MIXED_WITH_SINGLE"})
    return {stack: group[0] for stack, group in primary.items() if len(group) == 1}, issues


def build_movie_archive_only_plan(work: Path, manifest: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    output_root = artifact_output_root(work)
    discovery = manifest.get("discovery", {})
    videos = [
        item
        for item in discovery.get("videos", [])
        if item.get("status") == "OK" and any(track.get("type") == "video" for track in item.get("tracks", []))
    ]
    title = str(decisions.get("title") or work.name).strip()
    issues: list[dict[str, Any]] = []
    embedded = discovery.get("embeddedSubtitles", {})
    if embedded.get("status") != "COMPLETE":
        issues.append({"code": "ASS_REQUIRED", "detail": embedded.get("status") or "NO_ASS"})
    source_items, source_issues = _source_sets(videos, title, set())
    issues.extend(source_issues)

    final_video = []
    for stack, item in sorted(source_items.items()):
        source = Path(item["file"]["path"])
        if any(is_pgs(track) for track in item.get("tracks", [])):
            issues.append({"code": "ARCHIVE_ONLY_PGS_REQUIRES_REMUX", "file": source.name})
        normalized_tracks, compliance_issues = archive_only_expected_tracks(
            item, "movie", str(decisions.get("release_group") or "").strip()
        )
        issues.extend({**issue, "file": source.name} for issue in compliance_issues)
        attachments = [
            str(value.get("name") or "")
            for value in item.get("mkvInventory", {}).get("attachments", [])
            if value.get("name")
        ]
        final_video.append(
            {
                "source": str(source),
                "relativePath": movie_video_relative(title, stack),
                "expectedTracks": normalized_tracks,
                "expectedChapters": bool(item.get("chapters", {}).get("present")),
                "expectedAttachments": attachments,
            }
        )

    config = json.loads(Path(manifest["configPath"]).read_text(encoding="utf-8"))
    zip_source = str(decisions.get("zip") or "").strip()
    archive_root = str(config.get("paths", {}).get("movieSubtitleArchiveRoot") or "").strip()
    final_zip = []
    package = None
    if zip_source and archive_root:
        archive_destination = Path(archive_root) / f"{title}.zip"
        package_output = package_path(output_root, title)
        final_zip.append({"source": str(package_output), "destination": str(archive_destination)})
        package = {
            "output": str(package_output),
            "incomingZip": zip_source,
            "entries": [],
            "mergeBase": str(archive_destination),
            "mergePolicy": "preserve-existing-new-wins",
        }
    elif zip_source:
        issues.append({"code": "SUBTITLE_ARCHIVE_ROOT_REQUIRED"})
    plan = {
        "title": title,
        "preferredLibrary": decisions.get("library") or "Movie3",
        "expectedStatus": decisions.get("expected_status") or "Complete BDRip",
        "subtitleGroups": [],
        "renameJobs": [],
        "remuxJobs": [],
        "package": package,
        "final": {"mode": str(decisions.get("operation") or "create"), "video": final_video, "zip": final_zip},
    }
    return {"plan": plan, "issues": issues, "summary": {"videos": len(final_video), "direct": True}}


def _is_ass(track: dict[str, Any]) -> bool:
    value = f"{track.get('codecId', '')} {track.get('format', '')}".casefold()
    return track.get("type") == "subtitles" and ("ass" in value or "ssa" in value)


def movie_subtitle_metadata(item: dict[str, Any], decisions: dict[str, Any]) -> tuple[str | None, str | None]:
    path = Path(str(item.get("file", {}).get("path") or ""))
    stem = NUMBERED_STEM_RE.sub("", path.stem)
    parts = stem.split(".")
    language = next((part.upper() for part in parts if part.upper() in MOVIE_LANGUAGES), None)
    if language is None:
        folded = stem.casefold()
        if ".zh-cht" in folded or ".cht" in folded:
            language = "TC"
        elif ".zh-chs" in folded or ".chs" in folded:
            language = "SC"
    group = None
    if language and language in (part.upper() for part in parts):
        index = next(index for index, part in enumerate(parts) if part.upper() == language)
        suffix = ".".join(parts[index + 1 :]).strip(" ._-[]")
        if suffix:
            group = suffix
    requested = [str(value).strip() for value in decisions.get("subtitle_order", []) if str(value).strip()]
    if group is None:
        group = next((value for value in requested if value.casefold() in stem.casefold()), None)
    source_group = str(item.get("group") or "").strip()
    if group is None and source_group not in {"", "."}:
        group = Path(source_group).name
    if group is None and len(requested) == 1:
        group = requested[0]
    return language, group


def embedded_movie_subtitle_metadata(name: str) -> tuple[str | None, str]:
    parts = str(name).strip().split(maxsplit=1)
    language = parts[0].upper() if parts and parts[0].upper() in MOVIE_LANGUAGES else None
    group = parts[1].strip() if language and len(parts) > 1 else str(name).strip()
    return language, group


def _movie_audio_plan(work: Path, title: str, preflight: dict[str, Any], release_group: str, decisions: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not preflight:
        return None, [{"code": "MOVIE_AUDIO_PREFLIGHT_REQUIRED"}]
    if preflight.get("status") != "READY_FOR_PREFLIGHT":
        return None, [{"code": "MOVIE_AUDIO_PREFLIGHT_UNRESOLVED", "status": preflight.get("status")}]
    inventory = preflight.get("inventory", {})
    old_tracks = inventory.get("video_source", inventory.get("old_mkv", {})).get("tracks", [])
    source_tracks = inventory.get("disc_source", inventory.get("source_m2ts", {})).get("tracks", [])
    matching = preflight.get("matching", {})
    video = next((track for track in old_tracks if track.get("type") == "video"), None)
    if not video:
        return None, [{"code": "MOVIE_VIDEO_REQUIRED"}]
    issues: list[dict[str, Any]] = []
    audio = []
    offset = float(preflight.get("sync", {}).get("publicOffsetMs") or 0)
    disc_path = Path(preflight["verifiedDiscSource"]["path"])
    keep_map = decisions.get("disc_audio_keep", {})
    keep_values = keep_map.get(relative_media_key(disc_path, work)) if isinstance(keep_map, dict) else None
    explicit_keep = {str(value) for value in keep_values} if isinstance(keep_values, list) else None
    for track in source_tracks:
        if track.get("type") != "audio":
            continue
        special = bool(SPECIAL_AUDIO_RE.search(str(track.get("title") or "")))
        track_tokens = {str(track.get("trackKey") or ""), str(track.get("ffprobe_index", ""))}
        if special and (explicit_keep is None or not (track_tokens & explicit_keep)):
            continue
        if explicit_keep is not None and not special and not (track_tokens & explicit_keep):
            continue
        audio.append({**track, "origin": "disc", "source_ffprobe_index": track.get("ffprobe_index"), "sync_ms": offset})
    if not audio:
        issues.append({"code": "MAIN_AUDIO_REQUIRED", "stack": preflight.get("stack") or "single"})
    audio.sort(key=lambda item: (-(int(item.get("channels") or 0)), int(item.get("type_order") or 0)))
    for index, track in enumerate(audio):
        track["name"] = channel_name(track.get("channels"))
        track["default"] = index == 0
        track["forced"] = False
        track["language"] = track.get("language") or "jpn"
        track["mux_id"] = track.get("mkvmerge_id")
    stack = str(preflight.get("stack") or "")
    output = temporary_path(work, "movie-audio", movie_video_path(Path(), title, stack).name)
    return {
        "stack": stack,
        "video_source": preflight["videoSource"]["path"],
        "disc_source": preflight["verifiedDiscSource"]["path"],
        "output": str(output),
        "video": {"old_mux_id": video.get("mkvmerge_id"), "name": release_group, "language": "jpn"},
        "audio": audio,
        "subtitles": [],
    }, issues


def _build_remux_job(
    work: Path,
    output_root: Path,
    title: str,
    source_entry: dict[str, Any],
    planned_subtitles: list[dict[str, Any]],
    passthrough_attachments: list[dict[str, Any]],
    release_group: str,
    decisions: dict[str, Any],
    video_selection: dict[str, set[str]],
    audio_selection: dict[str, set[str]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    stack = str(source_entry.get("stack") or "")
    source = Path(source_entry["source"])
    source_inventory = source_entry["inventory"]
    movie_audio = bool(source_entry.get("movie_audio"))
    synthetic_audio = list(source_entry.get("synthetic_audio") or [])
    output = movie_video_path(output_root, title, stack)
    issues: list[dict[str, Any]] = []
    expected_tracks: list[dict[str, Any]] = [{
        "type": "video", "language": "jpn", "name": release_group, "default": True, "forced": False,
    }]
    arguments: list[str] = []
    track_order: list[str] = []
    track_sources: list[dict[str, Any]] = []
    keep_chapters = bool(decisions.get("keep_chapters", True))

    if movie_audio:
        arguments.extend([
            "--video-tracks", "0", "--language", "0:jpn", "--track-name", f"0:{release_group}",
            "--default-track-flag", "0:yes", "--forced-display-flag", "0:no",
        ])
        track_order.append("0:0")
        ids = [index + 1 for index in range(len(synthetic_audio))]
        if ids:
            arguments.extend(["--audio-tracks", ",".join(str(value) for value in ids)])
        for index, (track_id, track) in enumerate(zip(ids, synthetic_audio)):
            language = str(track.get("language") or "jpn")
            name = channel_name(track.get("channels"))
            arguments.extend([
                "--language", f"{track_id}:{language}", "--track-name", f"{track_id}:{name}",
                "--default-track-flag", f"{track_id}:{'yes' if index == 0 else 'no'}",
                "--forced-display-flag", f"{track_id}:no",
            ])
            track_order.append(f"0:{track_id}")
            expected_tracks.append({
                "type": "audio", "language": language, "name": name, "default": index == 0,
                "forced": False, "channels": track.get("channels"),
            })
        arguments.extend(["--no-subtitles", "--no-attachments"])
    else:
        video_tracks = [track for track in source_inventory.get("tracks", []) if track.get("type") == "video"]
        requested_video = selected_track_keys(video_selection, source, work)
        if len(video_tracks) > 1:
            if requested_video is None or len(requested_video) != 1:
                issues.append({"code": "VIDEO_TRACK_SELECTION_REQUIRED", "file": relative_media_key(source, work), "tracks": [str(track.get("trackKey")) for track in video_tracks]})
                video_tracks = []
            else:
                video_tracks = [track for track in video_tracks if str(track.get("trackKey")) in requested_video]
        elif requested_video is not None and (len(video_tracks) != 1 or requested_video != {str(video_tracks[0].get("trackKey"))}):
            issues.append({"code": "VIDEO_KEEP_UNKNOWN", "file": relative_media_key(source, work), "tracks": sorted(requested_video)})
        requested_audio = selected_track_keys(audio_selection, source, work)
        audio_decided = requested_audio is not None
        audio_tracks: list[dict[str, Any]] = []
        undecided: list[str] = []
        for track in source_inventory.get("tracks", []):
            if track.get("type") != "audio":
                continue
            special = SPECIAL_AUDIO_RE.search(str(track.get("title") or ""))
            flac = "flac" in f"{track.get('codecId', '')} {track.get('format', '')}".casefold()
            if special or flac:
                if audio_decided and str(track.get("trackKey")) in requested_audio:
                    audio_tracks.append(track)
                elif not audio_decided:
                    undecided.append(f"{relative_media_key(source, work)}#{track.get('trackKey')}")
            else:
                audio_tracks.append(track)
        if undecided:
            issues.append({"code": "MOVIE_SPECIAL_AUDIO_DECISION_REQUIRED", "tracks": undecided})
        audio_tracks.sort(key=lambda item: (-(int(item.get("channels") or 0)), str(item.get("trackKey"))))
        if not audio_tracks:
            issues.append({"code": "MAIN_AUDIO_REQUIRED", "stack": stack or "single"})
        selected = [*video_tracks, *audio_tracks]
        track_sources.append({"source": str(source), "selectedTrackKeys": [str(track["trackKey"]) for track in selected]})
        if video_tracks:
            token = marker(0, str(video_tracks[0]["trackKey"]))
            arguments.extend([
                "--video-tracks", token, "--language", f"{token}:jpn", "--track-name", f"{token}:{release_group}",
                "--default-track-flag", f"{token}:yes", "--forced-display-flag", f"{token}:no",
            ])
            track_order.append(f"0:{token}")
        if audio_tracks:
            arguments.extend(["--audio-tracks", ",".join(marker(0, str(item["trackKey"])) for item in audio_tracks)])
        else:
            arguments.append("--no-audio")
        for index, track in enumerate(audio_tracks):
            token = marker(0, str(track["trackKey"]))
            language = str(track.get("language") or "jpn")
            name = channel_name(track.get("channels"))
            arguments.extend([
                "--language", f"{token}:{language}", "--track-name", f"{token}:{name}",
                "--default-track-flag", f"{token}:{'yes' if index == 0 else 'no'}", "--forced-display-flag", f"{token}:no",
            ])
            track_order.append(f"0:{token}")
            expected_tracks.append({
                "type": "audio", "language": language, "name": name, "default": index == 0,
                "forced": False, "channels": track.get("channels"),
            })
        arguments.extend(["--no-subtitles", "--no-attachments"])

    if not keep_chapters:
        arguments.append("--no-chapters")
    arguments.append(str(source))
    selected_subtitles = [item for item in planned_subtitles if str(item.get("stack") or "") == stack]
    for input_index, subtitle in enumerate(selected_subtitles, start=1):
        arguments.extend([
            "--no-video", "--no-audio", "--no-chapters", "--language", "0:chi",
            "--track-name", f"0:{subtitle['name']}", "--default-track-flag", f"0:{'yes' if subtitle['default'] else 'no'}",
            "--forced-display-flag", "0:no", subtitle["path"],
        ])
        track_order.append(f"{input_index}:0")
        expected_tracks.append({
            "type": "subtitles", "language": "chi", "name": subtitle["name"],
            "default": subtitle["default"], "forced": False,
        })
    for attachment in passthrough_attachments:
        arguments.extend(["--attachment-name", str(attachment.get("name") or Path(attachment["path"]).name), "--attach-file", str(attachment["path"])])
    if track_order:
        arguments.extend(["--track-order", ",".join(track_order)])
    chapters = bool(source_inventory.get("chapters", {}).get("present")) if keep_chapters else False
    expected_attachments = [str(item.get("name") or Path(item["path"]).name) for item in passthrough_attachments]
    job = {
        "source": str(source), "output": str(output), "arguments": arguments, "trackSources": track_sources,
        "chapters": "preserve" if chapters else "drop", "dropPrimaryChapters": not keep_chapters,
        "expectedTracks": expected_tracks, "expectedChapters": chapters, "expectedAttachments": expected_attachments,
    }
    final = {
        "source": str(output), "relativePath": movie_video_relative(title, stack), "expectedTracks": expected_tracks,
        "expectedChapters": chapters, "expectedAttachments": expected_attachments,
    }
    return job, final, issues


def build_movie_plan(
    work: Path,
    manifest: dict[str, Any],
    decisions: dict[str, Any],
    completed_steps: set[str] | None = None,
) -> dict[str, Any]:
    work = work.resolve()
    output_root = artifact_output_root(work)
    discovery = manifest.get("discovery", {})
    title = str(decisions.get("title") or work.name).strip()
    completed_steps = completed_steps or set()
    all_source_items = [
        item
        for item in discovery.get("videos", [])
        if item.get("status") == "OK" and any(track.get("type") == "video" for track in item.get("tracks", []))
    ]
    primary_items, primary_issues = _source_sets(all_source_items, title, completed_steps)
    task_audio = list(discovery.get("movieAudioPreflights") or [])
    labels = source_release_labels(list(primary_items.values()))
    release_group, issues = resolve_release_group(labels, decisions, load_release_history())
    if not task_audio:
        issues.extend(primary_issues)
    movie_audio_plans: list[dict[str, Any]] = []
    audio_selection, audio_selection_issues = track_selection_map(decisions, "audio_keep")
    video_selection, video_selection_issues = track_selection_map(decisions, "video_keep")
    issues.extend(audio_selection_issues)
    issues.extend(video_selection_issues)
    sources: list[dict[str, Any]] = []
    if task_audio:
        seen_stacks: set[str] = set()
        for preflight in task_audio:
            movie_audio_plan, audio_issues = _movie_audio_plan(work, title, preflight, release_group or "", decisions)
            issues.extend(audio_issues)
            if not movie_audio_plan:
                continue
            stack = str(movie_audio_plan.get("stack") or "")
            if stack in seen_stacks:
                issues.append({"code": "MOVIE_STACK_DUPLICATE", "stack": stack or "single"})
                continue
            seen_stacks.add(stack)
            movie_audio_plans.append(movie_audio_plan)
            sources.append({
                "stack": stack,
                "source": Path(movie_audio_plan["output"]),
                "inventory": preflight.get("oldMediaInventory") or {"chapters": {"present": bool(decisions.get("keep_chapters", True))}},
                "synthetic_audio": movie_audio_plan["audio"],
                "movie_audio": True,
            })
    else:
        for stack, item in sorted(primary_items.items()):
            sources.append({
                "stack": stack,
                "source": Path(item["file"]["path"]),
                "inventory": item,
                "synthetic_audio": [],
                "movie_audio": False,
            })

    subtitles = list(discovery.get("subtitles", []))
    if "subtitle" in completed_steps:
        standard_subtitle = re.compile(
            rf"^{re.escape(title)}(?:\.cd[1-9]\d*)?\.(?:JASC|JATC|SC|TC)\..+$",
            re.IGNORECASE,
        )
        subtitles = [
            item
            for item in subtitles
            if not (
                standard_subtitle.match(NUMBERED_STEM_RE.sub("", Path(item["file"]["path"]).stem))
            )
        ]
    embedded = discovery.get("embeddedSubtitles", {})
    if not subtitles and embedded.get("status") not in {"COMPLETE", "METADATA_ONLY"}:
        issues.append({"code": "ASS_REQUIRED", "detail": embedded.get("status") or "NO_ASS"})
    source_stacks = {str(item.get("stack") or "") for item in sources}
    metadata: dict[str, tuple[str, str, str]] = {}
    for item in subtitles:
        path = str(item.get("file", {}).get("path") or "")
        language, group = movie_subtitle_metadata(item, decisions)
        stack = _stack_key(Path(path))
        if not language:
            issues.append({"code": "MOVIE_SUBTITLE_LANGUAGE_REQUIRED", "file": Path(path).name})
        if not group:
            issues.append({"code": "MOVIE_SUBTITLE_GROUP_REQUIRED", "file": Path(path).name})
        if len(source_stacks) > 1 and not stack:
            issues.append({"code": "MOVIE_SUBTITLE_STACK_REQUIRED", "file": Path(path).name})
        elif stack not in source_stacks:
            issues.append({"code": "MOVIE_SUBTITLE_STACK_UNKNOWN", "file": Path(path).name, "stack": stack or "single"})
        if language and group:
            metadata[path] = (language, group, stack)
    mixed_embedded_subtitles: list[dict[str, Any]] = []
    mixed_embedded_attachments: list[dict[str, Any]] = []
    if subtitles and decisions.get("retain_embedded_subtitles"):
        if embedded.get("status") not in {"COMPLETE", "METADATA_ONLY"}:
            issues.append({"code": "EMBEDDED_ASS_REQUIRED", "detail": embedded.get("status") or "NO_ASS"})
        else:
            flattened = [
                (file_item, track_item)
                for file_item in embedded.get("files", [])
                for track_item in file_item.get("tracks", [])
            ]
            names, embedded_name_issues = embedded_track_names(
                [str(item.get("track", {}).get("name") or "").strip() for _, item in flattened], decisions
            )
            issues.extend(embedded_name_issues)
            source_stack: dict[str, str] = {}
            for preflight in task_audio:
                source_path = str(preflight.get("videoSource", {}).get("path") or "")
                if source_path:
                    source_stack[str(Path(source_path).resolve()).casefold()] = str(preflight.get("stack") or "")
            for stack, item in primary_items.items():
                source_stack[str(Path(item["file"]["path"]).resolve()).casefold()] = stack
            for (file_item, track_item), name in zip(flattened, names):
                language, group = embedded_movie_subtitle_metadata(name)
                source_key = str(Path(file_item["source"]).resolve()).casefold()
                stack = source_stack.get(source_key, "" if len(source_stacks) == 1 else "__unknown__")
                if not language:
                    issues.append({"code": "MOVIE_SUBTITLE_LANGUAGE_REQUIRED", "file": Path(file_item["source"]).name, "track": name})
                if stack not in source_stacks:
                    issues.append({"code": "MOVIE_SUBTITLE_STACK_UNKNOWN", "file": Path(file_item["source"]).name, "stack": stack})
                mixed_embedded_subtitles.append({
                    "path": track_item["extracted"], "group": group, "stack": stack,
                    "name": name, "languageLabel": language, "default": False, "passthrough": True,
                })
            mixed_embedded_attachments = [
                attachment
                for file_item in embedded.get("files", [])
                for attachment in file_item.get("attachments", [])
            ]

    groups = list(dict.fromkeys([
        *(group for _, group, _ in metadata.values()),
        *(item["group"] for item in mixed_embedded_subtitles),
    ]))
    ordered_groups, subtitle_issues = subtitle_order(groups, decisions)
    issues.extend(subtitle_issues)
    rank = {name: index for index, name in enumerate(ordered_groups)}
    default_requested = str(decisions.get("default_subtitle") or "").casefold()
    default_group = next((name for name in ordered_groups if default_requested and default_requested in name.casefold()), ordered_groups[0] if ordered_groups else "")

    rename_jobs = []
    package_entries = []
    subtitle_groups: dict[str, list[str]] = {}
    planned_subtitles = []
    default_assigned: set[str] = set()
    valid_subtitles = [item for item in subtitles if str(item.get("file", {}).get("path") or "") in metadata]
    for item in sorted(
        valid_subtitles,
        key=lambda value: (
            _stack_key(Path(value["file"]["path"])),
            rank.get(metadata[str(value["file"]["path"])][1], len(rank)),
            MOVIE_LANGUAGE_ORDER[metadata[str(value["file"]["path"])][0]],
            str(value["file"]["path"]).casefold(),
        ),
    ):
        path = Path(item["file"]["path"])
        language, group, stack = metadata[str(path)]
        target = movie_subtitle_path(output_root, title, language, group, stack)
        subset = path.with_suffix(".assfonts" + path.suffix)
        rename_jobs.append({"source": str(subset), "target": str(target)})
        subtitle_groups.setdefault(group, []).append(str(path))
        package_entries.append({"source": str(target), "arcname": target.name})
        is_default = group == default_group and stack not in default_assigned
        if is_default:
            default_assigned.add(stack)
        planned_subtitles.append({
            "path": str(target),
            "group": group,
            "stack": stack,
            "name": f"{language} {group}",
            "default": is_default,
        })

    passthrough_attachments: list[dict[str, Any]] = []
    if not subtitles and embedded.get("status") in {"COMPLETE", "METADATA_ONLY"}:
        embedded_tracks = [track for item in embedded.get("files", []) for track in item.get("tracks", [])]
        requested_names = [str(value) for value in decisions.get("subtitle_order", [])]
        names, embedded_name_issues = embedded_track_names(
            [str(item.get("track", {}).get("name") or "").strip() for item in embedded_tracks], decisions
        )
        issues.extend(embedded_name_issues)
        if requested_names and names:
            order = sorted(range(len(names)), key=lambda index: next((rank for rank, value in enumerate(requested_names) if value.casefold() in names[index].casefold() or names[index].casefold() in value.casefold()), len(requested_names)))
            embedded_tracks = [embedded_tracks[index] for index in order]
            names = [names[index] for index in order]
        requested_default = str(decisions.get("default_subtitle") or (names[0] if names else "")).casefold()
        default_index = next(
            (index for index, name in enumerate(names) if name.casefold() == requested_default),
            0 if names else -1,
        )
        for index, (item, name) in enumerate(zip(embedded_tracks, names)):
            planned_subtitles.append({
                "path": item["extracted"],
                "group": name,
                "name": name,
                "default": index == default_index,
                "passthrough": True,
            })
        passthrough_attachments = [attachment for item in embedded.get("files", []) for attachment in item.get("attachments", [])]

    if mixed_embedded_subtitles:
        planned_subtitles.extend(mixed_embedded_subtitles)
        passthrough_attachments.extend(mixed_embedded_attachments)
        planned_subtitles.sort(key=lambda item: (
            str(item.get("stack") or ""),
            rank.get(str(item.get("group") or ""), len(rank)),
            MOVIE_LANGUAGE_ORDER.get(
                str(item.get("languageLabel") or embedded_movie_subtitle_metadata(str(item.get("name") or ""))[0]),
                len(MOVIE_LANGUAGE_ORDER),
            ),
            str(item.get("name") or "").casefold(),
        ))
        requested_default = str(decisions.get("default_subtitle") or "").casefold()
        for stack in source_stacks:
            current = [item for item in planned_subtitles if str(item.get("stack") or "") == stack]
            for item in current:
                item["default"] = False
            selected = next((item for item in current if str(item.get("name") or "").casefold() == requested_default), None)
            if selected is None:
                selected = next((item for item in current if str(item.get("group") or "").casefold() == default_group.casefold()), None)
            if selected is None and current:
                selected = current[0]
            if selected is not None:
                selected["default"] = True

    remux_jobs = []
    final_video = []
    for source_entry in sorted(sources, key=lambda item: str(item.get("stack") or "")):
        job, final, remux_issues = _build_remux_job(
            work,
            output_root,
            title,
            source_entry,
            planned_subtitles,
            passthrough_attachments,
            release_group or "",
            decisions,
            video_selection,
            audio_selection,
        )
        remux_jobs.append(job)
        final_video.append(final)
        issues.extend(remux_issues)

    config = json.loads(Path(manifest["configPath"]).read_text(encoding="utf-8"))
    archive_root = str(config.get("paths", {}).get("movieSubtitleArchiveRoot") or "").strip()
    package_output = package_path(output_root, title)
    final_zip = []
    package_plan = None
    if package_entries and archive_root:
        archive_destination = Path(archive_root) / f"{title}.zip"
        final_zip.append({"source": str(package_output), "destination": str(archive_destination)})
        package_plan = {
            "output": str(package_output),
            "entries": package_entries,
            "mergeBase": str(archive_destination),
            "mergePolicy": "preserve-existing-new-wins",
        }
    elif package_entries:
        issues.append({"code": "SUBTITLE_ARCHIVE_ROOT_REQUIRED"})
        package_plan = {"output": str(package_output), "entries": package_entries}
    plan = {
        "title": title,
        "preferredLibrary": decisions.get("library") or "Movie3",
        "expectedStatus": decisions.get("expected_status") or "Complete BDRip",
        "subtitleGroups": [{"name": name, "inputs": paths} for name, paths in subtitle_groups.items()],
        "renameJobs": rename_jobs,
        "remuxJobs": remux_jobs,
        "package": package_plan,
        "movieAudioPlans": movie_audio_plans,
        "final": {"mode": "replace" if manifest.get("taskMode") == "replacement" else "create", "video": final_video, "zip": final_zip},
    }
    return {"plan": plan, "issues": issues, "release_labels": labels, "resolved_release_group": release_group, "summary": {"movies": len(final_video), "subtitles": len(subtitles), "movie_audio": bool(movie_audio_plans)}}
