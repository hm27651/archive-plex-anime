"""Final NAS/ZIP delivery, checkpoints, and tracker commit execution."""

from __future__ import annotations

import concurrent.futures
import copy
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from archive_rules import resolve_path, state_path
from common import load_state as load_task_state
from common import save_state as save_task_state
from internal import library_target, tracker
from internal.errors import WorkflowError
from internal.signatures import canonical_metadata_digest, file_signature, signature_matches
from internal.subtitle_archive import zip_inventory_signature


def _merge_directory(source: Path, target: Path, allowed_overwrites: set[str]) -> list[Path]:
    deferred: list[Path] = []
    target.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        destination = target / child.name
        if not destination.exists():
            child.replace(destination)
        elif child.is_dir() and destination.is_dir():
            deferred.extend(_merge_directory(child, destination, allowed_overwrites))
        elif os.path.normcase(str(destination.resolve(strict=False))) in allowed_overwrites:
            deferred.append(child)
        else:
            raise WorkflowError("TV_MERGE_CONFLICT", f"Out-of-scope merge collision: {destination}", "DECISION_REQUIRED")
    try:
        source.rmdir()
    except OSError:
        pass
    return deferred


def normalize_tv_directory(candidate: dict[str, Any], allowed_destinations: list[str]) -> list[Path]:
    destinations = [resolve_path(value) for value in allowed_destinations]
    allowed = {os.path.normcase(str(value)) for value in destinations}
    source = resolve_path(candidate["path"])
    clean_root = source.with_name(library_target.normalize_title(source.name, "tv"))
    deferred: list[Path] = []
    if source.exists() and source != clean_root:
        if clean_root.exists():
            deferred.extend(_merge_directory(source, clean_root, allowed))
        else:
            source.replace(clean_root)
    if clean_root.exists():
        for season in list(clean_root.iterdir()):
            if not season.is_dir() or not library_target.is_webrip_marked(season.name):
                continue
            target = season.with_name(library_target.normalize_title(season.name, "tv"))
            if target.exists():
                deferred.extend(_merge_directory(season, target, allowed))
            else:
                season.replace(target)
    deferred_keys = {os.path.normcase(str(path)) for path in deferred}
    for destination in destinations:
        parent = destination.parent
        if not parent.is_dir():
            continue
        for sibling in parent.iterdir():
            key = os.path.normcase(str(sibling))
            if (
                sibling.is_file()
                and sibling.suffix.casefold() in {".mkv", ".mp4"}
                and sibling.stem.casefold() == destination.stem.casefold()
                and key != os.path.normcase(str(destination))
                and key not in deferred_keys
            ):
                deferred.append(sibling)
                deferred_keys.add(key)
    return deferred


def _final_transfer_temporary(destination: Path, batch_id: str) -> Path:
    token = re.sub(r"[^0-9A-Za-z_-]+", "-", batch_id).strip("-")[:16] or "unsealed"
    target_token = canonical_metadata_digest(str(destination))[:12]
    return destination.with_name(f"archive-{token}-{target_token}.part")


def _source_snapshot_matches(source: Path, expected: dict[str, Any]) -> bool:
    try:
        current = file_signature(source)
    except OSError:
        return False
    return current["size"] == expected.get("size") and current["mtimeUtcNs"] == expected.get("mtimeUtcNs")


def _direct_final_copy(source: Path, destination: Path, operation: str, owned_partial: bool) -> None:
    if operation != "create" or owned_partial:
        shutil.copy2(source, destination)
        return
    try:
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream, length=16 * 1024 * 1024)
    except FileExistsError as exc:
        raise WorkflowError(
            "FINAL_CREATE_TARGET_CHANGED",
            f"Create target appeared before direct fallback: {destination}",
        ) from exc
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass


def copy_and_verify(job: dict[str, Any], _media_tool: str | None = None) -> dict[str, Any]:
    source, destination = resolve_path(job["source"]), resolve_path(job["destination"])
    if not source.is_file():
        raise WorkflowError("FINAL_SOURCE_MISSING", f"Final source missing: {source}")
    reviewed_signature = job.get("sourceSignature")
    if isinstance(reviewed_signature, dict) and not signature_matches(reviewed_signature):
        raise WorkflowError("FINAL_SOURCE_CHANGED", f"Final source changed after review: {source}")
    source_snapshot = file_signature(source)
    source_size = int(source_snapshot["size"])
    operation = str(job.get("operation") or ("replace" if destination.exists() else "create"))
    if operation == "upsert":
        operation = "replace" if destination.exists() else "create"
    if operation not in {"create", "replace"}:
        raise WorkflowError("FINAL_OPERATION_INVALID", f"Unsupported final operation: {operation}")
    batch_id = str(job.get("batchId") or "unsealed")
    work = job.get("_work")
    checkpoint_lock = job.get("_checkpoint_lock")
    kind = str(job.get("kind") or "video")

    def require_zip_merge_base_unchanged() -> None:
        expected = job.get("expectedDestinationZipSignature")
        if kind == "zip" and isinstance(expected, dict) and zip_inventory_signature(destination) != expected:
            raise WorkflowError("ZIP_MERGE_BASE_CHANGED", f"Subtitle archive changed after review: {destination}")

    require_zip_merge_base_unchanged()
    owned_partial = bool(
        work
        and _final_attempt_matches(
            resolve_path(work),
            batch_id,
            kind,
            str(destination),
            str(source),
            source_size,
            operation,
        )
    )
    if operation == "create" and destination.exists() and not owned_partial:
        raise WorkflowError("FINAL_CREATE_TARGET_CHANGED", f"Create target appeared after confirmation: {destination}")
    if operation == "replace" and not destination.exists():
        raise WorkflowError("FINAL_REPLACE_TARGET_CHANGED", f"Replace target disappeared after confirmation: {destination}")
    if operation == "create" and owned_partial and destination.is_file() and destination.stat().st_size == source_size:
        return {
            "source": str(source),
            "destination": str(destination),
            "verification": {"size": source_size, "method": "direct-overwrite-recovered"},
            "warning": f"Recovered a completed direct-overwrite fallback for {destination}",
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _final_transfer_temporary(destination, batch_id)
    force_direct = bool(job.get("_force_direct_fallback"))
    warning = None
    disable_atomic_replace = False

    if not force_direct:
        if temporary.exists() and (not temporary.is_file() or temporary.stat().st_size != source_size):
            temporary.unlink(missing_ok=True)
        if not temporary.is_file():
            shutil.copy2(source, temporary)
        if not _source_snapshot_matches(source, source_snapshot):
            temporary.unlink(missing_ok=True)
            raise WorkflowError("FINAL_SOURCE_CHANGED", f"Final source changed during copy: {source}")
        if temporary.stat().st_size != source_size:
            raise WorkflowError("FINAL_SIZE_MISMATCH", f"Temporary copy size mismatch: {temporary}")

        try:
            require_zip_merge_base_unchanged()
            if operation == "create":
                if destination.exists() and not owned_partial:
                    raise WorkflowError("FINAL_CREATE_TARGET_CHANGED", f"Create target appeared during copy: {destination}")
                os.rename(temporary, destination)
                method = "atomic-create"
            else:
                if not destination.exists():
                    raise WorkflowError("FINAL_REPLACE_TARGET_CHANGED", f"Replace target disappeared during copy: {destination}")
                os.replace(temporary, destination)
                method = "atomic-replace"
        except WorkflowError:
            raise
        except OSError as replace_error:
            if not temporary.exists() and destination.is_file() and destination.stat().st_size == source_size:
                method = "atomic-create-recovered" if operation == "create" else "atomic-replace-recovered"
                warning = f"Recovered a completed atomic commit after an SMB response error for {destination}: {replace_error}"
            else:
                method = "direct-overwrite-fallback"
                warning = f"Atomic replace failed; direct overwrite fallback was used for {destination}: {replace_error}"
                disable_atomic_replace = True
    else:
        method = "direct-overwrite-fallback"
        warning = f"Direct overwrite fallback was reused for the current destination root: {destination}"

    if method not in {"atomic-create", "atomic-create-recovered", "atomic-replace", "atomic-replace-recovered"}:
        if work and checkpoint_lock is not None:
            _save_final_attempt(
                resolve_path(work),
                batch_id,
                kind,
                str(destination),
                str(source),
                source_size,
                operation,
                checkpoint_lock,
            )
        try:
            require_zip_merge_base_unchanged()
            _direct_final_copy(source, destination, operation, owned_partial)
        except WorkflowError:
            raise
        except OSError as copy_error:
            raise WorkflowError(
                "FINAL_DIRECT_COPY_FAILED",
                f"Direct overwrite fallback failed for {destination}: {copy_error}",
            ) from copy_error
        method = "direct-overwrite-fallback"
        temporary.unlink(missing_ok=True)

    if not _source_snapshot_matches(source, source_snapshot):
        raise WorkflowError("FINAL_SOURCE_CHANGED", f"Final source changed during final write: {source}")
    if not destination.is_file() or destination.stat().st_size != source_size:
        raise WorkflowError("FINAL_SIZE_MISMATCH", f"Copy size mismatch: {destination}")
    result = {
        "source": str(source),
        "destination": str(destination),
        "operation": operation,
        "verification": {"size": source_size, "method": method},
    }
    if warning:
        result["warning"] = warning
    if disable_atomic_replace:
        result["disableAtomicReplace"] = True
    return result


def _load_task_state(work: Path) -> tuple[Path, dict[str, Any]]:
    path = state_path(work)
    state = load_task_state(work)
    if not state:
        raise WorkflowError("STATE_REQUIRED", "current task state is required for final checkpoints")
    return path, state


def _final_attempt_matches(
    work: Path,
    batch_id: str,
    kind: str,
    destination: str,
    source: str,
    source_size: int,
    operation: str,
) -> bool:
    try:
        _, state = _load_task_state(work)
    except WorkflowError:
        return False
    final_results = state.get("final_results", {})
    if final_results.get("batch_id") != batch_id:
        return False
    attempt = final_results.get("attempts", {}).get(kind, {}).get(destination, {})
    return bool(
        attempt.get("status") == "IN_PROGRESS"
        and attempt.get("source") == source
        and attempt.get("source_size") == source_size
        and attempt.get("operation") == operation
    )


def _save_final_attempt(
    work: Path,
    batch_id: str,
    kind: str,
    destination: str,
    source: str,
    source_size: int,
    operation: str,
    lock: threading.Lock,
) -> None:
    with lock:
        _, state = _load_task_state(work)
        final_results = state.setdefault("final_results", {})
        if final_results.get("batch_id") != batch_id:
            final_results.clear()
            final_results["batch_id"] = batch_id
        final_results.setdefault("attempts", {}).setdefault(kind, {})[destination] = {
            "status": "IN_PROGRESS",
            "source": source,
            "source_size": int(source_size),
            "operation": operation,
        }
        save_task_state(work, state)


def _final_checkpoint_matches(
    work: Path,
    batch_id: str,
    kind: str,
    destination: str | None = None,
    source: str | None = None,
) -> bool:
    _, state = _load_task_state(work)
    final_results = state.get("final_results", {})
    if final_results.get("batch_id") != batch_id:
        return False
    if kind == "tracker":
        return final_results.get("tracker", {}).get("status") == "COMPLETE"
    item = final_results.get(kind, {}).get(str(destination), {})
    path = resolve_path(str(destination))
    source_path = resolve_path(str(source)) if source else None
    expected_source_size = item.get("source_size")
    return bool(
        item.get("status") == "COMPLETE"
        and path.is_file()
        and path.stat().st_size == item.get("size")
        and source_path is not None
        and source_path.is_file()
        and source_path.stat().st_size == expected_source_size
        and path.stat().st_size == expected_source_size
    )


def _save_final_checkpoint(
    work: Path,
    batch_id: str,
    kind: str,
    lock: threading.Lock,
    *,
    destination: str | None = None,
    size: int | None = None,
    source_size: int | None = None,
) -> None:
    with lock:
        _, state = _load_task_state(work)
        final_results = state.setdefault("final_results", {})
        if final_results.get("batch_id") != batch_id:
            final_results.clear()
            final_results["batch_id"] = batch_id
        if kind == "tracker":
            final_results["tracker"] = {"status": "COMPLETE"}
        else:
            final_results.setdefault(kind, {})[str(destination)] = {
                "status": "COMPLETE",
                "size": int(size or 0),
                "source_size": int(source_size or 0),
            }
            attempts = final_results.get("attempts", {}).get(kind, {})
            attempts.pop(str(destination), None)
        save_task_state(work, state)


def _tracker_chunk_progress(work: Path, batch_id: str, plan_digest: str) -> int:
    _, state = _load_task_state(work)
    final_results = state.get("final_results", {})
    if final_results.get("batch_id") != batch_id:
        return 0
    tracker_state = final_results.get("tracker", {})
    if tracker_state.get("plan_digest") != plan_digest:
        return 0
    try:
        return max(0, int(tracker_state.get("completed_chunks", 0)))
    except (TypeError, ValueError):
        return 0


def _save_tracker_chunk_progress(
    work: Path,
    batch_id: str,
    plan_digest: str,
    completed_chunks: int,
    total_chunks: int,
    lock: threading.Lock,
) -> None:
    with lock:
        _, state = _load_task_state(work)
        final_results = state.setdefault("final_results", {})
        if final_results.get("batch_id") != batch_id:
            final_results.clear()
            final_results["batch_id"] = batch_id
        final_results["tracker"] = {
            "status": "IN_PROGRESS",
            "plan_digest": plan_digest,
            "completed_chunks": int(completed_chunks),
            "total_chunks": int(total_chunks),
        }
        save_task_state(work, state)


def execute_final_delivery(
    work: Path,
    final: dict[str, Any],
    batch_id: str,
    *,
    copier: Callable[..., dict[str, Any]] | None = None,
    tracker_apply: Callable[..., dict[str, Any]] | None = None,
    directory_normalizer: Callable[[dict[str, Any], list[str]], list[Path]] | None = None,
    on_stage: Callable[[str, str, Any], None] | None = None,
) -> dict[str, Any]:
    """Commit video, subtitle ZIP, and tracker actions in parallel."""
    work = resolve_path(work)
    copier = copier or copy_and_verify
    tracker_apply = tracker_apply or tracker.apply_static_plan
    directory_normalizer = directory_normalizer or normalize_tv_directory
    checkpoint_lock = threading.Lock()
    fallback_mode_lock = threading.Lock()
    direct_fallback_roots: set[str] = set()

    def copy_with_checkpoint(job: dict[str, Any], kind: str) -> dict[str, Any]:
        destination = str(job["destination"])
        source = str(job["source"])
        if _final_checkpoint_matches(work, batch_id, kind, destination, source):
            return {"destination": destination, "status": "SKIPPED_VERIFIED"}
        destination_root = os.path.normcase(str(resolve_path(destination).anchor))
        with fallback_mode_lock:
            force_direct_fallback = destination_root in direct_fallback_roots
        item = copier(
            {
                **job,
                "kind": kind,
                "batchId": batch_id,
                "_work": str(work),
                "_checkpoint_lock": checkpoint_lock,
                "_force_direct_fallback": force_direct_fallback,
            }
        )
        if item.get("disableAtomicReplace"):
            with fallback_mode_lock:
                direct_fallback_roots.add(destination_root)
        destination_path = resolve_path(destination)
        source_path = resolve_path(job["source"])
        checkpoint_size = (
            destination_path.stat().st_size
            if destination_path.is_file()
            else source_path.stat().st_size
            if source_path.is_file()
            else 0
        )
        _save_final_checkpoint(
            work,
            batch_id,
            kind,
            checkpoint_lock,
            destination=destination,
            size=checkpoint_size,
            source_size=source_path.stat().st_size if source_path.is_file() else 0,
        )
        return item

    def run_video() -> dict[str, Any]:
        jobs = final.get("video", [])
        deferred: list[Path] = []
        if final.get("tvDirectoryCandidate"):
            deferred = directory_normalizer(final["tvDirectoryCandidate"], [job["destination"] for job in jobs])
        items = [copy_with_checkpoint(job, "video") for job in jobs]
        for stale in deferred:
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        candidate = final.get("tvDirectoryCandidate")
        if candidate:
            marked_root = resolve_path(candidate["path"])
            clean_root = marked_root.with_name(library_target.normalize_title(marked_root.name, "tv"))
            cleanup_dirs = [marked_root]
            if clean_root.exists():
                cleanup_dirs.extend(
                    item for item in clean_root.iterdir()
                    if item.is_dir() and library_target.is_webrip_marked(item.name)
                )
            for directory in sorted(cleanup_dirs, key=lambda item: len(item.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return {"items": items}

    def run_zip() -> dict[str, Any]:
        return {"items": [copy_with_checkpoint(job, "zip") for job in final.get("zip", [])]}

    def run_tracker() -> dict[str, Any]:
        if _final_checkpoint_matches(work, batch_id, "tracker"):
            return {"status": "SKIPPED_VERIFIED"}
        tracker_plan = final.get("trackerPlan")
        if not tracker_plan:
            _save_final_checkpoint(work, batch_id, "tracker", checkpoint_lock)
            return {"status": "SKIPPED"}
        tracker_plan = copy.deepcopy(tracker_plan)
        tracker_plan["batchId"] = batch_id
        tracker_plan_digest = canonical_metadata_digest(tracker_plan)
        completed_chunks = _tracker_chunk_progress(work, batch_id, tracker_plan_digest)

        def save_tracker_progress(completed: int, total: int) -> None:
            _save_tracker_chunk_progress(
                work,
                batch_id,
                tracker_plan_digest,
                completed,
                total,
                checkpoint_lock,
            )

        result = tracker_apply(
            tracker_plan,
            str(final["trackerExecutable"]),
            completed_chunks=completed_chunks,
            on_chunk_complete=save_tracker_progress,
        )
        output = {
            "status": "COMPLETE",
            "operations": result.get("operations", 0),
            "verification": result.get("verification", {}).get("status"),
        }
        _save_final_checkpoint(work, batch_id, "tracker", checkpoint_lock)
        return output

    workers: dict[str, Callable[[], dict[str, Any]]] = {}
    if final.get("video"):
        workers["final-video"] = run_video
    if final.get("zip"):
        workers["final-zip"] = run_zip
    if final.get("trackerPlan"):
        workers["final-tracker"] = run_tracker
    completed: dict[str, Any] = {}
    failed: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(worker): stage for stage, worker in workers.items()}
        for future in concurrent.futures.as_completed(futures):
            stage = futures[future]
            try:
                result = future.result()
                completed[stage] = result
                if on_stage:
                    on_stage(stage, "COMPLETE", result)
            except Exception as exc:
                failed[stage] = str(exc)
                if on_stage:
                    on_stage(stage, "FAILED", str(exc))
    warnings = [
        str(item["warning"])
        for stage_result in completed.values()
        if isinstance(stage_result, dict)
        for item in stage_result.get("items", [])
        if isinstance(item, dict) and item.get("warning")
    ]
    return {
        "status": "FAILED" if failed else "COMPLETE",
        "completed": completed,
        "failed": failed,
        "warnings": warnings,
    }
