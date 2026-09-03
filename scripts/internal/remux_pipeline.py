"""MKV inspection, exact track validation, and deterministic remux execution."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from archive_rules import (
    artifact_output_root,
    is_under,
    numbered_output_path,
    resolve_path,
    temporary_path,
)
from common import read_json, write_json_atomic
from internal import media_inspection
from internal.errors import WorkflowError
from internal.signatures import canonical_metadata_digest, file_signature, signature_matches
from internal.subtitle_pipeline import read_ass_text


def inspect_video(
    path: Path,
    mkvmerge: str,
    *,
    runner: Callable[[list[str]], dict[str, Any]],
) -> dict[str, Any]:
    result = runner([mkvmerge, "-J", str(path)])
    if result["exitCode"] != 0:
        return {"file": file_signature(path), "status": "FAILED", "error": result["stderr"] or result["stdout"]}
    try:
        inventory = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"file": file_signature(path), "status": "FAILED", "error": f"Invalid mkvmerge JSON: {exc}"}
    tracks = []
    for track in inventory.get("tracks", []):
        props = track.get("properties", {})
        tracks.append(
            {
                "id": track.get("id"),
                "type": track.get("type"),
                "codec": track.get("codec"),
                "codecId": props.get("codec_id"),
                "language": props.get("language"),
                "name": props.get("track_name"),
                "default": props.get("default_track"),
                "forced": props.get("forced_track"),
                "channels": props.get("audio_channels"),
            }
        )
    attachments = [
        {
            "id": item.get("id"),
            "name": str(item.get("file_name") or ""),
            "contentType": str(item.get("content_type") or ""),
        }
        for item in inventory.get("attachments", [])
    ]
    chapters = inventory.get("chapters", [])
    return {
        "file": file_signature(path),
        "status": "OK",
        "container": inventory.get("container", {}),
        "tracks": tracks,
        "chapters": {"present": bool(chapters), "count": len(chapters)},
        "attachments": attachments,
    }


def external_ass_attachment_names(job: dict[str, Any]) -> list[str]:
    """Infer MKVToolNix's automatic ASS font attachments for external subtitles."""
    names: set[str] = set()
    for value in job.get("arguments", []):
        subtitle = Path(str(value))
        if subtitle.suffix.casefold() != ".ass" or not subtitle.is_file():
            continue
        try:
            text = read_ass_text(subtitle)
        except Exception:
            continue
        names.update(
            match.group(1).strip()
            for match in re.finditer(r"^\s*fontname\s*:\s*(.*?)\s*$", text, re.IGNORECASE | re.MULTILINE)
            if match.group(1).strip()
        )
    return sorted(names)


def validate_mkv_output(
    path: Path,
    mkvmerge: str,
    job: dict[str, Any],
    *,
    inspector: Callable[[Path, str], dict[str, Any]],
    allow_preserved_chapter_repair: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    inventory = inspector(path, mkvmerge)
    if inventory["status"] != "OK":
        raise WorkflowError("LOCAL_MKV_INVALID", inventory.get("error", str(path)))
    expected_tracks = job.get("expectedTracks")
    if not expected_tracks:
        raise WorkflowError("TRACK_EXPECTATION_REQUIRED", f"Unified plan is missing expectedTracks: {path}")
    actual_tracks = inventory.get("tracks", [])
    mismatches: list[str] = []
    if len(actual_tracks) != len(expected_tracks):
        mismatches.append(f"track count expected={len(expected_tracks)} actual={len(actual_tracks)}")
    for index, expected in enumerate(expected_tracks):
        if index >= len(actual_tracks):
            break
        actual = actual_tracks[index]
        for key in ("type", "language", "name", "default", "forced", "channels"):
            if key not in expected:
                continue
            expected_value = expected.get(key)
            actual_value = actual.get(key)
            if key == "name":
                expected_value = str(expected_value or "")
                actual_value = str(actual_value or "")
            if actual_value != expected_value:
                mismatches.append(f"track[{index}].{key} expected={expected_value!r} actual={actual_value!r}")
    commentary = [
        track
        for track in actual_tracks
        if track.get("type") == "audio"
        and re.search(r"commentary|评论|解说", str(track.get("name") or ""), re.IGNORECASE)
    ]
    if commentary:
        mismatches.append("commentary audio remains")
    warnings: list[str] = []
    if "expectedChapters" in job:
        actual_chapters = bool(inventory.get("chapters", {}).get("present"))
        source = os.path.normcase(str(job.get("source") or ""))
        arguments = [str(value) for value in job.get("arguments", [])]
        source_indexes = [
            index
            for index, value in enumerate(arguments)
            if os.path.normcase(value) == source
        ]
        primary_source_kept_chapters = bool(source_indexes) and "--no-chapters" not in arguments[: source_indexes[0]]
        if (
            allow_preserved_chapter_repair
            and actual_chapters
            and not bool(job["expectedChapters"])
            and primary_source_kept_chapters
        ):
            job["expectedChapters"] = True
            job["chapters"] = "preserve"
            warnings.append(
                f"legacy chapter expectation repaired from the executed remux command: {path}"
            )
        if actual_chapters != bool(job["expectedChapters"]):
            mismatches.append(f"chapters expected={bool(job['expectedChapters'])} actual={actual_chapters}")
    if "expectedAttachments" in job:
        inferred_attachments = external_ass_attachment_names(job)
        if inferred_attachments:
            job["expectedAttachments"] = sorted({
                *(str(value) for value in job.get("expectedAttachments", [])),
                *inferred_attachments,
            })
        expected_attachments = sorted({str(value) for value in job.get("expectedAttachments", [])})
        actual_names = sorted({str(item.get("name", "")) for item in inventory.get("attachments", [])})
        if actual_names != expected_attachments:
            mismatches.append(f"attachments expected={expected_attachments} actual={actual_names}")
    if mismatches:
        raise WorkflowError("LOCAL_TRACK_MISMATCH", f"MKV does not match confirmed plan: {path}: {mismatches}")
    return inventory, warnings


def exact_track_id_map(
    path: Path,
    media_inventory: dict[str, Any],
    mkvmerge: str,
    selected_keys: list[str] | None = None,
) -> dict[str, int]:
    payload = media_inspection.read_mkvmerge_json(path, mkvmerge)
    mux_inventory = media_inspection.normalize_mkvmerge(payload)
    return media_inspection.map_selected_tracks(
        media_inventory.get("tracks", []), mux_inventory.get("tracks", []), selected_keys
    )


def remux_job_input_signatures(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic lightweight signatures for every local input of one job."""

    candidates: list[Path] = []
    if job.get("source"):
        candidates.append(resolve_path(job["source"]))
    for source_plan in job.get("trackSources", []):
        if source_plan.get("source"):
            candidates.append(resolve_path(source_plan["source"]))
    for value in job.get("arguments", []):
        candidate = Path(str(value))
        if candidate.is_absolute() and candidate.is_file():
            candidates.append(candidate.resolve())
    unique: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.is_file():
            unique[os.path.normcase(str(candidate.resolve()))] = candidate.resolve()
    return [file_signature(unique[key]) for key in sorted(unique)]


def remux_job_digest(job: dict[str, Any], inputs: list[dict[str, Any]]) -> str:
    plan = {key: value for key, value in job.items() if key != "output"}
    return canonical_metadata_digest({"plan": plan, "inputs": inputs})


def estimated_remux_bytes(job: dict[str, Any]) -> int:
    """Conservative output estimate: selected media plus subtitle/font inputs."""

    return sum(int(item.get("size") or 0) for item in remux_job_input_signatures(job))


def execute_remux(
    manifest: dict[str, Any],
    mkvmerge: str,
    *,
    runner: Callable[[list[str]], dict[str, Any]],
    track_mapper: Callable[[Path, dict[str, Any], str, list[str] | None], dict[str, int]],
    validator: Callable[[Path, str, dict[str, Any]], tuple[dict[str, Any], list[str]]],
    direct_output: bool = False,
    defer_output_validation: bool = False,
    on_warning: Callable[[str, str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop_after_current: Callable[[], bool | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    jobs = manifest.get("plan", {}).get("remuxJobs", [])
    if not jobs:
        return {"status": "SKIPPED", "reason": "NO_REMUX_JOBS"}
    results: list[dict[str, Any]] = []
    work = resolve_path(manifest["workPath"])
    output_root = artifact_output_root(work)
    previous_results = list(manifest.get("remuxResults", []))
    protected_paths: set[str] = set()

    def path_key(value: str | Path) -> str:
        return os.path.normcase(str(resolve_path(value)))

    for item in manifest.get("discovery", {}).get("videos", []):
        source_path = item.get("file", {}).get("path")
        if source_path:
            protected_paths.add(path_key(source_path))
    for job in jobs:
        if job.get("source"):
            protected_paths.add(path_key(job["source"]))
        for source_plan in job.get("trackSources", []):
            if source_plan.get("source"):
                protected_paths.add(path_key(source_plan["source"]))

    remux_temp = temporary_path(work, "remux")
    attempt_path = temporary_path(work, "remux", "attempt.json")
    resume_path = temporary_path(work, "remux", "resume.json")
    if attempt_path.is_file():
        try:
            interrupted_attempt = read_json(attempt_path)
        except Exception as exc:
            raise WorkflowError("REMUX_ATTEMPT_CACHE_INVALID", f"Invalid remux attempt cache: {attempt_path}") from exc
        if not isinstance(interrupted_attempt, dict) or interrupted_attempt.get("schema") not in {1, 2}:
            raise WorkflowError("REMUX_ATTEMPT_CACHE_INVALID", f"Invalid remux attempt cache: {attempt_path}")
        interrupted_outputs = (
            interrupted_attempt.get("outputs", [])
            if interrupted_attempt.get("schema") == 1
            else [interrupted_attempt.get("output")]
        )
        if not isinstance(interrupted_outputs, list):
            raise WorkflowError("REMUX_ATTEMPT_CACHE_INVALID", f"Invalid remux attempt cache: {attempt_path}")
        for signature in interrupted_outputs:
            if not isinstance(signature, dict) or not signature.get("path"):
                continue
            candidate = resolve_path(signature["path"])
            committed = False
            if interrupted_attempt.get("schema") == 2 and resume_path.is_file():
                try:
                    cached_jobs = read_json(resume_path).get("jobs", [])
                except Exception:
                    cached_jobs = []
                for cached in cached_jobs if isinstance(cached_jobs, list) else []:
                    if not isinstance(cached, dict):
                        continue
                    cached_result = cached.get("result")
                    cached_output = cached_result.get("output") if isinstance(cached_result, dict) else None
                    if (
                        cached.get("index") == interrupted_attempt.get("index")
                        and cached.get("jobDigest") == interrupted_attempt.get("jobDigest")
                        and isinstance(cached_output, dict)
                        and path_key(cached_output.get("path", "")) == path_key(candidate)
                        and signature_matches(cached_output)
                    ):
                        committed = True
                        break
            if (
                not committed
                and path_key(candidate) not in protected_paths
                and is_under(candidate, output_root)
                and signature_matches(signature)
            ):
                candidate.unlink()
        attempt_path.unlink()
    if remux_temp.is_dir():
        for stale in remux_temp.glob("*.tmp"):
            stale.unlink(missing_ok=True)

    resume_entries: dict[int, dict[str, Any]] = {}
    if resume_path.is_file():
        try:
            resume_cache = read_json(resume_path)
        except Exception as exc:
            raise WorkflowError("REMUX_RESUME_CACHE_INVALID", f"Invalid remux resume cache: {resume_path}") from exc
        if (
            not isinstance(resume_cache, dict)
            or resume_cache.get("schema") != 1
            or not isinstance(resume_cache.get("jobs"), list)
        ):
            raise WorkflowError("REMUX_RESUME_CACHE_INVALID", f"Invalid remux resume cache: {resume_path}")
        for entry in resume_cache["jobs"]:
            if isinstance(entry, dict) and isinstance(entry.get("index"), int):
                resume_entries[int(entry["index"])] = entry
    original_resume_entries = dict(resume_entries)

    job_inputs = [remux_job_input_signatures(job) for job in jobs]
    job_digests = [remux_job_digest(job, job_inputs[index]) for index, job in enumerate(jobs)]
    reusable_entries: dict[int, dict[str, Any]] = {}
    for index, digest in enumerate(job_digests):
        entry = resume_entries.get(index)
        item_result = entry.get("result") if isinstance(entry, dict) else None
        output_signature = item_result.get("output") if isinstance(item_result, dict) else None
        inventory = item_result.get("inventory") if isinstance(item_result, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("jobDigest") == digest
            and isinstance(output_signature, dict)
            and output_signature.get("path")
            and isinstance(inventory, dict)
            and inventory.get("status") != "DEFERRED"
        ):
            candidate = resolve_path(output_signature["path"])
            if (
                path_key(candidate) not in protected_paths
                and is_under(candidate, output_root)
                and signature_matches(output_signature)
            ):
                reusable_entries[index] = entry

    remaining_estimates = {
        index: sum(int(item.get("size") or 0) for item in job_inputs[index])
        for index, _job in enumerate(jobs)
        if index not in reusable_entries
    }
    remaining_required = sum(remaining_estimates.values())
    available_bytes = shutil.disk_usage(work).free
    reserve_bytes = max(1024**3, int(remaining_required * 0.05)) if remaining_required else 0
    if remaining_required and available_bytes < remaining_required + reserve_bytes:
        details = {
            "estimated_output_bytes": remaining_required,
            "reserve_bytes": reserve_bytes,
            "required_bytes": remaining_required + reserve_bytes,
            "available_bytes": available_bytes,
            "remaining_items": len(remaining_estimates),
            "total_items": len(jobs),
            "reused_items": len(reusable_entries),
        }
        raise WorkflowError(
            "ARCHIVE_INSUFFICIENT_SPACE",
            "Insufficient space for remaining remux outputs: "
            f"required={details['required_bytes']} available={available_bytes}",
            details=details,
            retryable=True,
        )

    temporary_outputs: list[Path] = []
    removed_superseded: list[str] = []
    current_created_signature: dict[str, Any] | None = None
    reused_items = 0

    def report_progress(completed: int, current: Path, action: str) -> None:
        if on_progress is None:
            return
        try:
            current_available = shutil.disk_usage(work).free
            on_progress(
                {
                    "stage": "remux",
                    "completed_items": completed,
                    "total_items": len(jobs),
                    "current_item": str(current),
                    "reused_items": reused_items,
                    "remaining_items": max(len(jobs) - completed, 0),
                    "remaining_bytes": remaining_required,
                    "available_bytes": current_available,
                    "action": action,
                }
            )
        except Exception:
            # Progress reporting is observational and must never change remux results.
            return

    try:
        for index, job in enumerate(jobs):
            planned_output = resolve_path(job["output"])
            reusable = reusable_entries.get(index)
            if reusable is not None:
                cached_result = reusable["result"]
                output = resolve_path(cached_result["output"]["path"])
                cached_job_state = cached_result.get("jobState")
                if isinstance(cached_job_state, dict):
                    for key in ("expectedChapters", "chapters"):
                        if key in cached_job_state:
                            job[key] = cached_job_state[key]
                job["output"] = str(output)
                for final_job in manifest.get("plan", {}).get("final", {}).get("video", []):
                    if os.path.normcase(str(final_job.get("source", ""))) == os.path.normcase(str(planned_output)):
                        final_job["source"] = str(output)
                        if job.get("expectedChapters") is not None:
                            final_job["expectedChapters"] = job["expectedChapters"]
                results.append(cached_result)
                reused_items += 1
                report_progress(index + 1, output, "reused")
                stop_request = should_stop_after_current() if index + 1 < len(jobs) and should_stop_after_current else False
                if stop_request:
                    raise WorkflowError(
                        "ARCHIVE_SAFE_STOP_REQUESTED",
                        "The current file is complete; recheck affected files before continuing",
                        details={
                            "completed_items": index + 1,
                            "total_items": len(jobs),
                            **(stop_request if isinstance(stop_request, dict) else {}),
                        },
                        retryable=True,
                    )
                continue

            output = planned_output
            if direct_output:
                if output.exists():
                    output = numbered_output_path(output)
                job["output"] = str(output)
                for final_job in manifest.get("plan", {}).get("final", {}).get("video", []):
                    if os.path.normcase(str(final_job.get("source", ""))) == os.path.normcase(str(planned_output)):
                        final_job["source"] = str(output)
            output_preexisted = output.exists()
            if output_preexisted:
                raise WorkflowError("REMUX_OUTPUT_CHANGED", f"Remux output appeared before write: {output}")
            report_progress(index, output, "processing")
            arguments = [str(value) for value in job.get("arguments", [])]
            track_map: dict[str, int] = {}
            if job.get("trackSources"):
                for source_index, source_plan in enumerate(job["trackSources"]):
                    source = resolve_path(source_plan["source"])
                    discovery = next(
                        (
                            item
                            for item in manifest.get("discovery", {}).get("videos", [])
                            if os.path.normcase(item.get("file", {}).get("path", "")) == os.path.normcase(str(source))
                        ),
                        None,
                    )
                    if not discovery:
                        raise WorkflowError("MEDIA_DISCOVERY_MISSING", f"No MediaInfo inventory for remux source: {source}")
                    try:
                        source_map = track_mapper(
                            source, discovery, mkvmerge, list(source_plan.get("selectedTrackKeys", []))
                        )
                    except Exception as exc:
                        category = getattr(exc, "category", "FAILED")
                        code = getattr(exc, "code", "TRACK_MAPPING_FAILED")
                        raise WorkflowError(code, str(exc), category) from exc
                    for track_key, track_id in source_map.items():
                        marker = "{{track:" + str(source_index) + ":" + track_key + "}}"
                        arguments = [value.replace(marker, str(track_id)) for value in arguments]
                        track_map[f"{source_index}:{track_key}"] = track_id
            elif job.get("source") and job.get("selectedTrackKeys"):
                source = resolve_path(job["source"])
                discovery = next(
                    (
                        item
                        for item in manifest.get("discovery", {}).get("videos", [])
                        if os.path.normcase(item.get("file", {}).get("path", "")) == os.path.normcase(str(source))
                    ),
                    None,
                )
                if not discovery:
                    raise WorkflowError("MEDIA_DISCOVERY_MISSING", f"No MediaInfo inventory for remux source: {source}")
                try:
                    track_map = track_mapper(source, discovery, mkvmerge, list(job["selectedTrackKeys"]))
                except Exception as exc:
                    category = getattr(exc, "category", "FAILED")
                    code = getattr(exc, "code", "TRACK_MAPPING_FAILED")
                    raise WorkflowError(code, str(exc), category) from exc
                for track_key, track_id in track_map.items():
                    marker = "{{track:" + track_key + "}}"
                    arguments = [value.replace(marker, str(track_id)) for value in arguments]
            if job.get("chapters", "preserve") == "preserve" and job.get("dropPrimaryChapters"):
                raise WorkflowError("CHAPTER_POLICY_CONFLICT", "Confirmed chapter policy conflicts with primary input options")
            if not arguments:
                raise WorkflowError("REMUX_ARGUMENTS_MISSING", f"Remux job {index} has no arguments")
            temporary = temporary_path(work, "remux", f"{index:04d}-{output.name}.tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            temporary_outputs.append(temporary)
            command = [mkvmerge, "-o", str(temporary), *arguments]
            result = runner(command)
            if result["exitCode"] not in {0, 1} or not temporary.is_file():
                message = result["stderr"] or result["stdout"]
                if "no space left" in message.casefold():
                    current_available = shutil.disk_usage(work).free
                    raise WorkflowError(
                        "ARCHIVE_INSUFFICIENT_SPACE",
                        message,
                        details={
                            "estimated_output_bytes": remaining_required,
                            "available_bytes": current_available,
                            "remaining_items": len(jobs) - index,
                            "total_items": len(jobs),
                            "reused_items": reused_items,
                        },
                        retryable=True,
                    )
                raise WorkflowError("MKVMERGE_FAILED", message)
            output.parent.mkdir(parents=True, exist_ok=True)
            pending_signature = file_signature(temporary)
            pending_signature["path"] = str(output)
            current_created_signature = pending_signature
            write_json_atomic(
                attempt_path,
                {
                    "schema": 2,
                    "index": index,
                    "jobDigest": job_digests[index],
                    "output": pending_signature,
                },
            )
            os.replace(temporary, output)
            output_signature = file_signature(output)
            warnings: list[str] = []
            if defer_output_validation and not direct_output:
                inventory = {"status": "DEFERRED"}
            else:
                inventory, warnings = validator(output, mkvmerge, job)
                if on_warning:
                    for warning in warnings:
                        on_warning("TRACK_EXPECTATION_UNAVAILABLE", warning)
            if result["exitCode"] == 1 and on_warning:
                on_warning("MKVMERGE_WARNING", f"mkvmerge warning for {output}")
            item_result = {
                "output": output_signature,
                "inventory": inventory,
                "warnings": warnings,
                "command": command,
                "trackIdMap": track_map,
                "jobDigest": job_digests[index],
                "jobState": {
                    "expectedChapters": job.get("expectedChapters"),
                    "chapters": job.get("chapters"),
                },
            }
            results.append(item_result)
            resume_entries[index] = {
                "index": index,
                "jobDigest": job_digests[index],
                "inputs": job_inputs[index],
                "result": item_result,
            }
            write_json_atomic(
                resume_path,
                {"schema": 1, "jobs": [resume_entries[key] for key in sorted(resume_entries)]},
            )
            attempt_path.unlink(missing_ok=True)
            current_created_signature = None
            remaining_required = max(remaining_required - remaining_estimates.get(index, 0), 0)
            report_progress(index + 1, output, "completed")
            stop_request = should_stop_after_current() if index + 1 < len(jobs) and should_stop_after_current else False
            if stop_request:
                raise WorkflowError(
                    "ARCHIVE_SAFE_STOP_REQUESTED",
                    "The current file is complete; recheck affected files before continuing",
                    details={
                        "completed_items": index + 1,
                        "total_items": len(jobs),
                        **(stop_request if isinstance(stop_request, dict) else {}),
                    },
                    retryable=True,
                )

        current_paths = {path_key(item["output"]["path"]) for item in results}
        stale_results = [
            entry.get("result")
            for entry in original_resume_entries.values()
        ]
        for old_result in [*previous_results, *stale_results]:
            signature = old_result.get("output") if isinstance(old_result, dict) else None
            if not isinstance(signature, dict) or not signature.get("path"):
                continue
            old_output = resolve_path(signature["path"])
            key = path_key(old_output)
            if key in current_paths or key in protected_paths or not is_under(old_output, output_root):
                continue
            if signature_matches(signature):
                old_output.unlink()
                removed_superseded.append(str(old_output))
    except Exception:
        for temporary in temporary_outputs:
            temporary.unlink(missing_ok=True)
        if current_created_signature is not None:
            candidate = resolve_path(current_created_signature["path"])
            if (
                path_key(candidate) not in protected_paths
                and is_under(candidate, output_root)
                and signature_matches(current_created_signature)
            ):
                candidate.unlink()
        attempt_path.unlink(missing_ok=True)
        try:
            remux_temp.rmdir()
        except OSError:
            pass
        raise
    attempt_path.unlink(missing_ok=True)
    write_json_atomic(
        resume_path,
        {
            "schema": 1,
            "jobs": [resume_entries[index] for index in range(len(jobs)) if index in resume_entries],
        },
    )
    manifest["remuxResults"] = results
    return {
        "status": "COMPLETE",
        "files": results,
        "removedSuperseded": removed_superseded,
        "stage": {
            "files": len(results),
            "reused": reused_items,
            "removedSuperseded": len(removed_superseded),
        },
    }
