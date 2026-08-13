"""Shared deterministic planning helpers for TV and Movie."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import config_path, read_text, write_json_atomic


RELEASE_SPLIT_RE = re.compile(r"\s*(?:&|\+|×|、|/|\band\b)\s*", re.IGNORECASE)
RELEASE_NOISE_RE = re.compile(
    r"^(?:\d{3,4}p|x?26[45]|hevc|avc|aac|flac|opus|web(?:-?dl|rip)?|bd(?:rip|mv)?|10bit|8bit)$",
    re.IGNORECASE,
)


def release_history_path() -> Path:
    return config_path().parent / "release-groups.json"


def load_release_history(path: Path | None = None) -> dict[str, str]:
    selected = path or release_history_path()
    if not selected.is_file():
        return {}
    payload = json.loads(read_text(selected))
    groups = payload.get("groups", {}) if isinstance(payload, dict) else {}
    return {str(key).casefold(): str(value) for key, value in groups.items() if str(key).strip() and str(value).strip()}


def update_release_history(labels: list[str], release_group: str, path: Path | None = None) -> None:
    selected = path or release_history_path()
    current = load_release_history(selected)
    for label in [*labels, release_group]:
        for component in release_components(label):
            current[component.casefold()] = release_group
        current[label.casefold()] = release_group
    write_json_atomic(selected, {"schema": 1, "groups": dict(sorted(current.items()))})


def release_components(label: str) -> list[str]:
    return [part.strip() for part in RELEASE_SPLIT_RE.split(str(label)) if part.strip()]


def source_release_labels(videos: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for item in videos:
        path = Path(item["file"]["path"])
        for value in re.findall(r"\[([^\[\]]+)\]", path.stem):
            value = value.strip()
            if value and not RELEASE_NOISE_RE.match(value) and not value.isdigit():
                labels.append(value)
        for track in item.get("tracks", []):
            if track.get("type") == "video" and str(track.get("title") or "").strip():
                labels.append(str(track["title"]).strip())
    return list(dict.fromkeys(labels))


def resolve_release_group(
    labels: list[str], decisions: dict[str, Any], history: dict[str, str]
) -> tuple[str | None, list[dict[str, Any]]]:
    explicit = str(decisions.get("release_group") or "").strip()
    if explicit:
        return explicit, []
    hits: set[str] = set()
    for label in labels:
        for candidate in [label, *release_components(label)]:
            value = history.get(candidate.casefold())
            if value:
                hits.add(value)
    if len(hits) == 1:
        return next(iter(hits)), []
    if len(hits) > 1:
        return None, [{"code": "RELEASE_GROUP_AMBIGUOUS", "candidates": sorted(hits)}]
    return None, [{"code": "RELEASE_GROUP_REQUIRED", "source_labels": labels}]


def channel_name(channels: Any) -> str:
    try:
        value = int(channels)
    except (TypeError, ValueError):
        return ""
    return {1: "1.0ch", 2: "2.0ch", 6: "5.1ch", 8: "7.1ch"}.get(value, f"{value}.0ch")


def relative_media_key(source: Path, work: Path) -> str:
    try:
        return source.resolve(strict=False).relative_to(work.resolve(strict=False)).as_posix()
    except ValueError:
        return ""


def track_selection_map(
    decisions: dict[str, Any], key: str
) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    if key not in decisions:
        return {}, []
    value = decisions.get(key)
    code = f"{key.upper()}_INVALID"
    if not isinstance(value, dict):
        return {}, [{"code": code, "detail": "selection must be an object keyed by work-relative path"}]
    selected: dict[str, set[str]] = {}
    issues: list[dict[str, Any]] = []
    for raw_path, raw_keys in value.items():
        path = str(raw_path).strip().replace("\\", "/").strip("/")
        parts = [part for part in path.split("/") if part]
        if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in parts:
            issues.append({"code": code, "path": str(raw_path)})
            continue
        keys = raw_keys if isinstance(raw_keys, list) else [raw_keys]
        normalized_keys = {str(item).strip() for item in keys if str(item).strip()}
        if len(normalized_keys) != len(keys):
            issues.append({"code": code, "path": path, "detail": "track keys must be non-empty and unique"})
            continue
        selected[path.casefold()] = normalized_keys
    return selected, issues


def selected_track_keys(selection: dict[str, set[str]], source: Path, work: Path) -> set[str] | None:
    relative = relative_media_key(source, work)
    return selection.get(relative.casefold()) if relative else None


def marker(source_index: int, track_key: str) -> str:
    return "{{track:" + str(source_index) + ":" + track_key + "}}"


def is_pgs(track: dict[str, Any]) -> bool:
    value = f"{track.get('codecId', '')} {track.get('format', '')} {track.get('codec', '')}".casefold()
    return track.get("type") == "subtitles" and ("pgs" in value or "hdmv" in value)


def embedded_track_names(current: list[str], decisions: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    supplied = decisions.get("embedded_subtitle_names")
    if supplied is not None:
        if not isinstance(supplied, list) or len(supplied) != len(current) or any(not str(value).strip() for value in supplied):
            return current, [{"code": "EMBEDDED_SUBTITLE_NAMES_INVALID", "tracks": len(current)}]
        return [str(value).strip() for value in supplied], []
    if any(not str(value).strip() for value in current):
        requested = decisions.get("subtitle_order")
        if isinstance(requested, list) and len(requested) == len(current) and all(str(value).strip() for value in requested):
            return [str(value).strip() for value in requested], []
        return current, [{"code": "EMBEDDED_SUBTITLE_METADATA_REQUIRED", "tracks": len(current)}]
    return current, []


def subtitle_order(groups: list[str], decisions: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    requested = [str(value).strip() for value in decisions.get("subtitle_order", []) if str(value).strip()]
    if len(groups) > 1 and not requested:
        return groups, [{"code": "SUBTITLE_ORDER_REQUIRED", "groups": groups}]
    if not requested:
        return groups, []
    if len(requested) != len(groups) or len({name.casefold() for name in requested}) != len(requested):
        return groups, [{"code": "SUBTITLE_ORDER_INVALID", "groups": groups, "requested": requested}]
    ordered: list[str] = []
    for requested_name in requested:
        folded = requested_name.casefold()
        matches = [name for name in groups if folded in name.casefold() or name.casefold() in folded]
        if len(matches) != 1 or matches[0] in ordered:
            return groups, [{"code": "SUBTITLE_ORDER_INVALID", "groups": groups, "requested": requested}]
        ordered.append(matches[0])
    if len(ordered) != len(groups):
        return groups, [{"code": "SUBTITLE_ORDER_INVALID", "groups": groups, "requested": requested}]
    return ordered, []


def archive_only_expected_tracks(
    item: dict[str, Any], branch: str, release_group: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracks = list(item.get("tracks", []))
    issues: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    videos = [track for track in tracks if track.get("type") == "video"]
    audios = [track for track in tracks if track.get("type") == "audio"]
    subtitles = [track for track in tracks if track.get("type") == "subtitles" and not is_pgs(track)]
    if len(videos) != 1:
        issues.append({"code": "ARCHIVE_ONLY_VIDEO_COUNT", "count": len(videos)})
    for track in videos:
        name = release_group or str(track.get("title") or "").strip()
        if (
            not name
            or str(track.get("language") or "und").casefold() != "jpn"
            or not bool(track.get("default"))
            or bool(track.get("forced"))
        ):
            issues.append({"code": "ARCHIVE_ONLY_VIDEO_METADATA", "trackKey": track.get("trackKey")})
        expected.append({"type": "video", "language": "jpn", "name": name, "default": True, "forced": False})
    if not audios:
        issues.append({"code": "MAIN_AUDIO_REQUIRED"})
    commentary = re.compile(r"commentary|评论|解说|audio description|无障碍|伴奏|karaoke", re.IGNORECASE)
    if branch == "movie":
        if any(commentary.search(str(track.get("title") or "")) or "flac" in f"{track.get('codecId', '')} {track.get('format', '')}".casefold() for track in audios):
            issues.append({"code": "ARCHIVE_ONLY_AUDIO_NONCOMPLIANT"})
        ordered = sorted(audios, key=lambda track: (-(int(track.get("channels") or 0)), str(track.get("trackKey"))))
    else:
        if any(commentary.search(str(track.get("title") or "")) for track in audios):
            issues.append({"code": "ARCHIVE_ONLY_AUDIO_NONCOMPLIANT"})
        ordered = sorted(audios, key=lambda track: (0 if track.get("channels") == 2 else 1, -(int(track.get("channels") or 0))))
    if [track.get("trackKey") for track in audios] != [track.get("trackKey") for track in ordered]:
        issues.append({"code": "ARCHIVE_ONLY_AUDIO_ORDER"})
    single_tv_stereo = branch == "tv" and len(audios) == 1 and audios[0].get("channels") == 2
    for index, track in enumerate(audios):
        name = "" if single_tv_stereo else channel_name(track.get("channels"))
        if (
            str(track.get("language") or "und").casefold() != "jpn"
            or str(track.get("title") or "") != name
            or bool(track.get("default")) != (index == 0)
            or bool(track.get("forced"))
        ):
            issues.append({"code": "ARCHIVE_ONLY_AUDIO_METADATA", "trackKey": track.get("trackKey")})
        expected.append({"type": "audio", "language": "jpn", "name": name, "default": index == 0, "forced": False, "channels": track.get("channels")})
    if not subtitles:
        issues.append({"code": "ASS_REQUIRED", "detail": "NO_ASS"})
    default_count = sum(1 for track in subtitles if bool(track.get("default")))
    for track in subtitles:
        name = str(track.get("title") or "").strip()
        if (
            not name
            or str(track.get("language") or "und").casefold() != "chi"
            or bool(track.get("forced"))
        ):
            issues.append({"code": "ARCHIVE_ONLY_SUBTITLE_METADATA", "trackKey": track.get("trackKey")})
        expected.append({"type": "subtitles", "language": "chi", "name": name, "default": bool(track.get("default")), "forced": False})
    if subtitles and default_count != 1:
        issues.append({"code": "ARCHIVE_ONLY_SUBTITLE_METADATA", "detail": "exactly one default subtitle is required"})
    return expected, issues
