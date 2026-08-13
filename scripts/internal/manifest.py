"""Execution-cache state, stage invalidation, and approval boundaries."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archive_rules import BACKEND_CACHE_SCHEMA, WORKFLOW_REVISION, backend_cache_path, resolve_path
from common import read_json, write_json_atomic
from internal.errors import WorkflowError
from internal.signatures import signature_matches
from internal.subtitle_pipeline import read_ass_text, write_ass_text


STAGES = (
    "inspect",
    "configure",
    "movie-audio",
    "prepare-fonts",
    "subset",
    "rename",
    "remux",
    "package",
    "verify-local",
    "final-prepare",
    "final-video",
    "final-zip",
    "final-tracker",
    "cleanup",
)

DOWNSTREAM = {
    "movie-audio": ("movie-audio", "remux", "verify-local", "final-video", "final-tracker", "cleanup"),
    "prepare-fonts": ("prepare-fonts", "subset", "rename", "remux", "package", "verify-local", "final-video", "final-zip", "final-tracker", "cleanup"),
    "subset": ("subset", "rename", "remux", "package", "verify-local", "final-video", "final-zip", "final-tracker", "cleanup"),
    "rename": ("rename", "remux", "package", "verify-local", "final-video", "final-zip", "final-tracker", "cleanup"),
    "remux": ("remux", "verify-local", "final-video", "final-tracker", "cleanup"),
    "package": ("package", "verify-local", "final-zip", "final-tracker", "cleanup"),
    "verify-local": ("verify-local", "final-prepare", "final-video", "final-zip", "final-tracker", "cleanup"),
    "final-prepare": ("final-prepare", "final-video", "final-zip", "final-tracker", "cleanup"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_stage_map() -> dict[str, Any]:
    return {stage: {"status": "PENDING", "updatedAt": None} for stage in STAGES}


def set_stage(manifest: dict[str, Any], stage: str, status: str, **details: Any) -> None:
    manifest.setdefault("stages", empty_stage_map())
    manifest["stages"][stage] = {"status": status, "updatedAt": utc_now(), **details}


def add_event(manifest: dict[str, Any], category: str, code: str, message: str, stage: str) -> None:
    manifest.setdefault("events", []).append(
        {"time": utc_now(), "category": category, "code": code, "message": message, "stage": stage}
    )


def invalidate_from(manifest: dict[str, Any], stage: str, reason: str) -> None:
    for selected in DOWNSTREAM.get(stage, (stage,)):
        set_stage(manifest, selected, "PENDING", invalidatedBy=stage, reason=reason)
    add_event(manifest, "AUTO_RECOVERED", "DOWNSTREAM_INVALIDATED", reason, stage)


def source_drift(manifest: dict[str, Any]) -> list[str]:
    changed = []
    for video in manifest.get("discovery", {}).get("videos", []):
        signature = video.get("file")
        if signature and not signature_matches(signature):
            changed.append(signature["path"])
    for subtitle in manifest.get("discovery", {}).get("subtitles", []):
        signature = subtitle.get("file")
        if signature and not signature_matches(signature):
            changed.append(signature["path"])
    return changed


def require_no_source_drift(manifest_path: Path, manifest: dict[str, Any], stage: str) -> None:
    changed = source_drift(manifest)
    if not changed:
        return
    invalidate_from(
        manifest,
        "prepare-fonts" if any(path.casefold().endswith((".ass", ".ssa")) for path in changed) else "remux",
        f"Source inputs changed: {changed}",
    )
    save_manifest(manifest_path, manifest)
    raise WorkflowError("SOURCE_INPUT_CHANGED", f"Source inputs changed after inspection: {changed}")


def require_result_signatures(
    manifest_path: Path,
    manifest: dict[str, Any],
    stage: str,
    signatures: list[dict[str, Any]],
    invalidate_stage: str,
) -> None:
    invalid = [item.get("path", "") for item in signatures if not signature_matches(item)]
    if not invalid:
        return
    invalidate_from(manifest, invalidate_stage, f"Upstream artifacts changed or disappeared: {invalid}")
    save_manifest(manifest_path, manifest)
    raise WorkflowError("UPSTREAM_ARTIFACT_CHANGED", f"Upstream artifacts changed or disappeared: {invalid}")


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError("BACKEND_CACHE_NOT_FOUND", f"Execution cache not found: {path}")
    manifest = read_json(path)
    if manifest.get("schemaVersion") != BACKEND_CACHE_SCHEMA:
        raise WorkflowError("BACKEND_CACHE_SCHEMA", "execution cache contract mismatch")
    if manifest.get("workflowRevision") != WORKFLOW_REVISION:
        raise WorkflowError("BACKEND_CACHE_REVISION", "execution cache workflow revision mismatch")
    work = resolve_path(manifest.get("workPath", ""))
    if path.resolve(strict=False) != backend_cache_path(work).resolve(strict=False):
        raise WorkflowError("BACKEND_CACHE_PATH", "execution cache path violates the static contract")
    return manifest


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["schemaVersion"] = BACKEND_CACHE_SCHEMA
    manifest["workflowRevision"] = WORKFLOW_REVISION
    manifest["updatedAt"] = utc_now()
    write_json_atomic(path, manifest)


def normalized_plan(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def jobs_equal_except(old_jobs: Any, new_jobs: Any, field: str) -> bool:
    if not isinstance(old_jobs, list) or not isinstance(new_jobs, list) or len(old_jobs) != len(new_jobs):
        return False
    for old_job, new_job in zip(old_jobs, new_jobs):
        if not isinstance(old_job, dict) or not isinstance(new_job, dict):
            return False
        old_copy = {key: value for key, value in old_job.items() if key != field}
        new_copy = {key: value for key, value in new_job.items() if key != field}
        if old_copy != new_copy:
            return False
    return old_jobs != new_jobs


def reuse_path_only_outputs(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Reconnect completed artifacts when only their planned path changed."""

    for jobs_key, field, ass in (("renameJobs", "target", True), ("remuxJobs", "output", False)):
        old_jobs = old.get(jobs_key, [])
        new_jobs = new.get(jobs_key, [])
        if not jobs_equal_except(old_jobs, new_jobs, field):
            continue
        for old_job, new_job in zip(old_jobs, new_jobs):
            old_path = resolve_path(old_job[field])
            new_path = resolve_path(new_job[field])
            if not old_path.is_file() or old_path == new_path:
                continue
            if new_path.exists():
                new_job[field] = str(old_path)
                if jobs_key == "remuxJobs":
                    for final_job in new.get("final", {}).get("video", []):
                        if os.path.normcase(str(final_job.get("source", ""))) == os.path.normcase(str(new_path)):
                            final_job["source"] = str(old_path)
                continue
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if ass:
                write_ass_text(new_path, read_ass_text(old_path))
                old_path.unlink()
            else:
                old_path.replace(new_path)


def changed_plan_stage(old: dict[str, Any], new: dict[str, Any]) -> str | None:
    if old == new:
        return None
    if old.get("movieAudioPlans") != new.get("movieAudioPlans"):
        return "movie-audio"
    if old.get("subtitleGroups") != new.get("subtitleGroups"):
        return "prepare-fonts"
    if old.get("renameJobs") != new.get("renameJobs"):
        return "package" if jobs_equal_except(old.get("renameJobs", []), new.get("renameJobs", []), "target") else "rename"
    if old.get("remuxJobs") != new.get("remuxJobs"):
        return "verify-local" if jobs_equal_except(old.get("remuxJobs", []), new.get("remuxJobs", []), "output") else "remux"
    if old.get("chapters") != new.get("chapters"):
        return "remux"
    if old.get("package") != new.get("package"):
        return "package"
    if any(old.get(key) != new.get(key) for key in ("final", "preferredLibrary", "title", "expectedStatus")):
        return "final-prepare"
    return "prepare-fonts"


def require_execution(args: argparse.Namespace, manifest: dict[str, Any], approval_key: str = "preflight") -> None:
    if not getattr(args, "execute", False):
        raise WorkflowError("EXECUTE_REQUIRED", "Mutation requires --execute", "DECISION_REQUIRED")
    approved = getattr(args, "approved_plan", None)
    if not approved or not approved.strip():
        raise WorkflowError("APPROVAL_REQUIRED", "Mutation requires --approved-plan", "DECISION_REQUIRED")
    manifest.setdefault("approvals", {})[approval_key] = {"text": approved.strip(), "recordedAt": utc_now()}
