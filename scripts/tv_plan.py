"""Build the TV branch of the unified deterministic archive plan."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from archive_rules import package_path, tv_subtitle_path, tv_video_path, tv_video_relative
from common import read_text
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
    track_selection_map,
)


COMMENTARY_RE = re.compile(r"commentary|评论|解说", re.IGNORECASE)
SEASON_RE = re.compile(r"^(?:s|season\s*)(\d{1,2})$", re.IGNORECASE)
EPISODE_PATTERNS = (
    re.compile(r"s\d{1,2}e(\d{1,3})", re.IGNORECASE),
    re.compile(r"\[(\d{1,3})(?:v\d+)?\]", re.IGNORECASE),
    re.compile(r"(?:^|[\s._\-(])(\d{1,3})(?:v\d+)?(?=$|[\s._\-)\[])", re.IGNORECASE),
)
IGNORED_NUMBERS = {264, 265, 480, 576, 720, 1080, 2160}


def build_tv_archive_only_plan(work: Path, manifest: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
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

    season_groups, release_group, release_issues, labels_by_season = _season_release_groups(
        work, videos, decisions, None
    )
    issues.extend(release_issues)

    records, episode_issues = _episode_records(work, videos, decisions)
    issues.extend(episode_issues)
    final_video = []
    for record in records:
        source = record["path"]
        item = record["inventory"]
        if any(is_pgs(track) for track in item.get("tracks", [])):
            issues.append({"code": "ARCHIVE_ONLY_PGS_REQUIRES_REMUX", "file": source.name})
        normalized_tracks, compliance_issues = archive_only_expected_tracks(
            item, "tv", _release_group_for_season(record["season"], season_groups, release_group)
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
                "relativePath": tv_video_relative(title, record["season"], record["target_episode"]),
                "expectedTracks": normalized_tracks,
                "expectedChapters": bool(item.get("chapters", {}).get("present")),
                "expectedAttachments": attachments,
            }
        )
    if not final_video:
        issues.append({"code": "DIRECT_ARCHIVE_VIDEO_REQUIRED"})

    config = json.loads(Path(manifest["configPath"]).read_text(encoding="utf-8"))
    zip_source = str(decisions.get("zip") or "").strip()
    archive_root = str(config.get("paths", {}).get("subtitleArchiveRoot") or "").strip()
    final_zip = []
    package = None
    if zip_source and archive_root:
        archive_destination = Path(archive_root) / f"{title}.zip"
        package_output = package_path(work, title)
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
        "preferredLibrary": decisions.get("library") or "Anime3",
        "expectedStatus": decisions.get("expected_status") or "Complete BDRip",
        "subtitleGroups": [],
        "renameJobs": [],
        "remuxJobs": [],
        "package": package,
        "final": {"mode": str(decisions.get("operation") or "create"), "video": final_video, "zip": final_zip},
    }
    return {
        "plan": plan,
        "issues": issues,
        "release_labels": source_release_labels(videos),
        "release_labels_by_season": labels_by_season,
        "resolved_release_groups": {f"S{season}": value for season, value in season_groups.items()},
        "resolved_release_group": release_group,
        "summary": {"videos": len(final_video), "direct": True},
    }


def season_number(path: Path, work: Path, decisions: dict[str, Any]) -> int:
    try:
        parents = path.relative_to(work).parts[:-1]
    except ValueError:
        parents = path.parts[:-1]
    for part in reversed(parents):
        match = SEASON_RE.match(part.strip())
        if match:
            return int(match.group(1))
    mapping = decisions.get("episode_map")
    if isinstance(mapping, dict):
        relative = relative_media_key(path, work).casefold()
        for raw_key, raw_value in mapping.items():
            key = str(raw_key).replace("\\", "/").strip("/").casefold()
            if key != relative:
                continue
            mapped = re.fullmatch(r"S(\d{2})E\d{2,3}", str(raw_value).strip(), re.IGNORECASE)
            if mapped:
                return int(mapped.group(1))
    return int(decisions.get("season") or 1)


def source_episode(path: Path) -> int | None:
    for pattern in EPISODE_PATTERNS:
        for match in pattern.finditer(path.stem):
            value = int(match.group(1))
            if value not in IGNORED_NUMBERS:
                return value
    return None


def _season_release_groups(
    work: Path,
    videos: list[dict[str, Any]],
    decisions: dict[str, Any],
    history: dict[str, str] | None,
) -> tuple[dict[int, str], str | None, list[dict[str, Any]], dict[str, list[str]]]:
    """Resolve one release label per season while supporting a global task label."""
    by_season: dict[str, list[dict[str, Any]]] = {}
    for item in videos:
        path = Path(item["file"]["path"])
        if not any(track.get("type") == "video" for track in item.get("tracks", [])):
            continue
        season = season_number(path, work, decisions)
        by_season.setdefault(str(season), []).append(item)
    labels_by_season = {
        season: source_release_labels(items) for season, items in sorted(by_season.items())
    }
    raw = decisions.get("release_group_by_season")
    if raw is None:
        labels = source_release_labels(videos)
        group, issues = resolve_release_group(labels, decisions, history or load_release_history())
        return {}, group, issues, labels_by_season
    if not isinstance(raw, dict):
        return {}, None, [{"code": "RELEASE_GROUP_BY_SEASON_INVALID", "detail": "must be an object"}], labels_by_season

    groups: dict[int, str] = {}
    issues: list[dict[str, Any]] = []
    for raw_season, raw_group in raw.items():
        key = str(raw_season).strip().upper()
        match = re.fullmatch(r"S?(\d+)", key)
        value = str(raw_group or "").strip()
        if not match or not value:
            issues.append({"code": "RELEASE_GROUP_BY_SEASON_INVALID", "season": str(raw_season)})
            continue
        season = int(match.group(1))
        if season in groups:
            issues.append({"code": "RELEASE_GROUP_BY_SEASON_DUPLICATE", "season": f"S{season}"})
            continue
        groups[season] = value
    expected = {int(season) for season in by_season}
    missing = sorted(expected - set(groups))
    extra = sorted(set(groups) - expected)
    if missing:
        issues.append({"code": "RELEASE_GROUP_BY_SEASON_INCOMPLETE", "missing": [f"S{value}" for value in missing]})
    if extra:
        issues.append({"code": "RELEASE_GROUP_BY_SEASON_UNKNOWN", "seasons": [f"S{value}" for value in extra]})
    return groups, None, issues, labels_by_season


def _release_group_for_season(season: int, season_groups: dict[int, str], global_group: str | None) -> str:
    return season_groups.get(season) or global_group or ""


def natural_name_parts(value: str) -> tuple[tuple[int, Any], ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", normalized)
        if part
    )


def stable_name_key(path: Path) -> tuple[tuple[tuple[int, Any], ...], str]:
    absolute = unicodedata.normalize("NFKC", str(path.resolve(strict=False))).casefold()
    return natural_name_parts(path.name), absolute


def explicit_episode(
    path: Path, work: Path, season: int, decisions: dict[str, Any]
) -> tuple[int | None, bool, dict[str, Any] | None]:
    mapping = decisions.get("episode_map") or {}
    if not isinstance(mapping, dict):
        return None, True, {"code": "EPISODE_MAP_INVALID"}
    relative = relative_media_key(path, work)
    normalized = {str(key).replace("\\", "/").strip("/").casefold(): value for key, value in mapping.items()}
    if relative.casefold() not in normalized:
        return None, False, None
    value = normalized[relative.casefold()]
    match = re.fullmatch(r"S(\d{2})E(\d{2,3})", str(value).strip(), re.IGNORECASE)
    if not match:
        return None, True, {"code": "EPISODE_MAPPING_INVALID", "file": relative, "value": value}
    mapped_season, episode = int(match.group(1)), int(match.group(2))
    if mapped_season != season:
        return None, True, {
            "code": "EPISODE_MAPPING_SEASON_MISMATCH", "file": relative,
            "expectedSeason": season, "mappedSeason": mapped_season,
        }
    if episode <= 0:
        return None, True, {"code": "EPISODE_MAPPING_INVALID", "file": relative, "value": value}
    return episode, True, None


def episode_map_source_issues(
    work: Path, decisions: dict[str, Any], paths: list[Path]
) -> list[dict[str, Any]]:
    if "episode_map" not in decisions:
        return []
    mapping = decisions.get("episode_map")
    if not isinstance(mapping, dict):
        return [{"code": "EPISODE_MAP_INVALID"}]
    known = {relative_media_key(path, work).casefold() for path in paths}
    issues: list[dict[str, Any]] = []
    for raw_key in mapping:
        key = str(raw_key).replace("\\", "/").strip("/")
        if not key or re.match(r"^[A-Za-z]:", key) or key.startswith("/") or ".." in key.split("/"):
            issues.append({"code": "EPISODE_MAP_INVALID", "path": str(raw_key)})
        elif key.casefold() not in known:
            issues.append({"code": "EPISODE_MAPPING_SOURCE_MISSING", "path": key})
    return issues


def is_ass_track(track: dict[str, Any]) -> bool:
    value = f"{track.get('codecId', '')} {track.get('format', '')}".casefold()
    return track.get("type") == "subtitles" and ("ass" in value or "ssa" in value)


def _subtitle_group(item: dict[str, Any]) -> str:
    group = str(item.get("group") or ".")
    directory = Path(group).name if group not in {"", "."} else "字幕"
    if re.match(r"^(?:JASC|JATC|SC|TC)(?:\s|$)", directory, flags=re.IGNORECASE):
        return directory
    path = Path(str(item.get("file", {}).get("path") or ""))
    stem = path.stem.casefold()
    if ".zh-cht" in stem or ".cht" in stem or re.search(r"(?:^|[._\- ])tc(?:[._\- ]|$)", stem):
        language = "TC"
    elif ".zh-chs" in stem or ".chs" in stem or re.search(r"(?:^|[._\- ])sc(?:[._\- ]|$)", stem):
        language = "SC"
    else:
        prefix = directory[:3].casefold()
        language = directory[:2].upper() if prefix in {"sc ", "tc "} else ""
    base = directory[3:] if directory[:3].casefold() in {"sc ", "tc "} else directory
    return f"{language} {base}".strip() if language else base


def _normalized_group(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _season_key(value: Any) -> int | None:
    match = re.fullmatch(r"S?(\d+)", str(value).strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _exact_group_order(
    actual: list[str], requested: Any
) -> tuple[list[str] | None, bool]:
    if not isinstance(requested, list):
        return None, False
    names = [str(value).strip() for value in requested]
    actual_by_key = {_normalized_group(name): name for name in actual}
    requested_keys = [_normalized_group(name) for name in names]
    valid = (
        all(names)
        and len(requested_keys) == len(set(requested_keys))
        and set(requested_keys) == set(actual_by_key)
    )
    if not valid:
        return None, False
    return [actual_by_key[key] for key in requested_keys], True


def _tv_subtitle_orders(
    groups_by_season: dict[int, list[str]], decisions: dict[str, Any]
) -> tuple[dict[int, list[str]], list[dict[str, Any]]]:
    """Resolve TV subtitle priority independently for every season."""
    canonical: dict[int, list[str]] = {}
    for season, groups in groups_by_season.items():
        by_key: dict[str, str] = {}
        for group in groups:
            by_key.setdefault(_normalized_group(group), group)
        canonical[season] = list(by_key.values())

    issues: list[dict[str, Any]] = []
    supplied = decisions.get("subtitle_order_by_season")
    explicit: dict[int, Any] = {}
    if supplied is not None:
        if not isinstance(supplied, dict):
            issues.append({"code": "SUBTITLE_ORDER_BY_SEASON_INVALID", "detail": "must be an object"})
        else:
            for raw_season, requested in supplied.items():
                season = _season_key(raw_season)
                if season is None or season in explicit or season not in canonical:
                    issues.append({
                        "code": "SUBTITLE_ORDER_BY_SEASON_INVALID",
                        "season": str(raw_season),
                    })
                    continue
                explicit[season] = requested

    legacy = decisions.get("subtitle_order")
    legacy_names = [str(value).strip() for value in legacy] if isinstance(legacy, list) else []
    legacy_keys = [_normalized_group(value) for value in legacy_names]
    legacy_needed = any(season not in explicit and len(groups) > 1 for season, groups in canonical.items())
    legacy_valid_shape = (
        not legacy_needed
        or legacy is None
        or (
            isinstance(legacy, list)
            and all(legacy_names)
            and len(legacy_keys) == len(set(legacy_keys))
        )
    )
    actual_keys = {
        _normalized_group(group)
        for groups in canonical.values()
        for group in groups
    }
    if legacy_needed and legacy is not None and legacy_valid_shape and set(legacy_keys) != actual_keys:
        legacy_valid_shape = False
    if legacy_needed and not legacy_valid_shape:
        issues.append({
            "code": "SUBTITLE_ORDER_INVALID",
            "groups": [group for season in sorted(canonical) for group in canonical[season]],
            "requested": legacy,
        })

    coverage: dict[str, int] = {}
    for groups in canonical.values():
        for key in {_normalized_group(group) for group in groups}:
            coverage[key] = coverage.get(key, 0) + 1

    resolved: dict[int, list[str]] = {}
    last_seen: dict[str, int] = {}
    previous_order: list[str] = []
    for season in sorted(canonical):
        groups = canonical[season]
        if not groups:
            resolved[season] = []
            continue

        if season in explicit:
            order, valid = _exact_group_order(groups, explicit[season])
            if not valid:
                issues.append({
                    "code": "SUBTITLE_ORDER_BY_SEASON_INVALID",
                    "season": f"S{season}",
                    "groups": groups,
                    "requested": explicit[season],
                })
                order = groups
        elif legacy_needed and legacy is not None and legacy_valid_shape:
            actual_by_key = {_normalized_group(group): group for group in groups}
            filtered = [actual_by_key[key] for key in legacy_keys if key in actual_by_key]
            if len(filtered) != len(groups):
                issues.append({
                    "code": "SUBTITLE_ORDER_INVALID",
                    "season": f"S{season}",
                    "groups": groups,
                    "requested": legacy_names,
                })
                order = groups
            else:
                order = filtered
        elif len(groups) == 1:
            order = groups
        else:
            previous_rank = {_normalized_group(group): rank for rank, group in enumerate(previous_order)}

            def priority(group: str) -> tuple[int, int, int]:
                key = _normalized_group(group)
                if key in previous_rank:
                    return 0, previous_rank[key], 0
                if key in last_seen:
                    return 1, -last_seen[key], -coverage[key]
                return 2, -coverage[key], 0

            ranked = sorted(groups, key=priority)
            tied = False
            for left, right in zip(ranked, ranked[1:]):
                if priority(left) == priority(right):
                    tied = True
                    break
            if tied:
                issues.append({
                    "code": "SUBTITLE_ORDER_BY_SEASON_REQUIRED",
                    "season": f"S{season}",
                    "groups": groups,
                })
                order = groups
            else:
                order = ranked

        resolved[season] = order
        previous_order = order
        for group in groups:
            last_seen[_normalized_group(group)] = season
    return resolved, issues


def _episode_records(
    work: Path, videos: list[dict[str, Any]], decisions: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    primary = [item for item in videos if Path(item["file"]["path"]).suffix.casefold() != ".mka" and any(track.get("type") == "video" for track in item.get("tracks", []))]
    by_season: dict[int, list[dict[str, Any]]] = {}
    for item in primary:
        path = Path(item["file"]["path"])
        season = season_number(path, work, decisions)
        if season == 0:
            by_season.setdefault(season, []).append({"inventory": item, "path": path})
            continue
        episode = source_episode(path)
        if episode is None:
            explicit, present, mapping_issue = explicit_episode(path, work, season, decisions)
            if mapping_issue:
                issues.append(mapping_issue)
                continue
            if not present or explicit is None:
                issues.append({"code": "EPISODE_NUMBER_REQUIRED", "file": path.name})
                continue
            episode = int(explicit)
        by_season.setdefault(season, []).append({"inventory": item, "path": path, "source_episode": episode})
    records: list[dict[str, Any]] = []
    for season, items in sorted(by_season.items()):
        if season == 0:
            ordered = sorted(items, key=lambda value: stable_name_key(value["path"]))
            resolved = [explicit_episode(item["path"], work, season, decisions) for item in ordered]
            mapping_issues = [issue for _, _, issue in resolved if issue]
            if mapping_issues:
                issues.extend(mapping_issues)
                continue
            explicit = [value for value, _, _ in resolved]
            provided = [present for _, present, _ in resolved]
            if any(provided) and not all(provided):
                issues.append({"code": "S0_MAPPING_INCOMPLETE", "kind": "video"})
                continue
            targets = [int(value) for value in explicit if value is not None] if all(provided) else list(range(1, len(ordered) + 1))
            mapped = [
                {**item, "season": season, "source_episode": index, "target_episode": target}
                for index, (item, target) in enumerate(zip(ordered, targets), start=1)
            ]
            if len(targets) != len(set(targets)):
                issues.append({"code": "S0_MAPPING_DUPLICATE", "kind": "video", "targets": targets})
                continue
            records.extend(mapped)
            continue
        values = [item["source_episode"] for item in items]
        if len(values) != len(set(values)):
            issues.append({"code": "EPISODE_NUMBER_AMBIGUOUS", "season": season, "episodes": values})
            continue
        ordered = sorted(items, key=lambda value: value["source_episode"])
        resolved = [explicit_episode(item["path"], work, season, decisions) for item in ordered]
        mapping_issues = [issue for _, _, issue in resolved if issue]
        if mapping_issues:
            issues.extend(mapping_issues)
            continue
        targets = [value if present else index for index, (value, present, _) in enumerate(resolved, start=1)]
        if len(targets) != len(set(targets)):
            issues.append({"code": "EPISODE_TARGET_DUPLICATE", "season": season, "targets": targets})
            continue
        for item, target in zip(ordered, targets):
            records.append({**item, "season": season, "target_episode": int(target)})
    return records, issues


def build_tv_plan(
    work: Path,
    manifest: dict[str, Any],
    decisions: dict[str, Any],
    history: dict[str, str] | None = None,
    completed_steps: set[str] | None = None,
) -> dict[str, Any]:
    work = work.resolve()
    title = str(decisions.get("title") or work.name).strip()
    discovery = manifest.get("discovery", {})
    completed_steps = completed_steps or set()
    videos = [item for item in discovery.get("videos", []) if item.get("status") == "OK"]
    if "remux" in completed_steps:
        generated_paths: set[Path] = set()
        standard = re.compile(rf"^{re.escape(title)}\.S\d{{2}}E\d{{2}}(?P<number> \([1-9]\d*\))?$", re.IGNORECASE)
        for item in videos:
            path = Path(item["file"]["path"])
            if path.suffix.casefold() != ".mka" and standard.match(path.stem):
                generated_paths.add(path)
        videos = [item for item in videos if Path(item["file"]["path"]) not in generated_paths]
    season_groups, release_group, issues, labels_by_season = _season_release_groups(
        work, videos, decisions, history
    )
    records, episode_issues = _episode_records(work, videos, decisions)
    issues.extend(episode_issues)
    if not records:
        issues.append({"code": "TV_VIDEO_REQUIRED"})

    mka_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in videos:
        path = Path(item["file"]["path"])
        if path.suffix.casefold() != ".mka":
            continue
        parent = relative_media_key(path.parent, work).casefold()
        mka_candidates.setdefault((parent, path.stem.casefold()), []).append(item)
    for key, candidates in mka_candidates.items():
        if len(candidates) > 1:
            issues.append({"code": "MKA_PAIR_AMBIGUOUS", "parent": key[0], "stem": key[1], "count": len(candidates)})
    mka_by_key = {key: values[0] for key, values in mka_candidates.items() if len(values) == 1}
    subtitles = list(discovery.get("subtitles", []))
    embedded_status = str(discovery.get("embeddedSubtitles", {}).get("status") or "MISSING")
    if not subtitles and embedded_status not in {"COMPLETE", "METADATA_ONLY"}:
        issues.append({"code": "ASS_REQUIRED", "detail": embedded_status})
    if "subtitle" in completed_steps:
        generated_paths: set[Path] = set()
        standard = re.compile(rf"^{re.escape(title)}\.S\d{{2}}E\d{{2}}(?P<number> \([1-9]\d*\))?$", re.IGNORECASE)
        for item in subtitles:
            path = Path(item["file"]["path"])
            if standard.match(path.stem):
                generated_paths.add(path)
        subtitles = [item for item in subtitles if Path(item["file"]["path"]) not in generated_paths]
    mapping_paths = [
        Path(item["file"]["path"])
        for item in [*videos, *subtitles]
        if Path(item["file"]["path"]).suffix.casefold() != ".mka"
    ]
    issues.extend(episode_map_source_issues(work, decisions, mapping_paths))
    audio_selection, audio_selection_issues = track_selection_map(decisions, "audio_keep")
    video_selection, video_selection_issues = track_selection_map(decisions, "video_keep")
    issues.extend(audio_selection_issues)
    issues.extend(video_selection_issues)
    subtitle_groups: dict[str, list[str]] = {}
    rename_jobs: list[dict[str, str]] = []
    remux_jobs: list[dict[str, Any]] = []
    package_entries: list[dict[str, str]] = []
    final_video: list[dict[str, str]] = []

    mapped = {(item["season"], item["source_episode"]): item for item in records}
    mapped_targets = {(item["season"], item["target_episode"]): item for item in records}
    subtitle_by_episode: dict[tuple[int, int], list[dict[str, Any]]] = {}
    s0_subtitle_groups: dict[str, list[dict[str, Any]]] = {}
    for subtitle in subtitles:
        path = Path(subtitle["file"]["path"])
        season = season_number(path, work, decisions)
        if season == 0:
            s0_subtitle_groups.setdefault(_subtitle_group(subtitle), []).append(subtitle)
            continue
        explicit, present, mapping_issue = explicit_episode(path, work, season, decisions)
        if mapping_issue:
            issues.append({**mapping_issue, "kind": "subtitle", "group": _subtitle_group(subtitle)})
            continue
        if present:
            record = mapped_targets.get((season, int(explicit))) if explicit is not None else None
            if record is None:
                issues.append({
                    "code": "EPISODE_MAPPING_TARGET_MISSING",
                    "file": path.name,
                    "season": season,
                    "target": explicit,
                })
                continue
            subtitle_by_episode.setdefault((season, record["source_episode"]), []).append(subtitle)
            continue
        episode = source_episode(path)
        if episode is None or (season, episode) not in mapped:
            issues.append({"code": "SUBTITLE_EPISODE_UNMATCHED", "file": path.name})
            continue
        subtitle_by_episode.setdefault((season, episode), []).append(subtitle)

    s0_records = sorted(
        (item for item in records if item["season"] == 0),
        key=lambda item: item["source_episode"],
    )
    s0_by_target = {item["target_episode"]: item for item in s0_records}
    for group, items in s0_subtitle_groups.items():
        ordered = sorted(items, key=lambda item: stable_name_key(Path(item["file"]["path"])))
        resolved = [explicit_episode(Path(item["file"]["path"]), work, 0, decisions) for item in ordered]
        mapping_issues = [issue for _, _, issue in resolved if issue]
        if mapping_issues:
            issues.extend({**issue, "kind": "subtitle", "group": group} for issue in mapping_issues)
            continue
        explicit = [value for value, _, _ in resolved]
        provided = [present for _, present, _ in resolved]
        if any(provided):
            if not all(provided):
                issues.append({"code": "S0_MAPPING_INCOMPLETE", "kind": "subtitle", "group": group})
                continue
            targets = [int(value) for value in explicit if value is not None]
            if len(targets) != len(set(targets)):
                issues.append({"code": "S0_MAPPING_DUPLICATE", "kind": "subtitle", "group": group, "targets": targets})
                continue
            missing = [target for target in targets if target not in s0_by_target]
            if missing:
                issues.append({"code": "S0_MAPPING_TARGET_MISSING", "kind": "subtitle", "group": group, "targets": missing})
                continue
            for subtitle, target in zip(ordered, targets):
                record = s0_by_target[target]
                subtitle_by_episode.setdefault((0, record["source_episode"]), []).append(subtitle)
            continue
        if len(ordered) != len(s0_records):
            issues.append(
                {
                    "code": "S0_MAPPING_COUNT_MISMATCH",
                    "group": group,
                    "videos": len(s0_records),
                    "subtitles": len(ordered),
                }
            )
            continue
        for subtitle, record in zip(ordered, s0_records):
            subtitle_by_episode.setdefault((0, record["source_episode"]), []).append(subtitle)

    embedded_by_source: dict[Path, tuple[list[dict[str, Any]], list[str]]] = {}
    groups_by_season: dict[int, list[str]] = {}
    if subtitles:
        for (season, _), episode_items in subtitle_by_episode.items():
            groups_by_season.setdefault(season, []).extend(_subtitle_group(item) for item in episode_items)
    elif embedded_status in {"COMPLETE", "METADATA_ONLY"}:
        for record in records:
            tracks = [track for track in record["inventory"].get("tracks", []) if is_ass_track(track)]
            names, name_issues = embedded_track_names(
                [str(track.get("title") or "").strip() for track in tracks], decisions
            )
            issues.extend({**issue, "file": record["path"].name} for issue in name_issues)
            embedded_by_source[record["path"]] = (tracks, names)
            groups_by_season.setdefault(record["season"], []).extend(names)

    subtitle_orders, subtitle_issues = _tv_subtitle_orders(groups_by_season, decisions)
    issues.extend(subtitle_issues)
    subtitle_ranks = {
        season: {_normalized_group(name): rank for rank, name in enumerate(order)}
        for season, order in subtitle_orders.items()
    }

    for record in records:
        source = record["path"]
        season = record["season"]
        source_number = record["source_episode"]
        target_number = record["target_episode"]
        record_release_group = _release_group_for_season(season, season_groups, release_group)
        base_name = f"{title}.S{season:02d}E{target_number:02d}"
        output = tv_video_path(work, title, season, target_number)
        sources = [(source, record["inventory"])]
        paired = mka_by_key.get((relative_media_key(source.parent, work).casefold(), source.stem.casefold()))
        if paired:
            sources.append((Path(paired["file"]["path"]), paired))

        video_candidates = [track for track in record["inventory"].get("tracks", []) if track.get("type") == "video"]
        requested_video = selected_track_keys(video_selection, source, work)
        if len(video_candidates) > 1:
            if requested_video is None or len(requested_video) != 1:
                issues.append({"code": "VIDEO_TRACK_SELECTION_REQUIRED", "file": relative_media_key(source, work), "tracks": [str(track.get("trackKey")) for track in video_candidates]})
                video_candidates = []
            else:
                video_candidates = [track for track in video_candidates if str(track.get("trackKey")) in requested_video]
                if len(video_candidates) != 1:
                    issues.append({"code": "VIDEO_KEEP_UNKNOWN", "file": relative_media_key(source, work), "tracks": sorted(requested_video)})
        elif requested_video is not None and (len(video_candidates) != 1 or requested_video != {str(video_candidates[0].get("trackKey"))}):
            issues.append({"code": "VIDEO_KEEP_UNKNOWN", "file": relative_media_key(source, work), "tracks": sorted(requested_video)})
        video_tracks = [(0, track) for track in video_candidates]
        audio_candidates: list[tuple[int, Path, dict[str, Any]]] = []
        for source_index, (input_path, inventory) in enumerate(sources):
            for track in inventory.get("tracks", []):
                if track.get("type") != "audio" or COMMENTARY_RE.search(str(track.get("title") or "")):
                    continue
                audio_candidates.append((source_index, input_path, track))
        audio_tracks: list[tuple[int, Path, dict[str, Any]]] = []
        by_channels: dict[Any, list[tuple[int, Path, dict[str, Any]]]] = {}
        for candidate in audio_candidates:
            by_channels.setdefault(candidate[2].get("channels"), []).append(candidate)
        for channels, candidates in by_channels.items():
            if len(candidates) == 1:
                audio_tracks.extend(candidates)
                continue
            selected = [
                candidate for candidate in candidates
                if (selected_track_keys(audio_selection, candidate[1], work) or set()) & {str(candidate[2].get("trackKey"))}
            ]
            if not selected:
                issues.append({
                    "code": "AUDIO_SAME_CHANNEL_AMBIGUOUS", "file": relative_media_key(source, work),
                    "channels": [str(channels)],
                    "candidates": [f"{relative_media_key(path, work)}#{track.get('trackKey')}" for _, path, track in candidates],
                })
            else:
                audio_tracks.extend(selected)
        if not audio_tracks:
            issues.append({"code": "MAIN_AUDIO_REQUIRED", "file": source.name})

        audio_tracks.sort(key=lambda item: (0 if item[2].get("channels") == 2 else 1, -(item[2].get("channels") or 0), item[0]))
        embedded_tracks, embedded_names = embedded_by_source.get(source, ([], []))
        season_rank = subtitle_ranks.get(season, {})
        if embedded_names:
            embedded_order = sorted(
                range(len(embedded_tracks)),
                key=lambda index: season_rank.get(_normalized_group(embedded_names[index]), len(season_rank)),
            )
            embedded_tracks = [embedded_tracks[index] for index in embedded_order]
            embedded_names = [embedded_names[index] for index in embedded_order]
        if len(sources) > 1 and not any(source_index == 1 for source_index, _, _ in audio_tracks):
            sources = sources[:1]
        arguments: list[str] = []
        track_sources: list[dict[str, Any]] = []
        expected_tracks: list[dict[str, Any]] = []
        track_order: list[str] = []
        for source_index, track in video_tracks:
            if source_index < len(sources):
                token = marker(source_index, str(track["trackKey"]))
                track_order.append(f"{source_index}:{token}")
                expected_tracks.append({"type": "video", "language": "jpn", "name": record_release_group, "default": True, "forced": False})
        single_20 = len(audio_tracks) == 1 and audio_tracks[0][2].get("channels") == 2
        for global_index, (source_index, _, track) in enumerate(audio_tracks):
            token = marker(source_index, str(track["trackKey"]))
            name = "" if single_20 else channel_name(track.get("channels"))
            track_order.append(f"{source_index}:{token}")
            expected_tracks.append({
                "type": "audio",
                "language": "jpn",
                "name": name,
                "default": global_index == 0,
                "forced": False,
                "channels": track.get("channels"),
            })
        for source_index, (input_path, inventory) in enumerate(sources):
            selected_video = [track for index, track in video_tracks if index == source_index]
            selected_audio = [track for index, _, track in audio_tracks if index == source_index]
            selected_embedded = embedded_tracks if source_index == 0 else []
            selected = [*selected_video, *selected_audio, *selected_embedded]
            track_sources.append({"source": str(input_path), "selectedTrackKeys": [str(track["trackKey"]) for track in selected]})
            if selected_video:
                video_ids = ",".join(marker(source_index, str(track["trackKey"])) for track in selected_video)
                arguments.extend(["--video-tracks", video_ids])
                for track in selected_video:
                    token = marker(source_index, str(track["trackKey"]))
                    arguments.extend([
                        "--language", f"{token}:jpn",
                        "--track-name", f"{token}:{record_release_group}",
                        "--default-track-flag", f"{token}:yes",
                        "--forced-display-flag", f"{token}:no",
                    ])
            else:
                arguments.append("--no-video")
            if selected_audio:
                audio_ids = ",".join(marker(source_index, str(track["trackKey"])) for track in selected_audio)
                arguments.extend(["--audio-tracks", audio_ids])
                for global_index, (global_source_index, _, track) in enumerate(audio_tracks):
                    if global_source_index != source_index or not any(track.get("trackKey") == selected.get("trackKey") for selected in selected_audio):
                        continue
                    token = marker(source_index, str(track["trackKey"]))
                    name = "" if single_20 else channel_name(track.get("channels"))
                    arguments.extend([
                        "--language", f"{token}:jpn",
                        "--track-name", f"{token}:{name}",
                        "--default-track-flag", f"{token}:{'yes' if global_index == 0 else 'no'}",
                        "--forced-display-flag", f"{token}:no",
                    ])
            else:
                arguments.append("--no-audio")
            if selected_embedded:
                subtitle_ids = ",".join(marker(source_index, str(track["trackKey"])) for track in selected_embedded)
                arguments.extend(["--subtitle-tracks", subtitle_ids])
                for subtitle_index, track in enumerate(selected_embedded):
                    token = marker(source_index, str(track["trackKey"]))
                    group = embedded_names[subtitle_index]
                    default = subtitle_index == 0
                    arguments.extend([
                        "--language", f"{token}:chi",
                        "--track-name", f"{token}:{group}",
                        "--default-track-flag", f"{token}:{'yes' if default else 'no'}",
                        "--forced-display-flag", f"{token}:no",
                    ])
                    track_order.append(f"{source_index}:{token}")
                    expected_tracks.append({"type": "subtitles", "language": "chi", "name": group, "default": default, "forced": False})
            else:
                arguments.append("--no-subtitles")
            if subtitles:
                arguments.append("--no-attachments")
            if source_index > 0 or not decisions.get("keep_chapters", True):
                arguments.append("--no-chapters")
            arguments.append(str(input_path))

        episode_subtitles = sorted(
            subtitle_by_episode.get((season, source_number), []),
            key=lambda item: season_rank.get(_normalized_group(_subtitle_group(item)), len(season_rank)),
        )
        for subtitle_index, subtitle in enumerate(episode_subtitles):
            source_ass = Path(subtitle["file"]["path"])
            group = _subtitle_group(subtitle)
            # Keep the archive's stable TV layout even when source subtitles
            # are supplied directly under the task root instead of S1/.
            labeled_parent = Path(f"S{season}") / group
            planned_ass = tv_subtitle_path(work, title, season, target_number, group)
            subset_ass = source_ass.with_suffix(".assfonts" + source_ass.suffix)
            subtitle_groups.setdefault(group, []).append(str(source_ass))
            rename_jobs.append({"source": str(subset_ass), "target": str(planned_ass)})
            package_entries.append({
                "source": str(planned_ass),
                "arcname": str((labeled_parent / f"{base_name}.ass").as_posix()),
            })
            arguments.extend([
                "--no-video", "--no-audio", "--no-chapters",
                "--language", "0:chi",
                "--track-name", f"0:{group}",
                "--default-track-flag", f"0:{'yes' if subtitle_index == 0 else 'no'}",
                "--forced-display-flag", "0:no",
                str(planned_ass),
            ])
            subtitle_input_index = len(sources) + subtitle_index
            track_order.append(f"{subtitle_input_index}:0")
            expected_tracks.append({
                "type": "subtitles",
                "language": "chi",
                "name": group,
                "default": subtitle_index == 0,
                "forced": False,
            })
        if track_order:
            arguments.extend(["--track-order", ",".join(track_order)])
        expected_chapters = bool(record["inventory"].get("chapters", {}).get("present")) if decisions.get("keep_chapters", True) else False
        embedded_attachments = [
            str(value.get("name") or "")
            for value in record["inventory"].get("mkvInventory", {}).get("attachments", [])
            if value.get("name")
        ] if embedded_tracks else []
        expected_attachments = [str(value) for value in decisions.get("expected_attachments", embedded_attachments)]
        remux_jobs.append({
            "source": str(source),
            "output": str(output),
            "arguments": arguments,
            "trackSources": track_sources,
            "chapters": "preserve" if expected_chapters else "drop",
            "expectedTracks": expected_tracks,
            "expectedChapters": expected_chapters,
            "expectedAttachments": expected_attachments,
        })
        final_video.append({
            "source": str(output),
            "relativePath": tv_video_relative(title, season, target_number),
            "expectedTracks": expected_tracks,
            "expectedChapters": expected_chapters,
            "expectedAttachments": expected_attachments,
        })

    config = json.loads(read_text(Path(manifest["configPath"])))
    archive_root_value = str(config.get("paths", {}).get("subtitleArchiveRoot") or "").strip()
    archive_root = Path(archive_root_value) if archive_root_value else None
    package_output = package_path(work, title)
    final_zip = []
    package_plan = None
    if package_entries and archive_root is not None:
        archive_destination = archive_root / f"{title}.zip"
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
        "preferredLibrary": decisions.get("library") or config.get("defaults", {}).get("library", "Anime3"),
        "expectedStatus": decisions.get("expected_status") or "Complete BDRip",
        "subtitleGroups": [{"name": group, "inputs": paths} for group, paths in subtitle_groups.items()],
        "renameJobs": rename_jobs,
        "remuxJobs": remux_jobs,
        "package": package_plan,
        "final": {
            "mode": "replace" if manifest.get("taskMode") == "replacement" else "create",
            "video": final_video,
            "zip": final_zip,
        },
    }
    return {
        "plan": plan,
        "issues": issues,
        "release_labels": source_release_labels(videos),
        "release_labels_by_season": labels_by_season,
        "resolved_release_group": release_group,
        "resolved_release_groups": {f"S{season}": value for season, value in season_groups.items()},
        "summary": {"episodes": len(records), "subtitles": len(subtitles), "mka_pairs": sum(1 for item in records if (relative_media_key(item["path"].parent, work).casefold(), item["path"].stem.casefold()) in mka_by_key)},
    }
