#!/usr/bin/env python3
"""Internal execution backend for Plex Anime/TV and Movie archive tasks."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from archive_rules import (
    ALLOWED_HIDDEN_NAMES,
    BACKEND_CACHE_SCHEMA,
    LOCAL_ONLY_REQUESTABLE_STEPS,
    WORKFLOW_REVISION,
    backend_cache_path,
    is_under,
    resolve_path as expand_path,
    route_branch,
    temporary_path,
)
from common import (
    configure_utf8_stdio,
    decode_output,
    load_config as load_common_config,
    load_state as load_task_state,
    normalize_status,
    read_json as json_read,
    write_json_atomic as json_write_atomic,
)
from internal import library_target, metadata_match, movie_audio, preflight, tracker
from internal.errors import WorkflowError
from internal.final_delivery import (
    _direct_final_copy,
    _final_attempt_matches,
    _final_checkpoint_matches,
    _final_transfer_temporary,
    _load_task_state,
    _merge_directory,
    _save_final_attempt,
    _save_final_checkpoint,
    _save_tracker_chunk_progress,
    _source_snapshot_matches,
    _tracker_chunk_progress,
    copy_and_verify,
    execute_final_delivery,
    normalize_tv_directory,
)
from internal.manifest import (
    DOWNSTREAM,
    STAGES,
    add_event,
    changed_plan_stage,
    empty_stage_map,
    invalidate_from,
    jobs_equal_except,
    load_manifest,
    normalized_plan,
    require_execution,
    require_no_source_drift,
    require_result_signatures,
    reuse_path_only_outputs,
    save_manifest,
    set_stage,
    source_drift,
    utc_now,
)
from internal.preflight import (
    VIDEO_SUFFIXES,
    _mkvextract_path,
    inspect_media,
    inspection_required_tools,
    list_task_files,
    safe_font_attachment_target,
)
from internal.remux_pipeline import (
    exact_track_id_map as remux_exact_track_id_map,
    execute_remux,
    external_ass_attachment_names,
    inspect_video as remux_inspect_video,
    validate_mkv_output as remux_validate_mkv_output,
)
from internal.signatures import (
    canonical_metadata_digest,
    file_signature,
    final_batch_payload,
    seal_final_batch,
    signature_matches,
)
from internal.subtitle_archive import (
    ZIP_METADATA_ENCODING,
    build_package,
    merge_zip_archives,
    safe_zip_entry_name,
    verify_zip,
    zip_entry_key,
    zip_entry_sort_key,
    zip_inventory_signature,
)
from internal.subtitle_pipeline import (
    FONT_SUFFIXES,
    SUBTITLE_SUFFIXES,
    build_font_lookup,
    build_recovery_font_index,
    cleanup_assfonts_intermediates,
    convert_recovery_font,
    expected_assfonts_output,
    failed_font_requirements,
    font_aliases,
    font_candidate_sort_key,
    font_content_identity,
    font_file_records,
    load_assfonts_database,
    locate_font_sources,
    normalize_font_name,
    parse_ass_font_requirements,
    prepare_fonts,
    readable_font_records,
    read_ass_text,
    recovery_font_sources,
    rename_subtitles,
    resolve_font_availability,
    search_missing_fonts,
    stage_recovery_font,
    stable_font_destination,
    subset_subtitles,
    validate_subset_output,
    write_ass_text,
)


def require_backend_config(path: Path | None = None) -> dict[str, Any]:
    try:
        config = load_common_config(path)
    except FileNotFoundError as exc:
        raise WorkflowError("CONFIG_NOT_FOUND", f"Configuration not found: {exc.filename}") from exc
    for key in ("paths", "tools", "storageTargets", "plexLibraries"):
        if not isinstance(config.get(key), dict):
            raise WorkflowError("CONFIG_INVALID", f"Configuration property must be an object: {key}")
    return config


def run_command(arguments: list[str], *, stdin_json: Any | None = None, retries: int = 0) -> dict[str, Any]:
    payload = None
    if stdin_json is not None:
        payload = json.dumps(stdin_json, ensure_ascii=False).encode("utf-8")
    attempts = []
    for attempt in range(retries + 1):
        started = time.monotonic()
        completed = subprocess.run(arguments, input=payload, capture_output=True, check=False)
        result = {
            "arguments": arguments,
            "exitCode": completed.returncode,
            "stdout": decode_output(completed.stdout),
            "stderr": decode_output(completed.stderr),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }
        attempts.append(result)
        if completed.returncode == 0:
            break
    result["attempts"] = attempts
    return result


def tool_path(config: dict[str, Any], tool_id: str) -> str:
    value = config["tools"].get(tool_id)
    if not value:
        raise WorkflowError("TOOL_NOT_CONFIGURED", f"Tool is not configured: {tool_id}")
    path = expand_path(value)
    if not path.is_file():
        raise WorkflowError("TOOL_NOT_FOUND", f"Configured tool is missing: {path}")
    return str(path)


def database_dir(config: dict[str, Any]) -> Path:
    configured = config["paths"].get("assfontsDatabase")
    if configured:
        return expand_path(configured)
    return expand_path(config["tools"].get("assfonts", "assfonts.exe")).parent / "database"


def inspect_video(path: Path, mkvmerge: str) -> dict[str, Any]:
    return remux_inspect_video(path, mkvmerge, runner=run_command)


def inspect_embedded_subtitles(
    work: Path,
    video_inventory: list[dict[str, Any]],
    mkvmerge: str,
) -> dict[str, Any]:
    return preflight.inspect_embedded_subtitles(
        work,
        video_inventory,
        mkvmerge,
        video_inspector=inspect_video,
        runner=run_command,
        extractor_resolver=_mkvextract_path,
        font_inspector=font_file_records,
        requirement_parser=parse_ass_font_requirements,
    )


def validate_mkv_output(path: Path, mkvmerge: str, job: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    return remux_validate_mkv_output(path, mkvmerge, job, inspector=inspect_video)


def exact_track_id_map(path: Path, media_inventory: dict[str, Any], mkvmerge: str, selected_keys: list[str] | None = None) -> dict[str, int]:
    return remux_exact_track_id_map(path, media_inventory, mkvmerge, selected_keys)


def configured_library_root(config: dict[str, Any], library_id: str) -> Path | None:
    library = config.get("plexLibraries", {}).get(library_id)
    if not isinstance(library, dict):
        return None
    target = config.get("storageTargets", {}).get(library.get("storageTarget"), {})
    for key in ("localPath", "uncPath"):
        value = target.get(key) if isinstance(target, dict) else None
        if value:
            root = expand_path(value)
            if root.is_dir():
                return root / str(library.get("relativePath", ""))
    return None


def require_final_target_outside_work(destination: Path, work: Path) -> None:
    if is_under(destination, work):
        raise WorkflowError(
            "FINAL_TARGET_INSIDE_TASK",
            f"Final target must be outside the task directory: {destination}",
        )


def read_tracker_state(config: dict[str, Any]) -> dict[str, Any]:
    tracker_config = config.get("tracker", {})
    if not tracker_config.get("enabled"):
        return {"status": "SKIPPED", "reason": "TRACKER_DISABLED", "entries": []}
    executable = tool_path(config, "kdocs-cli")
    file_id = tracker.resolve_file_id(executable, str(tracker_config.get("name", "Plex维护.xlsx")))
    worksheet_id, worksheet_name = tracker.resolve_worksheet(executable, file_id, tracker_config.get("worksheet"))
    snapshot = tracker.read_complete_snapshot(executable, file_id, worksheet_id, 0, 20)
    snapshot["worksheetName"] = worksheet_name
    columns = tracker.header_columns(snapshot)
    data_start_row = tracker.header_row(snapshot) + 1
    entries = []
    for column in ("Anime1", "Anime2", "Anime3", "Movie1", "Movie2", "Movie3"):
        if column not in columns:
            continue
        for item in tracker.snapshot_column(snapshot, columns[column], data_start_row):
            if item.get("value"):
                entries.append({"column": column, "row": item["row"], "title": item["value"], "xf": item.get("xf", {})})
    return {
        "status": "OK", "executable": executable, "snapshot": snapshot,
        "fileId": file_id, "worksheetId": worksheet_id, "worksheetName": worksheet_name,
        "dataStartRow": data_start_row, "entries": entries,
    }


def inspect_library_existing(config: dict[str, Any], title: str, branch: str, preferred_library: str) -> dict[str, Any]:
    tracker_state = read_tracker_state(config)
    entries = library_target.tracker_candidates(tracker_state.get("entries", []), title, branch)
    roots = {library: configured_library_root(config, library) for library in library_target.LIBRARIES[branch]}
    nas = library_target.nas_candidates({key: value for key, value in roots.items() if value is not None}, title, branch)
    resolution = library_target.resolve_target(branch, entries, nas, preferred_library)
    return {"trackerState": tracker_state, "trackerMatches": entries, "nasMatches": nas, "resolution": resolution}


def inspect_movie_audio_replacement(
    config: dict[str, Any], title: str, disc_source_value: str, video_source_value: str | None, stack: str = ""
) -> dict[str, Any]:
    disc_source = expand_path(disc_source_value)
    if not disc_source.is_file() or disc_source.suffix.casefold() not in {".m2ts", ".mkv"}:
        raise WorkflowError("VERIFIED_DISC_SOURCE_NOT_FOUND", f"User-verified Movie disc source not found: {disc_source}", "DECISION_REQUIRED")
    if not video_source_value:
        return {"status": "NEEDS_USER", "code": "MOVIE_VIDEO_SOURCE_REQUIRED", "stack": stack or "single"}
    video_source = expand_path(video_source_value)
    if not video_source.is_file() or video_source.suffix.casefold() != ".mkv":
        raise WorkflowError("MOVIE_VIDEO_SOURCE_NOT_FOUND", f"Movie compressed video source not found: {video_source}", "DECISION_REQUIRED")
    location = {"status": "OK", "matches": [{"library": None, "directory": str(video_source.parent), "mkv": str(video_source)}]}

    ffprobe = tool_path(config, "ffprobe")
    ffmpeg = tool_path(config, "ffmpeg")
    mkvmerge = tool_path(config, "mkvmerge")
    inventory = {
        "video_source": movie_audio.normalize_inventory(video_source, "video_source", ffprobe, mkvmerge),
        "disc_source": movie_audio.normalize_inventory(disc_source, "disc_source", ffprobe, mkvmerge),
    }
    matching = movie_audio.match_inventory(inventory)
    sync_results = []
    public_offset = None
    sync_status = "BLOCKED"
    if matching.get("status") == "READY_FOR_PCM":
        for mapping in matching.get("mappings", []):
            old_track = mapping["reference_track"]
            source_track = mapping["selected_source"]
            result = movie_audio.analyze_pair(
                video_source,
                int(old_track["ffprobe_index"]),
                disc_source,
                int(source_track["ffprobe_index"]),
                ffmpeg,
                ffprobe,
                points=5,
                window=20.0,
                search=120.0,
                sample_rate=2000,
                min_correlation=0.65,
                min_margin=0.03,
                tolerance_ms=50.0,
            )
            sync_results.append(result)
        offsets = [item.get("median_offset_ms") for item in sync_results if item.get("status") == "OK"]
        if len(offsets) == len(sync_results) and offsets and max(offsets) - min(offsets) <= 50.0:
            public_offset = round(sum(offsets) / len(offsets), 3)
            sync_status = "OK"
    subtitle_root = config.get("paths", {}).get("movieSubtitleArchiveRoot")
    subtitle_zip = expand_path(subtitle_root) / f"{title}.zip" if subtitle_root else None
    status = "READY_FOR_PREFLIGHT" if matching.get("status") == "READY_FOR_PCM" and sync_status == "OK" else "NEEDS_USER"
    return {
        "status": status,
        "title": title,
        "stack": stack,
        "verifiedDiscSource": file_signature(disc_source),
        "location": location,
        "videoSource": file_signature(video_source),
        "subtitleZip": file_signature(subtitle_zip) if subtitle_zip and subtitle_zip.is_file() else None,
        "inventory": inventory,
        "matching": matching,
        "sync": {"status": sync_status, "publicOffsetMs": public_offset, "pairs": sync_results},
    }


def inspect_archive_metadata(
    work: Path, config: dict[str, Any], route: dict[str, Any], files: list[Path], supplied: Any
) -> dict[str, Any]:
    return metadata_match.inspect_metadata(work, config, route, files, supplied)


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return preflight.execute_inspection(
        args,
        config_loader=require_backend_config,
        file_lister=list_task_files,
        media_inspector=inspect_media,
        tool_resolver=tool_path,
        embedded_inspector=inspect_embedded_subtitles,
        library_inspector=inspect_library_existing,
        movie_audio_inspector=inspect_movie_audio_replacement,
        database_resolver=database_dir,
        metadata_inspector=inspect_archive_metadata,
    )


def command_configure(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    require_no_source_drift(path, manifest, "configure")
    if not getattr(args, "plan_stdin", False):
        raise WorkflowError("PLAN_STDIN_REQUIRED", "configure requires --plan-stdin")
    try:
        plan = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("PLAN_INVALID", f"Invalid UTF-8 plan JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise WorkflowError("PLAN_INVALID", "Plan root must be an object")
    plan = normalized_plan(plan)
    old_plan = manifest.get("plan", {})
    reuse_path_only_outputs(old_plan, plan)
    invalidate_stage = changed_plan_stage(old_plan, plan)
    manifest["plan"] = plan
    manifest["planRevision"] = canonical_metadata_digest(plan)
    if invalidate_stage:
        invalidate_from(manifest, invalidate_stage, f"Confirmed plan changed at {invalidate_stage}")
    set_stage(manifest, "configure", "COMPLETE", source="utf-8-stdin", planRevision=manifest["planRevision"])
    save_manifest(path, manifest)
    return {"status": "COMPLETE", "manifest": str(path), "planKeys": sorted(plan), "planRevision": manifest["planRevision"], "invalidatedFrom": invalidate_stage}


def command_movie_audio(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    plans = manifest.get("plan", {}).get("movieAudioPlans")
    if not isinstance(plans, list) or not plans:
        raise WorkflowError("MOVIE_AUDIO_PLAN_REQUIRED", "Confirmed Movie audio plans are missing")
    preflights = list(manifest.get("discovery", {}).get("movieAudioPreflights") or [])
    preflight_by_stack = {str(item.get("stack") or ""): item for item in preflights}
    source_signatures = [
        signature
        for preflight in preflights
        for signature in (preflight.get("videoSource"), preflight.get("verifiedDiscSource"))
    ]
    changed = [
        str(signature.get("path") or "")
        for signature in source_signatures
        if not isinstance(signature, dict) or not signature_matches(signature)
    ]
    if changed:
        set_stage(manifest, "movie-audio", "FAILED", changedSources=changed)
        save_manifest(path, manifest)
        raise WorkflowError("MOVIE_AUDIO_SOURCE_CHANGED", f"Movie audio inputs changed after preflight: {changed}")

    config = require_backend_config(Path(manifest["configPath"]))
    mkvmerge = tool_path(config, "mkvmerge")
    ffprobe = tool_path(config, "ffprobe")
    mkvinfo = tool_path(config, "mkvinfo")
    work = expand_path(manifest["workPath"])
    results = []
    for plan in plans:
        stack = str(plan.get("stack") or "")
        preflight = preflight_by_stack.get(stack, {})
        output = expand_path(plan["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_plan = copy.deepcopy(plan)
        local_sources: dict[str, str] = {}
        for key, prefix in (("video_source", "video"), ("disc_source", "disc")):
            source = expand_path(plan[key])
            local = source
            if movie_audio.is_remote_path(source):
                local = temporary_path(work, "movie-audio", f"{prefix}-{stack or 'single'}-{source.name}")
                if not local.is_file() or local.stat().st_size != source.stat().st_size:
                    shutil.copy2(source, local)
                if local.stat().st_size != source.stat().st_size:
                    raise WorkflowError("MOVIE_AUDIO_LOCAL_COPY_FAILED", f"Local Movie source copy size mismatch: {local}")
                runtime_plan[key] = str(local)
            local_sources[key] = str(local)
        video_inventory = preflight.get("inventory", {}).get("video_source")
        reused = False
        verification = None
        if output.is_file():
            verification = movie_audio.verify_plan(runtime_plan, ffprobe, mkvmerge, mkvinfo, old_inventory=video_inventory)
            if verification.get("status") in {"OK", "WARN"}:
                reused = True
            else:
                output.unlink()
        if not reused:
            mux = movie_audio.execute_plan(runtime_plan, mkvmerge)
            if mux.get("status") not in {"OK", "WARNING"}:
                set_stage(manifest, "movie-audio", "FAILED", stack=stack, error=mux.get("error") or "mkvmerge failed")
                save_manifest(path, manifest)
                raise WorkflowError("MOVIE_AUDIO_MUX_FAILED", str(mux.get("error") or "mkvmerge failed"))
            if mux.get("status") == "WARNING":
                add_event(manifest, "WARNING", "MOVIE_AUDIO_MKVMERGE_WARNING", str(mux.get("warning") or "Movie original-disc audio mux completed with warnings"), "movie-audio")
            verification = movie_audio.verify_plan(runtime_plan, ffprobe, mkvmerge, mkvinfo, old_inventory=video_inventory)
            if verification.get("status") not in {"OK", "WARN"}:
                set_stage(manifest, "movie-audio", "FAILED", stack=stack, verification=verification)
                save_manifest(path, manifest)
                raise WorkflowError("MOVIE_AUDIO_VERIFY_FAILED", f"Movie original-disc audio output failed verification: {stack or 'single'}")
        results.append({"stack": stack, "file": file_signature(output), "sources": local_sources, "reused": reused, "verification": verification})

    manifest["movieAudioResult"] = {"files": results}
    set_stage(manifest, "movie-audio", "COMPLETE", outputs=[item["file"]["path"] for item in results], reused=sum(1 for item in results if item["reused"]))
    save_manifest(path, manifest)
    return {"status": "COMPLETE", "outputs": [item["file"]["path"] for item in results], "reused": sum(1 for item in results if item["reused"])}


def command_prepare_fonts(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    require_no_source_drift(path, manifest, "prepare-fonts")
    config = require_backend_config(Path(manifest["configPath"]))
    result = prepare_fonts(
        manifest,
        config,
        assfonts=tool_path(config, "assfonts"),
        database=database_dir(config),
        runner=run_command,
        inspector=font_file_records,
    )
    if result["status"] == "SKIPPED":
        set_stage(manifest, "prepare-fonts", "SKIPPED", reason=result["reason"])
        save_manifest(path, manifest)
        return result
    stage = result.pop("stage")
    set_stage(manifest, "prepare-fonts", "COMPLETE", **stage)
    save_manifest(path, manifest)
    return result


def command_subset(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    config = require_backend_config(Path(manifest["configPath"]))

    def mark_failure(details: dict[str, Any]) -> None:
        set_stage(manifest, "subset", "FAILED", **details)
        save_manifest(path, manifest)

    result = subset_subtitles(
        manifest,
        config,
        assfonts=tool_path(config, "assfonts"),
        database=database_dir(config),
        runner=run_command,
        tool_resolver=lambda tool_id: tool_path(config, tool_id),
        inspector=font_file_records,
        on_failure=mark_failure,
    )
    if result["status"] == "SKIPPED":
        set_stage(manifest, "subset", "SKIPPED", reason=result["reason"])
        save_manifest(path, manifest)
        return result
    for group in result.pop("warningGroups"):
        add_event(manifest, "WARNING", "ASSFONTS_OTF_WARNING", f"Non-blocking OTF warning in {group}", "subset")
    stage = result.pop("stage")
    set_stage(manifest, "subset", "COMPLETE", **stage)
    save_manifest(path, manifest)
    return result


def command_rename(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    result = rename_subtitles(manifest, direct_output=getattr(args, "direct_output", False))
    if result["status"] == "SKIPPED":
        set_stage(manifest, "rename", "SKIPPED", reason=result["reason"])
        save_manifest(path, manifest)
        return result
    stage = result.pop("stage")
    set_stage(manifest, "rename", "COMPLETE", **stage)
    save_manifest(path, manifest)
    return result


def command_remux(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    config = require_backend_config(Path(manifest["configPath"]))

    def record_warning(code: str, message: str) -> None:
        add_event(manifest, "WARNING", code, message, "remux")

    result = execute_remux(
        manifest,
        tool_path(config, "mkvmerge"),
        runner=run_command,
        track_mapper=exact_track_id_map,
        validator=validate_mkv_output,
        direct_output=getattr(args, "direct_output", False),
        defer_output_validation=getattr(args, "defer_output_validation", False),
        on_warning=record_warning,
    )
    if result["status"] == "SKIPPED":
        set_stage(manifest, "remux", "SKIPPED", reason=result["reason"])
        save_manifest(path, manifest)
        return result
    stage = result.pop("stage")
    set_stage(manifest, "remux", "COMPLETE", **stage)
    save_manifest(path, manifest)
    return result


def command_package(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest)
    package = manifest.get("plan", {}).get("package")
    if not package:
        set_stage(manifest, "package", "SKIPPED", reason="NO_PACKAGE_PLAN")
        save_manifest(path, manifest)
        return {"status": "SKIPPED", "reason": "NO_PACKAGE_PLAN"}
    result = build_package(
        expand_path(manifest["workPath"]),
        package,
        manifest.get("plan", {}).get("final", {}).get("zip", []),
        direct_output=getattr(args, "direct_output", False),
        defer_output_validation=getattr(args, "defer_output_validation", False),
        verifier=verify_zip,
    )
    manifest["packageResult"] = result
    set_stage(manifest, "package", "COMPLETE", entries=len(result["entries"]))
    save_manifest(path, manifest)
    return {"status": "COMPLETE", "zip": result}


def command_verify_local(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    config = require_backend_config(Path(manifest["configPath"]))
    mkvmerge = tool_path(config, "mkvmerge")
    outputs = []
    warnings: list[str] = []
    jobs = manifest.get("plan", {}).get("remuxJobs", [])
    if not jobs:
        jobs = [job for job in manifest.get("plan", {}).get("final", {}).get("video", []) if job.get("source")]
    for job in jobs:
        output = expand_path(job.get("output") or job["source"])
        inventory, item_warnings = validate_mkv_output(output, mkvmerge, job)
        warnings.extend(item_warnings)
        outputs.append(inventory)
    package = manifest.get("plan", {}).get("package")
    zip_result = None
    if package:
        expected = list((manifest.get("packageResult") or {}).get("entries") or [])
        if not expected:
            expected = [safe_zip_entry_name(str(item["arcname"])) for item in package.get("entries", [])]
        zip_result = verify_zip(expand_path(package["output"]), expected)
    elif manifest.get("plan", {}).get("final", {}).get("zip"):
        direct_jobs = manifest["plan"]["final"]["zip"]
        if len(direct_jobs) != 1:
            raise WorkflowError("DIRECT_ZIP_COUNT", "Direct archive supports exactly one subtitle ZIP")
        zip_result = verify_zip(expand_path(direct_jobs[0]["source"]))
    for warning in warnings:
        add_event(manifest, "WARNING", "TRACK_EXPECTATION_UNAVAILABLE", warning, "verify-local")
    result = {"status": "COMPLETE", "videos": outputs, "zip": zip_result, "warnings": warnings, "events": manifest.get("events", [])}
    manifest["localVerification"] = result
    set_stage(manifest, "verify-local", "COMPLETE", videoCount=len(outputs), zip=bool(zip_result))
    save_manifest(path, manifest)
    return result


def command_prepare_final(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    if manifest.get("stages", {}).get("verify-local", {}).get("status") != "COMPLETE":
        raise WorkflowError("LOCAL_VERIFICATION_REQUIRED", "verify-local must complete before prepare-final", "DECISION_REQUIRED")
    config = require_backend_config(Path(manifest["configPath"]))
    plan = manifest.get("plan", {})
    work = expand_path(manifest["workPath"])
    final = copy.deepcopy(plan.get("final", {}))
    if not final:
        raise WorkflowError("FINAL_PLAN_MISSING", "Confirmed plan has no provisional final actions")
    local_verification = manifest.get("localVerification", {})
    verified_videos = {
        os.path.normcase(str(expand_path(signature["path"]))): copy.deepcopy(signature)
        for item in local_verification.get("videos", [])
        if isinstance(item, dict)
        for signature in [item.get("file")]
        if isinstance(signature, dict) and signature.get("path")
    }
    verified_zip = local_verification.get("zip", {}).get("file") if isinstance(local_verification.get("zip"), dict) else None
    title = str(plan.get("title") or final.get("title") or "").strip()
    preferred = str(plan.get("preferredLibrary") or final.get("library") or config.get("defaults", {}).get("library", "Anime3"))
    tracker_state: dict[str, Any] | None = None
    target_resolution = plan.get("libraryTarget") or {}
    directory_candidate = None
    resolved_library = preferred
    if target_resolution:
        if target_resolution.get("status") != "OK":
            raise WorkflowError("FINAL_TARGET_UNRESOLVED", "Library target must be resolved during preflight")
        resolved_library = str(target_resolution.get("library") or preferred)
        final["mode"] = target_resolution.get("mode") or final.get("mode")
        final["library"] = resolved_library
        if manifest.get("route", {}).get("branch") == "anime" and final.get("mode") == "tv-webrip-to-bdrip":
            directory_candidate = target_resolution.get("nas")
            if not directory_candidate:
                raise WorkflowError("TV_REPLACEMENT_TARGET_CHANGED", "Confirmed WebRip directory is no longer available")
            final["tvDirectoryCandidate"] = directory_candidate
            final["directoryOperations"] = library_target.build_tv_directory_operations(directory_candidate)
    if config.get("tracker", {}).get("enabled") and final.get("tracker") is not False:
        tracker_state = copy.deepcopy(
            manifest.get("discovery", {}).get("libraryTarget", {}).get("trackerState") or {}
        )
        if tracker_state.get("status") != "OK" or not tracker_state.get("snapshot"):
            raise WorkflowError("TRACKER_PREFLIGHT_MISSING", "Confirmed tracker snapshot is missing; rerun preflight")
        if tracker.snapshot_needs_expansion(tracker_state["snapshot"]):
            tracker_state = read_tracker_state(config)
            if tracker_state.get("status") != "OK" or not tracker_state.get("snapshot"):
                raise WorkflowError("TRACKER_REFRESH_FAILED", "Could not expand the confirmed tracker snapshot")
        tracker_state["module"] = tracker

    root = configured_library_root(config, resolved_library)
    if root is None and final.get("video"):
        raise WorkflowError("FINAL_LIBRARY_OFFLINE", f"Resolved library is unavailable: {resolved_library}")
    for job in final.get("video", []):
        source = expand_path(job["source"])
        source_signature = verified_videos.get(os.path.normcase(str(source)))
        if not source_signature:
            raise WorkflowError("FINAL_SOURCE_VERIFICATION_MISSING", f"Reviewed source signature is missing: {source}")
        job["sourceSignature"] = source_signature
        if job.get("relativePath") and root is not None:
            job["destination"] = str((root / str(job["relativePath"])).resolve(strict=False))
        destination = expand_path(job["destination"])
        require_final_target_outside_work(destination, work)
        mode = str(job.get("operation") or ("replace" if final.get("mode") in {"replace", "tv-webrip-to-bdrip"} else "create"))
        if final.get("mode") == "tv-webrip-to-bdrip":
            mode = "upsert"
        exists = destination.exists()
        if mode == "create" and exists:
            raise WorkflowError("FINAL_CREATE_TARGET_EXISTS", f"Confirmed create target now exists: {destination}")
        if mode == "replace" and not exists and not directory_candidate:
            raise WorkflowError("FINAL_REPLACE_TARGET_MISSING", f"Confirmed replacement target is missing: {destination}")
        job["operation"] = mode
    for job in final.get("zip", []):
        source = expand_path(job["source"])
        if not isinstance(verified_zip, dict) or os.path.normcase(str(expand_path(verified_zip.get("path", "")))) != os.path.normcase(str(source)):
            raise WorkflowError("FINAL_ZIP_VERIFICATION_MISSING", f"Reviewed ZIP signature is missing: {source}")
        job["sourceSignature"] = copy.deepcopy(verified_zip)
        destination = expand_path(job["destination"])
        require_final_target_outside_work(destination, work)
        package_result = manifest.get("packageResult") or {}
        merge_base = str(package_result.get("mergeBase") or "").strip()
        merge_details = package_result.get("merge") or {}
        if merge_base and os.path.normcase(str(expand_path(merge_base))) == os.path.normcase(str(destination)):
            expected_destination = merge_details.get("baseSignature")
            if not isinstance(expected_destination, dict):
                raise WorkflowError("ZIP_MERGE_BASE_SIGNATURE_MISSING", "Merged ZIP base signature is missing")
            if zip_inventory_signature(destination) != expected_destination:
                raise WorkflowError("ZIP_MERGE_BASE_CHANGED", f"Subtitle archive changed after local merge: {destination}")
            job["expectedDestinationZipSignature"] = copy.deepcopy(expected_destination)
        job["operation"] = str(job.get("operation") or ("replace" if destination.exists() else "create"))

    expected_status = plan.get("expectedStatus") or final.get("status")
    if tracker_state and tracker_state.get("status") == "OK" and expected_status and final.get("tracker") is not False:
        tracker_module = tracker_state["module"]
        tracker_title = title
        tracker_options: dict[str, Any] = {}
        if target_resolution and target_resolution.get("mode") == "tv-webrip-to-bdrip":
            tracker_title = library_target.normalize_title(title, "tv")
            matched_tracker = target_resolution.get("tracker") or {}
            replace_title = str(matched_tracker.get("title") or "").strip()
            if not replace_title:
                raise WorkflowError("TRACKER_REPLACEMENT_SOURCE_MISSING", "Confirmed WebRip tracker entry is missing")
            tracker_options["replace_title"] = replace_title
        tracker_plan = tracker_module.plan_column_update(
            tracker_state["snapshot"],
            resolved_library,
            tracker_title,
            str(expected_status),
            tracker_state["dataStartRow"],
            **tracker_options,
        )
        final["trackerPlan"] = tracker_plan
        final["trackerExecutable"] = tracker_state["executable"]
    seal_final_batch(final)
    manifest["finalPreparation"] = {
        "preparedAt": utc_now(), "resolvedLibrary": resolved_library, "targetResolution": target_resolution,
        "final": final,
    }
    set_stage(manifest, "final-prepare", "COMPLETE", batchId=final["batchId"], library=resolved_library, mode=final.get("mode"))
    save_manifest(path, manifest)
    return {
        "status": "READY_FOR_COMBINED_REVIEW",
        "batchId": final["batchId"],
        "batchDigest": final["batchDigest"],
        "library": resolved_library,
        "mode": final.get("mode"),
        "final": final,
    }


def command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    path = expand_path(args.manifest)
    manifest = load_manifest(path)
    require_execution(args, manifest, "final")
    local_signatures = [item.get("file") for item in manifest.get("localVerification", {}).get("videos", []) if item.get("file")]
    local_zip_signature = manifest.get("localVerification", {}).get("zip", {}).get("file")
    if local_zip_signature:
        local_signatures.append(local_zip_signature)
    if local_signatures:
        require_result_signatures(path, manifest, "finalize", local_signatures, "verify-local")
    final = manifest.get("finalPreparation", {}).get("final")
    if not final:
        raise WorkflowError("FINAL_PREPARATION_REQUIRED", "prepare-final must complete before finalize", "DECISION_REQUIRED")
    current_digest = canonical_metadata_digest(final_batch_payload(final))
    expected_digest = str(final.get("batchDigest", ""))
    expected_batch = str(final.get("batchId", ""))
    if not expected_digest or expected_digest != current_digest or expected_batch != current_digest[:24]:
        raise WorkflowError(
            "FINAL_BATCH_CONTENT_CHANGED",
            "Final actions changed after review; rerun review before finalizing",
        )
    if (
        not args.approved_batch
        or args.approved_batch != expected_batch
        or not getattr(args, "approved_digest", None)
        or args.approved_digest != expected_digest
    ):
        raise WorkflowError(
            "FINAL_BATCH_MISMATCH",
            "Approved batch id and digest must exactly match the reviewed final batch",
        )
    if manifest.get("stages", {}).get("verify-local", {}).get("status") != "COMPLETE":
        raise WorkflowError("LOCAL_REVIEW_REQUIRED", "Local verification must complete before finalize", "DECISION_REQUIRED")
    work = expand_path(manifest["workPath"])

    def record_stage(stage: str, status: str, payload: Any) -> None:
        if status == "COMPLETE":
            set_stage(manifest, stage, status, result=payload)
        else:
            set_stage(manifest, stage, status, error=str(payload))
        save_manifest(path, manifest)

    delivery = execute_final_delivery(
        work,
        final,
        expected_batch,
        copier=copy_and_verify,
        tracker_apply=tracker.apply_static_plan,
        directory_normalizer=normalize_tv_directory,
        on_stage=record_stage,
    )
    if delivery["status"] == "FAILED":
        save_manifest(path, manifest)
        return {
            "status": "FAILED",
            "completed": delivery["completed"],
            "failed": delivery["failed"],
            "warnings": delivery["warnings"],
            "taskDirectoryKept": manifest["workPath"],
        }
    set_stage(manifest, "cleanup", "PENDING", reason="Deferred to cleanup step")
    save_manifest(path, manifest)
    return {
        "status": "COMPLETE",
        "completed": delivery["completed"],
        "warnings": delivery["warnings"],
        "taskDirectoryKept": manifest["workPath"],
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(expand_path(args.manifest))
    return {
        "status": "OK",
        "workPath": manifest["workPath"],
        "route": manifest.get("route"),
        "taskMode": manifest.get("taskMode"),
        "stages": manifest.get("stages"),
        "events": manifest.get("events", []),
    }


def add_mutation_flags(parser: argparse.ArgumentParser, *, final: bool = False) -> None:
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan")
    parser.add_argument("--direct-output", action="store_true")
    if final:
        parser.add_argument("--approved-batch")
        parser.add_argument("--approved-digest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--work-path", required=True)
    inspect.add_argument("--manifest")
    inspect.add_argument("--config")
    inspect.add_argument("--task-mode", default="complete-archive", choices=["complete-archive", "replacement", "archive-only", "local-only"])
    inspect.add_argument("--movie-audio-replacement", action="store_true")
    inspect.add_argument("--retain-embedded-subtitles", action="store_true")
    inspect.add_argument("--rerun", action="store_true", help="reuse unchanged preflight components from the compatible execution cache")
    inspect.add_argument("--title")
    inspect.add_argument("--metadata-json")
    inspect.add_argument("--disc-source", help="user-verified Movie disc-audio source (.m2ts or .mkv)")
    inspect.add_argument("--video-source", help="compressed Movie MKV whose video track is retained")
    inspect.add_argument("--movie-audio-pairs-json", help="UTF-8 JSON array of stack/video/disc source mappings")
    inspect.add_argument(
        "--requested-step",
        action="append",
        default=[],
        choices=list(LOCAL_ONLY_REQUESTABLE_STEPS),
        help="selected local-only capability; repeat for multiple steps",
    )
    inspect.set_defaults(handler=command_inspect)

    configure = subparsers.add_parser("configure")
    configure.add_argument("--manifest", required=True)
    configure.add_argument("--plan-stdin", action="store_true", required=True)
    add_mutation_flags(configure)
    configure.set_defaults(handler=command_configure)

    for name, handler in (
        ("movie-audio", command_movie_audio),
        ("prepare-fonts", command_prepare_fonts),
        ("subset", command_subset),
        ("rename", command_rename),
        ("remux", command_remux),
        ("package", command_package),
    ):
        child = subparsers.add_parser(name)
        child.add_argument("--manifest", required=True)
        add_mutation_flags(child)
        if name in {"remux", "package"}:
            child.add_argument("--defer-output-validation", action="store_true")
        child.set_defaults(handler=handler)

    verify = subparsers.add_parser("verify-local")
    verify.add_argument("--manifest", required=True)
    verify.set_defaults(handler=command_verify_local)

    prepare_final = subparsers.add_parser("prepare-final")
    prepare_final.add_argument("--manifest", required=True)
    prepare_final.set_defaults(handler=command_prepare_final)

    status = subparsers.add_parser("status")
    status.add_argument("--manifest", required=True)
    status.set_defaults(handler=command_status)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--manifest", required=True)
    add_mutation_flags(finalize, final=True)
    finalize.set_defaults(handler=command_finalize)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
        if isinstance(result, dict) and result.get("status"):
            result["status"] = normalize_status(str(result["status"]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if normalize_status(str(result.get("status") or "OK")) not in {"FAILED", "NEEDS_USER"} else 2
    except WorkflowError as exc:
        print(
            json.dumps(
                {"status": normalize_status(exc.category), "code": exc.code, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        category = getattr(exc, "category", None)
        code = getattr(exc, "code", None)
        if category and code:
            print(
                json.dumps({"status": normalize_status(str(category)), "code": str(code), "error": str(exc)}, ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"status": "FAILED", "code": "UNEXPECTED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
