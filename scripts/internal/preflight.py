"""Non-destructive media, subtitle, font, and destination preflight coordination."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable

from archive_rules import (
    ALLOWED_HIDDEN_NAMES,
    BACKEND_CACHE_SCHEMA,
    WORKFLOW_REVISION,
    backend_cache_path,
    resolve_path,
    route_branch,
    temporary_path,
)
from internal import media_inspection
from internal.metadata_client import credential_presence
from internal.metadata_match import inspect_metadata
from internal.errors import WorkflowError
from internal.manifest import empty_stage_map, save_manifest, set_stage, utc_now
from internal.signatures import canonical_metadata_digest, file_signature, signature_matches
from internal.subtitle_archive import ZIP_METADATA_ENCODING
from internal.subtitle_pipeline import (
    FONT_SUFFIXES,
    SUBTITLE_SUFFIXES,
    fallback_font_database_path,
    font_file_records,
    load_assfonts_database,
    load_fallback_font_database,
    normalize_font_name,
    parse_ass_font_requirements,
    resolve_font_availability,
)


VIDEO_SUFFIXES = {".mkv", ".mp4", ".mka"}
PREFLIGHT_CACHE_VERSION = 1
MOVIE_PCM_ANALYSIS_VERSION = 1


def list_task_files(work_path: Path) -> tuple[list[Path], list[Path]]:
    videos: list[Path] = []
    subtitles: list[Path] = []
    for path in work_path.rglob("*"):
        if not path.is_file() or any(part in ALLOWED_HIDDEN_NAMES for part in path.parts):
            continue
        suffix = path.suffix.casefold()
        if suffix in VIDEO_SUFFIXES:
            videos.append(path.resolve())
        elif suffix in SUBTITLE_SUFFIXES and not path.name.casefold().endswith(".assfonts.ass"):
            subtitles.append(path.resolve())
    return sorted(videos), sorted(subtitles)


def _mkvextract_path(mkvmerge: str) -> Path:
    executable = Path(mkvmerge)
    candidates = [executable.with_name("mkvextract.exe"), executable.with_name("mkvextract")]
    selected = next((item for item in candidates if item.is_file()), None)
    if selected is None:
        raise WorkflowError("MKVEXTRACT_NOT_FOUND", f"mkvextract was not found beside mkvmerge: {mkvmerge}")
    return selected


def _is_ass_track(track: dict[str, Any]) -> bool:
    value = f"{track.get('codec', '')} {track.get('codecId', '')}".casefold()
    return track.get("type") == "subtitles" and ("ass" in value or "ssa" in value)


def safe_font_attachment_target(font_dir: Path, attachment: dict[str, Any], ordinal: int) -> Path:
    """Build an extraction path without trusting the MKV attachment name."""

    try:
        attachment_id = str(int(attachment.get("id")))
    except (TypeError, ValueError):
        attachment_id = str(ordinal)
    suffix = Path(str(attachment.get("name") or "")).suffix.casefold()
    if suffix not in FONT_SUFFIXES:
        suffix = ".ttf"
    return font_dir / f"attachment-{attachment_id}{suffix}"


def inspect_embedded_subtitles(
    work: Path,
    video_inventory: list[dict[str, Any]],
    mkvmerge: str,
    *,
    video_inspector: Callable[[Path, str], dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    extractor_resolver: Callable[[str], Path] = _mkvextract_path,
    font_inspector: Callable[[Path], list[dict[str, Any]]] = font_file_records,
    requirement_parser: Callable[[Path], list[dict[str, Any]]] = parse_ass_font_requirements,
) -> dict[str, Any]:
    extractor = extractor_resolver(mkvmerge)
    root = temporary_path(work, "embedded-subtitles")
    extracted_subtitles: list[str] = []
    files: list[dict[str, Any]] = []
    all_missing_fonts: set[str] = set()
    metadata_only = False
    ass_count = 0
    for index, item in enumerate(video_inventory, start=1):
        source = resolve_path(item["file"]["path"])
        if source.suffix.casefold() != ".mkv" or item.get("status") != "OK":
            continue
        inventory = video_inspector(source, mkvmerge)
        if inventory.get("status") != "OK":
            raise WorkflowError("EMBEDDED_INSPECTION_FAILED", inventory.get("error", str(source)))
        item["mkvInventory"] = inventory
        ass_tracks = [track for track in inventory.get("tracks", []) if _is_ass_track(track)]
        if not ass_tracks:
            continue
        ass_count += len(ass_tracks)
        try:
            relative_parent = source.relative_to(work).parent
        except ValueError:
            relative_parent = Path()
        directory = root / relative_parent / f"{index:03d}-{source.stem}"
        font_dir = directory / "fonts"
        font_dir.mkdir(parents=True, exist_ok=True)
        font_records: list[dict[str, Any]] = []
        extracted_fonts: list[dict[str, Any]] = []
        for attachment_index, attachment in enumerate(inventory.get("attachments", []), start=1):
            content_type = str(attachment.get("contentType") or "").casefold()
            name = str(attachment.get("name") or "")
            if "font" not in content_type and Path(name).suffix.casefold() not in FONT_SUFFIXES:
                continue
            target = safe_font_attachment_target(font_dir, attachment, attachment_index)
            result = runner([str(extractor), "attachments", str(source), f"{attachment['id']}:{target}"])
            if result["exitCode"] != 0 or not target.is_file():
                raise WorkflowError("EMBEDDED_FONT_EXTRACT_FAILED", result["stderr"] or result["stdout"])
            font_records.extend(font_inspector(target))
            extracted_fonts.append({"name": name or target.name, "path": str(target), "contentType": attachment.get("contentType")})
        aliases = {normalize_font_name(name) for record in font_records for name in record.get("names", [])}
        track_results = []
        for track in ass_tracks:
            group = str(track.get("name") or "内封").strip()
            safe_group = re.sub(r"[<>:\"/\\|?*]+", "-", group).strip() or "内封"
            group_dir = directory / safe_group
            group_dir.mkdir(parents=True, exist_ok=True)
            target = group_dir / f"{source.stem}.embedded{track['id']}.ass"
            result = runner([str(extractor), "tracks", str(source), f"{track['id']}:{target}"])
            if result["exitCode"] != 0 or not target.is_file():
                raise WorkflowError("EMBEDDED_ASS_EXTRACT_FAILED", result["stderr"] or result["stdout"])
            requirements = requirement_parser(target)
            missing = sorted(value["name"] for value in requirements if value["normalized"] not in aliases)
            all_missing_fonts.update(missing)
            metadata_ok = bool(track.get("name")) and str(track.get("language") or "").casefold() in {"chi", "zho", "zh", "chs", "cht"}
            metadata_only = metadata_only or not metadata_ok
            extracted_subtitles.append(str(target))
            track_results.append({"track": track, "extracted": str(target), "requiredFonts": requirements, "missingFonts": missing, "metadataOk": metadata_ok})
        files.append({"source": str(source), "tracks": track_results, "attachments": extracted_fonts})
    if ass_count == 0:
        status = "MISSING"
    elif all_missing_fonts:
        status = "INCOMPLETE"
    elif metadata_only:
        status = "METADATA_ONLY"
    else:
        status = "COMPLETE"
    return {
        "status": status,
        "assTracks": ass_count,
        "missingFonts": sorted(all_missing_fonts),
        "files": files,
        "extractedSubtitles": extracted_subtitles,
    }


def inspect_media(path: Path, mediainfo: str) -> dict[str, Any]:
    try:
        payload = media_inspection.read_mediainfo_json(path, mediainfo)
        inventory = media_inspection.normalize_mediainfo(payload)
        return {"file": file_signature(path), "status": "OK", **inventory}
    except Exception as exc:
        return {"file": file_signature(path), "status": "FAILED", "error": str(exc)}


def inspection_required_tools(
    args: Any,
    config: dict[str, Any],
    *,
    has_external_subtitles: bool,
) -> list[str]:
    """Return only tools consumed by the selected task capabilities."""

    task = str(getattr(args, "task_mode", "complete-archive"))
    requested = {str(value) for value in (getattr(args, "requested_step", None) or [])}
    selected = {str(value) for value in (getattr(args, "selected_capability", None) or [])}
    tools = {"mediainfo"}
    if selected:
        needs_mkvmerge = bool(selected & {"movie-audio", "remux", "video-delivery"}) or (
            not has_external_subtitles
            and bool(selected & {"subtitle", "subtitle-package", "subtitle-delivery"})
        )
    else:
        needs_mkvmerge = task != "local-only" or "remux" in requested or not has_external_subtitles
    if needs_mkvmerge:
        tools.add("mkvmerge")
    subtitle_processing = (
        bool(selected & {"subtitle", "remux", "subtitle-package", "subtitle-delivery", "video-delivery"})
        if selected
        else task in {"complete-archive", "replacement"} or bool(requested & {"subtitle", "remux", "package"})
    )
    if has_external_subtitles and subtitle_processing:
        tools.add("assfonts")
    if getattr(args, "movie_audio_replacement", False):
        tools.update({"mkvmerge", "ffprobe", "ffmpeg", "mkvinfo"})
    kdocs_selected = bool(getattr(args, "kdocs_tracker", task != "local-only"))
    if kdocs_selected and bool(config.get("tracker", {}).get("enabled")):
        tools.add("kdocs-cli")
    return sorted(tools)


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(resolve_path(path)))


def _safe_file_signature(path: Path | str) -> dict[str, Any]:
    selected = resolve_path(path)
    try:
        return file_signature(selected)
    except (FileNotFoundError, OSError):
        return {"path": str(selected), "kind": "missing"}


def _tool_identity(config: dict[str, Any], name: str, resolved: str | None = None) -> dict[str, Any]:
    value = resolved if resolved is not None else config.get("tools", {}).get(name)
    if not value:
        return {"name": name, "configured": False}
    return {"name": name, "configured": True, "file": _safe_file_signature(str(value))}


def _component_key(value: Any) -> str:
    return canonical_metadata_digest(value)


def _load_incremental_manifest(path: Path, work: Path, enabled: bool) -> dict[str, Any] | None:
    if not enabled or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schemaVersion") != BACKEND_CACHE_SCHEMA or value.get("workflowRevision") != WORKFLOW_REVISION:
        return None
    if _path_key(value.get("workPath", "")) != _path_key(work):
        return None
    cache = value.get("preflightCache")
    if not isinstance(cache, dict) or cache.get("version") != PREFLIGHT_CACHE_VERSION:
        return None
    if not isinstance(cache.get("components"), dict) or not isinstance(value.get("discovery"), dict):
        return None
    return value


def _previous_component(previous: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if previous is None:
        return {}
    value = previous.get("preflightCache", {}).get("components", {}).get(name)
    return value if isinstance(value, dict) else {}


def _component_matches(previous: dict[str, Any] | None, name: str, key: str) -> bool:
    return _previous_component(previous, name).get("key") == key


def _signatures_still_match(signatures: Any) -> bool:
    return isinstance(signatures, list) and all(isinstance(item, dict) and signature_matches(item) for item in signatures)


def _embedded_cache_ready(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    extracted = value.get("extractedSubtitles") or []
    if value.get("assTracks", 0) and not extracted:
        return False
    return all(resolve_path(path).is_file() for path in extracted)


def _movie_audio_cache_ready(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("status") != "READY_FOR_PREFLIGHT":
        return False
    matching = value.get("matching", {})
    sync = value.get("sync", {})
    pairs = sync.get("pairs") or []
    return (
        matching.get("status") == "READY_FOR_PCM"
        and bool(matching.get("mappings"))
        and sync.get("status") == "OK"
        and bool(pairs)
        and all(item.get("status") == "OK" and int(item.get("valid_points", 0)) >= 3 for item in pairs)
    )


def _work_font_signatures(work: Path) -> list[dict[str, Any]]:
    values = []
    for path in work.rglob("*"):
        if (
            path.is_file()
            and path.suffix.casefold() in FONT_SUFFIXES
            and not any(part in ALLOWED_HIDDEN_NAMES for part in path.parts)
            and not any(part.casefold().endswith("_subsetted") for part in path.parts)
        ):
            values.append(file_signature(path))
    return sorted(values, key=lambda item: _path_key(item["path"]))


def _movie_subtitle_zip(config: dict[str, Any], title: str) -> dict[str, Any] | None:
    root = str(config.get("paths", {}).get("movieSubtitleArchiveRoot") or "").strip()
    if not root:
        return None
    path = resolve_path(root) / f"{title}.zip"
    return file_signature(path) if path.is_file() else None


def _extract_subtitle_zip(work: Path, signature: dict[str, Any]) -> list[Path]:
    destination = temporary_path(work, "movie-subtitle-zip")
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(resolve_path(signature["path"]), "r", metadata_encoding=ZIP_METADATA_ENCODING) as archive:
        for name in archive.namelist():
            entry = Path(name.replace("\\", "/"))
            if ".." in entry.parts or entry.suffix.casefold() not in SUBTITLE_SUFFIXES:
                continue
            target = destination / entry.name
            target.write_bytes(archive.read(name))
            extracted.append(target.resolve())
    return sorted(dict.fromkeys(extracted))


def execute_inspection(
    args: Any,
    *,
    config_loader: Callable[[Path | None], dict[str, Any]],
    file_lister: Callable[[Path], tuple[list[Path], list[Path]]],
    media_inspector: Callable[[Path, str], dict[str, Any]],
    tool_resolver: Callable[[dict[str, Any], str], str],
    embedded_inspector: Callable[[Path, list[dict[str, Any]], str], dict[str, Any]],
    library_inspector: Callable[[dict[str, Any], str, str, str], dict[str, Any]],
    movie_audio_inspector: Callable[[dict[str, Any], str, str, str | None, str], dict[str, Any]],
    database_resolver: Callable[[dict[str, Any]], Path],
    metadata_inspector: Callable[[Path, dict[str, Any], dict[str, Any], list[Path], Any], dict[str, Any]] = inspect_metadata,
) -> dict[str, Any]:
    work = resolve_path(args.work_path)
    if not work.is_dir():
        raise WorkflowError("WORK_PATH_NOT_FOUND", f"Task directory not found: {work}")
    config = config_loader(Path(args.config) if args.config else None)
    manifest_path = resolve_path(args.manifest) if args.manifest else backend_cache_path(work)
    if manifest_path != backend_cache_path(work).resolve(strict=False):
        raise WorkflowError("BACKEND_CACHE_PATH", "execution cache path violates the static contract")
    route = route_branch(work, config)
    previous = _load_incremental_manifest(manifest_path, work, bool(getattr(args, "rerun", False)))
    cache_mode = "incremental" if previous is not None else "full-fallback" if getattr(args, "rerun", False) else "full"
    previous_discovery = previous.get("discovery", {}) if previous is not None else {}
    components: dict[str, Any] = {}
    reused_components: set[str] = set()
    checked_components: set[str] = set()

    videos, external_subtitles = file_lister(work)
    mediainfo = tool_resolver(config, "mediainfo")
    mediainfo_identity = _tool_identity(config, "mediainfo", mediainfo)
    video_items = [
        {
            "path": str(path),
            "key": _component_key({"file": file_signature(path), "mediainfo": mediainfo_identity}),
        }
        for path in videos
    ]
    old_video_keys = {
        _path_key(item.get("path", "")): item.get("key")
        for item in _previous_component(previous, "videos").get("items", [])
        if isinstance(item, dict) and item.get("path")
    }
    old_videos = {
        _path_key(item.get("file", {}).get("path", "")): item
        for item in previous_discovery.get("videos", [])
        if isinstance(item, dict) and item.get("file", {}).get("path")
    }
    video_inventory: list[dict[str, Any]] = []
    for path, item in zip(videos, video_items):
        old = old_videos.get(_path_key(path))
        if old_video_keys.get(_path_key(path)) == item["key"] and isinstance(old, dict) and old.get("status") == "OK":
            video_inventory.append(copy.deepcopy(old))
            reused_components.add("videos")
            continue
        inspected = media_inspector(path, mediainfo)
        if not isinstance(inspected.get("file"), dict):
            inspected["file"] = file_signature(path)
        video_inventory.append(inspected)
        checked_components.add("videos")
    video_key = _component_key(video_items)
    components["videos"] = {"key": video_key, "items": video_items}
    environment = {
        "mediainfo": {"status": "READY", "path": mediainfo},
        "python": {"status": "READY", "path": sys.executable},
    }

    selected_capabilities = {str(value) for value in (getattr(args, "selected_capability", None) or [])}
    metadata_selected = (
        "metadata" in selected_capabilities
        if selected_capabilities
        else args.task_mode != "local-only"
    )
    old_metadata = previous_discovery.get("metadata")
    if metadata_selected:
        raw_metadata = getattr(args, "metadata_json", None)
        if raw_metadata:
            try:
                metadata_decision = json.loads(raw_metadata)
            except json.JSONDecodeError as exc:
                raise WorkflowError("METADATA_OPTIONS_INVALID", f"Invalid metadata decision JSON: {exc}", "DECISION_REQUIRED") from exc
            if not isinstance(metadata_decision, dict):
                raise WorkflowError("METADATA_OPTIONS_INVALID", "Metadata decisions must be an object", "DECISION_REQUIRED")
        else:
            metadata_decision = {}
        if args.title and not metadata_decision.get("query"):
            metadata_decision = {**metadata_decision, "query": str(args.title)}
        metadata_config = config.get("metadata", {}) if isinstance(config.get("metadata"), dict) else {}
        metadata_enabled = bool(metadata_decision.get("enabled", metadata_config.get("enabled", False))) and str(
            metadata_decision.get("mode", metadata_config.get("mode", "auto"))
        ).casefold() != "off"
        metadata_files = [
            *[path for path in videos if path.suffix.casefold() in {".mkv", ".mp4"}],
            *external_subtitles,
        ]
        metadata_key = _component_key({
            "selected": True,
            "enabled": metadata_enabled,
            "route": route.get("branch"),
            "decision": metadata_decision,
            "config": metadata_config,
            "credentials": credential_presence(),
            "files": [str(path.relative_to(work)).replace("\\", "/") for path in metadata_files],
        })
        if (
            _component_matches(previous, "metadata", metadata_key)
            and isinstance(old_metadata, dict)
            and old_metadata.get("selected") is True
            and old_metadata.get("status") in {"MATCHED", "OFF"}
        ):
            metadata = copy.deepcopy(old_metadata)
            reused_components.add("metadata")
        else:
            inspected_metadata = metadata_inspector(work, config, route, metadata_files, metadata_decision)
            metadata = {**inspected_metadata, "selected": True}
            checked_components.add("metadata")
    else:
        metadata_key = _component_key({"selected": False, "route": route.get("branch")})
        if (
            _component_matches(previous, "metadata", metadata_key)
            and isinstance(old_metadata, dict)
            and old_metadata.get("selected") is False
            and old_metadata.get("status") == "OFF"
        ):
            metadata = copy.deepcopy(old_metadata)
            reused_components.add("metadata")
        else:
            metadata = {
                "status": "OFF",
                "mode": "off",
                "selected": False,
                "reason": "CAPABILITY_NOT_SELECTED",
                "suggestedDecisions": {},
                "issues": [],
                "warnings": [],
                "candidates": [],
                "episodes": [],
            }
            checked_components.add("metadata")
    components["metadata"] = {"key": metadata_key}
    selected_final_sinks = set(getattr(args, "selected_final_sink", None) or [])
    if selected_final_sinks and not metadata_selected and not str(args.title or "").strip():
        raise WorkflowError(
            "TITLE_REQUIRED_WITHOUT_METADATA",
            "Custom final delivery without metadata requires a confirmed title",
            "DECISION_REQUIRED",
        )

    workspace_embedded = {"status": "EXTERNAL", "assTracks": 0, "files": [], "extractedSubtitles": []}
    retain_embedded = bool(getattr(args, "retain_embedded_subtitles", False))
    inspect_workspace_embedded = not external_subtitles or args.task_mode == "archive-only" or retain_embedded
    embedded_key_payload: dict[str, Any] = {
        "enabled": inspect_workspace_embedded,
        "videoKey": video_key if inspect_workspace_embedded else None,
        "taskMode": args.task_mode,
        "retainEmbedded": retain_embedded,
    }
    if inspect_workspace_embedded:
        mkvmerge = tool_resolver(config, "mkvmerge")
        environment["mkvmerge"] = {"status": "READY", "path": mkvmerge}
        embedded_key_payload["mkvmerge"] = _tool_identity(config, "mkvmerge", mkvmerge)
        embedded_key = _component_key(embedded_key_payload)
        old_embedded = previous_discovery.get("workspaceEmbeddedSubtitles")
        if _component_matches(previous, "embeddedSubtitles", embedded_key) and _embedded_cache_ready(old_embedded):
            workspace_embedded = copy.deepcopy(old_embedded)
            reused_components.add("embeddedSubtitles")
        else:
            workspace_embedded = embedded_inspector(work, video_inventory, mkvmerge)
            checked_components.add("embeddedSubtitles")
    else:
        embedded_key = _component_key(embedded_key_payload)
    components["embeddedSubtitles"] = {"key": embedded_key}

    subtitles = list(external_subtitles)
    if not subtitles and workspace_embedded.get("status") == "INCOMPLETE":
        subtitles.extend(resolve_path(value) for value in workspace_embedded.get("extractedSubtitles", []))

    library_preflight = None
    selected_sinks = set(getattr(args, "selected_final_sink", None) or [])
    has_explicit_sinks = hasattr(args, "selected_final_sink")
    needs_library = bool(selected_sinks) if has_explicit_sinks else args.task_mode != "local-only"
    if route.get("status") == "OK" and needs_library:
        media_branch = "tv" if route.get("branch") == "anime" else "movie"
        default_key = "library" if media_branch == "tv" else "movieLibrary"
        fallback_library = "Anime3" if media_branch == "tv" else "Movie3"
        preferred = str(config.get("defaults", {}).get(default_key, fallback_library))
        lookup_title = args.title or metadata.get("suggestedDecisions", {}).get("title") or work.name
        kdocs_selected = bool(getattr(args, "kdocs_tracker", args.task_mode != "local-only"))
        library_config = copy.deepcopy(config)
        if not kdocs_selected:
            library_config.setdefault("tracker", {})["enabled"] = False
        library_key = _component_key({
            "branch": media_branch,
            "title": lookup_title,
            "preferred": preferred,
            "storageTargets": config.get("storageTargets", {}),
            "plexLibraries": config.get("plexLibraries", {}),
            "tracker": library_config.get("tracker", {}),
            "trackerTool": _tool_identity(config, "kdocs-cli") if kdocs_selected else None,
        })
        old_library = previous_discovery.get("libraryTarget")
        if (
            _component_matches(previous, "libraryTarget", library_key)
            and isinstance(old_library, dict)
            and old_library.get("resolution", {}).get("status") == "OK"
        ):
            library_preflight = copy.deepcopy(old_library)
            reused_components.add("libraryTarget")
        else:
            lookup = library_inspector(library_config, lookup_title, media_branch, preferred)
            tracker_state = lookup.get("trackerState", {})
            tracker_cache = {
                key: copy.deepcopy(tracker_state[key])
                for key in ("status", "executable", "snapshot", "fileId", "worksheetId", "worksheetName", "dataStartRow")
                if key in tracker_state
            }
            library_preflight = {
                "branch": media_branch,
                "trackerMatches": lookup["trackerMatches"],
                "nasMatches": lookup["nasMatches"],
                "resolution": lookup["resolution"],
                "trackerState": tracker_cache,
            }
            checked_components.add("libraryTarget")
        components["libraryTarget"] = {"key": library_key}
    else:
        components["libraryTarget"] = {"key": _component_key({"enabled": False, "route": route, "taskMode": args.task_mode})}

    movie_audio_preflights: list[dict[str, Any]] = []
    movie_audio_items: list[dict[str, Any]] = []
    if args.movie_audio_replacement:
        if route.get("branch") != "movie":
            raise WorkflowError("MOVIE_BRANCH_REQUIRED", "Movie audio replacement requires the Movie branch", "DECISION_REQUIRED")
        if not args.title:
            raise WorkflowError("MOVIE_PREFLIGHT_ARGUMENTS", "Movie audio replacement requires --title", "DECISION_REQUIRED")
        raw_pairs = getattr(args, "movie_audio_pairs_json", None)
        if raw_pairs:
            try:
                pairs = json.loads(raw_pairs)
            except json.JSONDecodeError as exc:
                raise WorkflowError("MOVIE_AUDIO_PAIRS_INVALID", f"Invalid Movie audio pair JSON: {exc}", "DECISION_REQUIRED") from exc
        else:
            pairs = [{
                "stack": "",
                "video_source": getattr(args, "video_source", None),
                "disc_source": getattr(args, "disc_source", None),
            }]
        if not isinstance(pairs, list) or not pairs:
            raise WorkflowError("MOVIE_AUDIO_PAIRS_INVALID", "Movie audio pairs must be a non-empty array", "DECISION_REQUIRED")

        movie_tools = {
            name: _tool_identity(config, name)
            for name in ("ffmpeg", "ffprobe", "mkvmerge", "mkvinfo")
        }
        old_movie_items = _previous_component(previous, "movieAudio").get("items", [])
        old_movie_preflights = previous_discovery.get("movieAudioPreflights", [])
        current_inventory = {
            _path_key(item.get("file", {}).get("path", "")): item
            for item in video_inventory
            if isinstance(item, dict) and item.get("file", {}).get("path")
        }
        for index, pair in enumerate(pairs):
            if not isinstance(pair, dict) or not pair.get("video_source") or not pair.get("disc_source"):
                raise WorkflowError("MOVIE_AUDIO_PAIRS_INVALID", f"Incomplete Movie audio pair: {pair!r}", "DECISION_REQUIRED")
            stack = str(pair.get("stack") or "")
            pair_key = _component_key({
                "version": MOVIE_PCM_ANALYSIS_VERSION,
                "stack": stack,
                "videoSource": _safe_file_signature(str(pair["video_source"])),
                "discSource": _safe_file_signature(str(pair["disc_source"])),
                "tools": movie_tools,
                "mediainfo": mediainfo_identity,
                "algorithm": {
                    "points": 5,
                    "window": 20.0,
                    "search": 120.0,
                    "sampleRate": 2000,
                    "minCorrelation": 0.65,
                    "minMargin": 0.03,
                    "toleranceMs": 50.0,
                },
            })
            movie_audio_items.append({"stack": stack, "key": pair_key})
            old_item = old_movie_items[index] if index < len(old_movie_items) and isinstance(old_movie_items[index], dict) else {}
            old_preflight = old_movie_preflights[index] if index < len(old_movie_preflights) else None
            if old_item.get("stack") == stack and old_item.get("key") == pair_key and _movie_audio_cache_ready(old_preflight):
                current = copy.deepcopy(old_preflight)
                current["title"] = args.title
                current["subtitleZip"] = _movie_subtitle_zip(config, args.title)
                reused_components.add(f"movieAudio:{stack or 'single'}")
            else:
                current = movie_audio_inspector(
                    config,
                    args.title,
                    str(pair["disc_source"]),
                    str(pair["video_source"]),
                    stack,
                )
                if current.get("videoSource", {}).get("path"):
                    source_path = resolve_path(current["videoSource"]["path"])
                    cached_inventory = current_inventory.get(_path_key(source_path))
                    current["oldMediaInventory"] = (
                        copy.deepcopy(cached_inventory)
                        if cached_inventory is not None
                        else media_inspector(source_path, mediainfo)
                    )
                checked_components.add(f"movieAudio:{stack or 'single'}")
            movie_audio_preflights.append(current)
    components["movieAudio"] = {"key": _component_key(movie_audio_items), "items": movie_audio_items}

    subtitle_zip_entry = movie_audio_preflights[0].get("subtitleZip") if movie_audio_preflights else None
    use_movie_zip = not subtitles and isinstance(subtitle_zip_entry, dict) and bool(subtitle_zip_entry.get("path"))
    movie_zip_key = _component_key({"enabled": use_movie_zip, "file": subtitle_zip_entry if use_movie_zip else None})
    movie_zip_files: list[Path] = []
    old_zip_component = _previous_component(previous, "movieSubtitleZip")
    if use_movie_zip:
        old_outputs = old_zip_component.get("outputs")
        if old_zip_component.get("key") == movie_zip_key and _signatures_still_match(old_outputs):
            movie_zip_files = [resolve_path(item["path"]) for item in old_outputs]
            reused_components.add("movieSubtitleZip")
        else:
            movie_zip_files = _extract_subtitle_zip(work, subtitle_zip_entry)
            checked_components.add("movieSubtitleZip")
        subtitles.extend(movie_zip_files)
    components["movieSubtitleZip"] = {
        "key": movie_zip_key,
        "outputs": [file_signature(path) for path in movie_zip_files],
    }

    movie_embedded = {"status": "NOT_NEEDED", "assTracks": 0, "files": [], "extractedSubtitles": []}
    need_movie_embedded = (
        not subtitles
        and workspace_embedded.get("status") == "MISSING"
        and bool(movie_audio_preflights)
        and bool(movie_audio_preflights[0].get("videoSource", {}).get("path"))
    )
    movie_embedded_key_payload: dict[str, Any] = {"enabled": need_movie_embedded}
    if need_movie_embedded:
        old_path = resolve_path(movie_audio_preflights[0]["videoSource"]["path"])
        source_signature = _safe_file_signature(old_path)
        mkvmerge = environment.get("mkvmerge", {}).get("path") or tool_resolver(config, "mkvmerge")
        environment["mkvmerge"] = {"status": "READY", "path": mkvmerge}
        movie_embedded_key_payload.update({
            "source": source_signature,
            "mkvmerge": _tool_identity(config, "mkvmerge", mkvmerge),
        })
        movie_embedded_key = _component_key(movie_embedded_key_payload)
        old_movie_embedded = previous_discovery.get("movieEmbeddedSubtitles")
        if _component_matches(previous, "movieEmbeddedSubtitles", movie_embedded_key) and _embedded_cache_ready(old_movie_embedded):
            movie_embedded = copy.deepcopy(old_movie_embedded)
            reused_components.add("movieEmbeddedSubtitles")
        else:
            old_inventory = movie_audio_preflights[0].get("oldMediaInventory") or media_inspector(old_path, mediainfo)
            mkvmerge = tool_resolver(config, "mkvmerge")
            movie_embedded = embedded_inspector(work, [old_inventory], mkvmerge)
            checked_components.add("movieEmbeddedSubtitles")
        if movie_embedded.get("status") == "INCOMPLETE":
            subtitles.extend(resolve_path(value) for value in movie_embedded.get("extractedSubtitles", []))
    else:
        movie_embedded_key = _component_key(movie_embedded_key_payload)
    components["movieEmbeddedSubtitles"] = {"key": movie_embedded_key}

    embedded_subtitles = movie_embedded if need_movie_embedded else workspace_embedded
    if movie_zip_files:
        embedded_subtitles = {"status": "EXTERNAL", "source": "movie-subtitle-zip", "assTracks": 0, "files": [], "extractedSubtitles": []}

    subtitles = sorted({_path_key(path): resolve_path(path) for path in subtitles}.values())

    required_tools = inspection_required_tools(args, config, has_external_subtitles=bool(subtitles))
    for selected_tool in required_tools:
        if selected_tool in environment:
            continue
        try:
            environment[selected_tool] = {"status": "READY", "path": tool_resolver(config, selected_tool)}
        except WorkflowError as exc:
            environment[selected_tool] = {"status": "FAILED", "code": exc.code, "error": str(exc)}

    old_subtitle_items = {
        _path_key(item.get("path", "")): item.get("key")
        for item in _previous_component(previous, "subtitles").get("items", [])
        if isinstance(item, dict) and item.get("path")
    }
    old_subtitles = {
        _path_key(item.get("file", {}).get("path", "")): item
        for item in previous_discovery.get("subtitles", [])
        if isinstance(item, dict) and item.get("file", {}).get("path")
    }
    subtitle_items: list[dict[str, Any]] = []
    subtitle_inventory: list[dict[str, Any]] = []
    for subtitle in subtitles:
        signature = file_signature(subtitle)
        item_key = _component_key(signature)
        subtitle_items.append({"path": str(subtitle), "key": item_key})
        old = old_subtitles.get(_path_key(subtitle))
        if old_subtitle_items.get(_path_key(subtitle)) == item_key and isinstance(old, dict):
            subtitle_inventory.append(copy.deepcopy(old))
            reused_components.add("subtitles")
        else:
            requirements_for_file = parse_ass_font_requirements(subtitle)
            subtitle_inventory.append(
                {"file": signature, "group": str(subtitle.parent.relative_to(work)), "fonts": requirements_for_file}
            )
            checked_components.add("subtitles")
    components["subtitles"] = {"key": _component_key(subtitle_items), "items": subtitle_items}

    requirements_by_name: dict[str, dict[str, Any]] = {}
    for subtitle_item in subtitle_inventory:
        for item in subtitle_item.get("fonts", []):
            current = requirements_by_name.setdefault(
                item["normalized"], {"normalized": item["normalized"], "name": item["name"], "sources": set(), "contexts": set()}
            )
            current["sources"].update(item.get("sources", []))
            current["contexts"].update(item.get("contexts", []))
    requirements = [
        {**item, "sources": sorted(item["sources"]), "contexts": sorted(item["contexts"])}
        for item in sorted(requirements_by_name.values(), key=lambda value: value["normalized"])
    ]

    db_file = database_resolver(config) / "fonts.json"
    primary_value = str(config["paths"].get("primaryFonts") or "").strip()
    primary = resolve_path(primary_value) if primary_value else work / "__missing_primary_fonts__"
    fallback_value = str(config["paths"].get("fallbackFonts") or "").strip()
    fallback = resolve_path(fallback_value) if fallback_value else None
    fallback_database = fallback_font_database_path(config, fallback) if fallback is not None else None
    font_key = _component_key({
        "subtitleKey": components["subtitles"]["key"],
        "requirements": requirements,
        "workFonts": _work_font_signatures(work),
        "assfontsDatabase": _safe_file_signature(db_file),
        "fallbackDatabase": _safe_file_signature(fallback_database) if fallback_database is not None else None,
        "primary": str(primary),
        "fallback": str(fallback) if fallback is not None else None,
    })
    old_font_component = _previous_component(previous, "fonts")
    font_fields = (
        "fontRequirements", "fontDatabase", "fallbackFontDatabase", "fontAvailability", "fontIssues", "missingFonts"
    )
    if old_font_component.get("key") == font_key and all(key in previous_discovery for key in font_fields):
        font_values = {key: copy.deepcopy(previous_discovery[key]) for key in font_fields}
        requirements = font_values["fontRequirements"]
        font_availability = font_values["fontAvailability"]
        font_issues = font_values["fontIssues"]
        db_records_count = int(font_values["fontDatabase"].get("entries", 0))
        fallback_state = font_values["fallbackFontDatabase"]
        reused_components.add("fonts")
    else:
        db_records = load_assfonts_database(db_file) if requirements else []
        fallback_state: dict[str, Any] = {
            "status": "NOT_NEEDED",
            "path": str(fallback_database) if fallback_database is not None else None,
        }

        def fallback_loader() -> list[dict[str, Any]]:
            if fallback is None or fallback_database is None:
                fallback_state.update({"status": "UNCONFIGURED"})
                return []
            try:
                loaded = load_fallback_font_database(fallback_database, fallback)
            except WorkflowError as exc:
                fallback_state.update({"status": "UNAVAILABLE", "code": exc.code, "error": str(exc)})
                return []
            fallback_state.update(
                {
                    "status": "READY",
                    "path": loaded["path"],
                    "files": loaded["fileCount"],
                    "names": loaded["nameCount"],
                    "stale": loaded["stale"],
                }
            )
            return loaded["records"]

        font_availability, font_issues = resolve_font_availability(
            requirements,
            db_records,
            work,
            primary,
            fallback_loader if fallback is not None else None,
        )
        if font_issues and fallback_state.get("status") == "UNAVAILABLE":
            font_issues = [
                {
                    "code": fallback_state["code"],
                    "font": item["font"],
                    "detail": fallback_state["error"],
                }
                for item in font_issues
            ]
        elif font_issues and fallback_state.get("status") == "READY":
            font_issues = [
                {
                    "code": "FALLBACK_FONT_INDEX_MISS",
                    "font": item["font"],
                    "indexStale": bool(fallback_state.get("stale")),
                }
                for item in font_issues
            ]
        db_records_count = len(db_records)
        checked_components.add("fonts")
    components["fonts"] = {"key": font_key}

    manifest = {
        "schemaVersion": BACKEND_CACHE_SCHEMA,
        "workflowRevision": WORKFLOW_REVISION,
        "createdAt": previous.get("createdAt", utc_now()) if previous is not None else utc_now(),
        "updatedAt": utc_now(),
        "workPath": str(work),
        "configPath": config["_path"],
        "route": route,
        "taskMode": args.task_mode,
        "replacementKind": (
            library_preflight.get("resolution", {}).get("mode")
            if library_preflight and library_preflight.get("resolution", {}).get("mode") != "create"
            else None
        ),
        "discovery": {
            "environment": environment,
            "videos": video_inventory,
            "subtitles": subtitle_inventory,
            "fontRequirements": requirements,
            "fontDatabase": {"path": str(db_file), "entries": db_records_count},
            "fallbackFontDatabase": fallback_state,
            "fontAvailability": font_availability,
            "fontIssues": font_issues,
            "missingFonts": [item for item in font_availability if not item.get("available")],
            "movieAudioPreflights": movie_audio_preflights,
            "libraryTarget": library_preflight,
            "embeddedSubtitles": embedded_subtitles,
            "workspaceEmbeddedSubtitles": workspace_embedded,
            "movieEmbeddedSubtitles": movie_embedded,
            "metadata": metadata,
        },
        "preflightCache": {
            "version": PREFLIGHT_CACHE_VERSION,
            "mode": cache_mode,
            "components": components,
            "reused": sorted(reused_components),
            "checked": sorted(checked_components),
        },
        "plan": {},
        "planRevision": canonical_metadata_digest({}),
        "approvals": {},
        "events": [],
        "stages": empty_stage_map(),
    }
    failed_videos = [item for item in video_inventory if item["status"] != "OK"]
    failed_environment = [item for item in environment.values() if item.get("status") == "FAILED"]
    status = "FAILED" if failed_videos or failed_environment else "NEEDS_USER" if route["status"] != "OK" else "READY_FOR_PREFLIGHT"
    if movie_audio_preflights and any(item.get("status") != "READY_FOR_PREFLIGHT" for item in movie_audio_preflights):
        status = "NEEDS_USER"
    if library_preflight and library_preflight.get("resolution", {}).get("status") != "OK":
        status = "NEEDS_USER"
    if font_issues:
        status = "NEEDS_USER"
    if metadata.get("status") == "NEEDS_USER":
        status = "NEEDS_USER"
    set_stage(
        manifest,
        "inspect",
        status,
        videoCount=len(videos),
        subtitleCount=len(subtitles),
        missingFontCount=len(font_issues),
        metadataStatus=metadata.get("status"),
        cacheMode=cache_mode,
        reusedComponents=sorted(reused_components),
        checkedComponents=sorted(checked_components),
    )
    if status == "FAILED":
        manifest_path.unlink(missing_ok=True)
    else:
        save_manifest(manifest_path, manifest)
    return {"status": status, "manifest": str(manifest_path), "summary": manifest["stages"]["inspect"], "route": route}
