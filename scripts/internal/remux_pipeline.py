"""MKV inspection, exact track validation, and deterministic remux execution."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from archive_rules import is_under, numbered_output_path, resolve_path, temporary_path
from common import read_json, write_json_atomic
from internal import media_inspection
from internal.errors import WorkflowError
from internal.signatures import file_signature, signature_matches
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
    if "expectedChapters" in job:
        actual_chapters = bool(inventory.get("chapters", {}).get("present"))
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
    return inventory, []


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
) -> dict[str, Any]:
    jobs = manifest.get("plan", {}).get("remuxJobs", [])
    if not jobs:
        return {"status": "SKIPPED", "reason": "NO_REMUX_JOBS"}
    results = []
    work = resolve_path(manifest["workPath"])
    previous_results = list(manifest.get("remuxResults", []))
    previous_by_path = {
        os.path.normcase(str(resolve_path(item["output"]["path"]))): item["output"]
        for item in previous_results
        if isinstance(item, dict) and isinstance(item.get("output"), dict) and item["output"].get("path")
    }
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
    if attempt_path.is_file():
        try:
            interrupted_attempt = read_json(attempt_path)
        except Exception as exc:
            raise WorkflowError("REMUX_ATTEMPT_CACHE_INVALID", f"Invalid remux attempt cache: {attempt_path}") from exc
        if (
            not isinstance(interrupted_attempt, dict)
            or interrupted_attempt.get("schema") != 1
            or not isinstance(interrupted_attempt.get("outputs"), list)
        ):
            raise WorkflowError("REMUX_ATTEMPT_CACHE_INVALID", f"Invalid remux attempt cache: {attempt_path}")
        for signature in interrupted_attempt["outputs"]:
            if not isinstance(signature, dict) or not signature.get("path"):
                continue
            candidate = resolve_path(signature["path"])
            if path_key(candidate) not in protected_paths and is_under(candidate, work) and signature_matches(signature):
                candidate.unlink()
        attempt_path.unlink()
    if remux_temp.is_dir():
        for stale in remux_temp.glob("*.tmp"):
            stale.unlink(missing_ok=True)
    created_outputs: list[dict[str, Any]] = []
    temporary_outputs: list[Path] = []
    removed_superseded: list[str] = []
    try:
        for index, job in enumerate(jobs):
            planned_output = resolve_path(job["output"])
            output = planned_output
            if direct_output:
                previous_signature = previous_by_path.get(path_key(output))
                if output.exists() and not (
                    previous_signature and path_key(output) not in protected_paths and signature_matches(previous_signature)
                ):
                    output = numbered_output_path(output)
                job["output"] = str(output)
                for final_job in manifest.get("plan", {}).get("final", {}).get("video", []):
                    if os.path.normcase(str(final_job.get("source", ""))) == os.path.normcase(str(planned_output)):
                        final_job["source"] = str(output)
            output_preexisted = output.exists()
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
                raise WorkflowError("MKVMERGE_FAILED", result["stderr"] or result["stdout"])
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output_preexisted:
                pending_signature = file_signature(temporary)
                pending_signature["path"] = str(output)
                created_outputs.append(pending_signature)
                write_json_atomic(attempt_path, {"schema": 1, "outputs": created_outputs})
            os.replace(temporary, output)
            output_signature = file_signature(output)
            warnings: list[str] = []
            if defer_output_validation:
                inventory = {"status": "DEFERRED"}
            else:
                inventory, warnings = validator(output, mkvmerge, job)
                if on_warning:
                    for warning in warnings:
                        on_warning("TRACK_EXPECTATION_UNAVAILABLE", warning)
            if result["exitCode"] == 1 and on_warning:
                on_warning("MKVMERGE_WARNING", f"mkvmerge warning for {output}")
            results.append(
                {
                    "output": output_signature,
                    "inventory": inventory,
                    "warnings": warnings,
                    "command": command,
                    "trackIdMap": track_map,
                }
            )

        current_paths = {path_key(item["output"]["path"]) for item in results}
        for old_result in previous_results:
            signature = old_result.get("output") if isinstance(old_result, dict) else None
            if not isinstance(signature, dict) or not signature.get("path"):
                continue
            old_output = resolve_path(signature["path"])
            key = path_key(old_output)
            if key in current_paths or key in protected_paths or not is_under(old_output, work):
                continue
            if signature_matches(signature):
                old_output.unlink()
                removed_superseded.append(str(old_output))
    except Exception:
        for temporary in temporary_outputs:
            temporary.unlink(missing_ok=True)
        for signature in reversed(created_outputs):
            candidate = resolve_path(signature["path"])
            if path_key(candidate) not in protected_paths and is_under(candidate, work) and signature_matches(signature):
                candidate.unlink()
        attempt_path.unlink(missing_ok=True)
        try:
            remux_temp.rmdir()
        except OSError:
            pass
        raise
    attempt_path.unlink(missing_ok=True)
    try:
        remux_temp.rmdir()
    except OSError:
        pass
    manifest["remuxResults"] = results
    return {
        "status": "COMPLETE",
        "files": results,
        "removedSuperseded": removed_superseded,
        "stage": {"files": len(results), "removedSuperseded": len(removed_superseded)},
    }
