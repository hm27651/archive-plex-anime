"""Pure Hub task contract validation and file-scoped planning helpers.

This module deliberately has no Hub, Plex, database, or filesystem mutation
dependencies.  The Hub can use the same projections before it starts an
Archive command and the executor validates them again at its trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from internal.errors import WorkflowError


REMOTE_SOURCES = {"tmdb", "tvdb", "manual"}
EPISODE_ORDERS = {
    "tmdb": {"tmdb"},
    "aired": {"tvdb"},
    "dvd": {"tvdb"},
    "absolute": {"tvdb"},
    "alternate": {"tvdb"},
    "regional": {"tvdb"},
}
TARGET_ACTIONS = {"create", "replace", "keep", "skip", "conflict"}


def _relative(value: Any, field: str, *, allow_root: bool = False) -> str:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if allow_root and text == ".":
        return "."
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowError("ARCHIVE_TASK_SCOPE_INVALID", f"{field} must stay inside the task source root")
    return path.as_posix()


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _directory_snapshot(path: Path) -> dict[str, Any]:
    rows = []
    total_bytes = 0
    if path.is_dir():
        for item in sorted(path.rglob("*"), key=lambda value: value.as_posix().casefold()):
            if not item.is_file():
                continue
            stat = item.stat()
            size = int(stat.st_size)
            total_bytes += size
            rows.append((item.relative_to(path).as_posix(), size, int(stat.st_mtime_ns)))
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "snapshot_id": hashlib.sha256(canonical).hexdigest(),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "exists": path.is_dir(),
    }


def validate_remote_identity(value: Any, *, branch: str) -> dict[str, Any]:
    """Require one provider and one series/movie identity for the whole task."""

    if not isinstance(value, dict):
        raise WorkflowError("REMOTE_IDENTITY_REQUIRED", "A remote or manual work identity is required")
    source = str(value.get("source") or "").casefold()
    if source not in REMOTE_SOURCES:
        raise WorkflowError("REMOTE_SOURCE_INVALID", "Remote source must be TMDB, TVDB, or manual")
    raw_id = value.get("series_id" if branch == "tv" else "movie_id")
    identity = str(raw_id or "").strip()
    if source != "manual" and (not identity.isdigit() or int(identity) <= 0):
        raise WorkflowError("REMOTE_SERIES_ID_REQUIRED", "The selected provider requires one positive identity")
    order = str(value.get("episode_order") or ("tmdb" if source == "tmdb" else "aired")).casefold()
    if branch == "movie":
        order = "movie"
    elif source == "manual":
        order = "manual"
    elif order not in EPISODE_ORDERS or source not in EPISODE_ORDERS[order]:
        raise WorkflowError(
            "REMOTE_EPISODE_ORDER_INVALID",
            "Episode order does not belong to the selected provider",
        )
    bindings = value.get("season_bindings") or {}
    if not isinstance(bindings, dict):
        raise WorkflowError("REMOTE_SEASON_BINDINGS_INVALID", "season_bindings must be an object")
    for season, binding in bindings.items():
        if not isinstance(binding, dict):
            raise WorkflowError("REMOTE_SEASON_BINDINGS_INVALID", "Each season binding must be an object")
        nested_source = str(binding.get("source") or source).casefold()
        nested_id = str(binding.get("series_id") or identity).strip()
        if nested_source != source or (source != "manual" and nested_id != identity):
            raise WorkflowError(
                "REMOTE_SERIES_SPLIT_REQUIRED",
                "All seasons and S00 must use one provider and one series identity",
            )
    return {
        "source": source,
        "series_id" if branch == "tv" else "movie_id": int(identity) if identity else None,
        "episode_order": order,
        "season_bindings": bindings,
    }


def validate_task_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError("ARCHIVE_TASK_SCOPE_REQUIRED", "One-work task source scope is required")
    root = _relative(value.get("source_relative_path"), "source_relative_path", allow_root=True)
    files = value.get("files") or []
    if not isinstance(files, list) or not files:
        raise WorkflowError("ARCHIVE_TASK_FILES_REQUIRED", "At least one assigned source file is required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    targets: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise WorkflowError("ARCHIVE_TASK_FILE_INVALID", "Each assigned file must be an object")
        path = _relative(item.get("path"), "files.path")
        folded = path.casefold()
        if folded in seen:
            raise WorkflowError("ARCHIVE_TASK_FILE_DUPLICATE", "A source file may belong to only one task")
        seen.add(folded)
        target = str(item.get("target_episode") or "").upper().strip()
        if target:
            if target in targets:
                raise WorkflowError(
                    "EPISODE_TARGET_DUPLICATE",
                    "Two source files cannot map to the same target episode",
                )
            targets[target] = path
        normalized.append({**item, "path": path, "target_episode": target or None})
    exclusive = bool(value.get("exclusive_source_directory", False))
    shared_parent = bool(value.get("shared_parent_directory", False))
    if exclusive and shared_parent:
        raise WorkflowError("ARCHIVE_SOURCE_OWNERSHIP_CONFLICT", "A shared parent cannot be an exclusive source directory")
    return {
        "source_relative_path": root,
        "files": normalized,
        "exclusive_source_directory": exclusive,
        "shared_parent_directory": shared_parent,
    }


def validate_target_plan(value: Any, assigned_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowError("ARCHIVE_TARGET_PLAN_INVALID", "target_plan must be an array")
    assigned = {str(item["path"]).casefold() for item in assigned_files}
    planned: set[str] = set()
    destinations: dict[str, str] = {}
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise WorkflowError("ARCHIVE_TARGET_PLAN_INVALID", "Each target action must be an object")
        source = _relative(item.get("source"), "target_plan.source")
        if source.casefold() not in assigned or source.casefold() in planned:
            raise WorkflowError("ARCHIVE_TARGET_SOURCE_INVALID", "Target actions must reference each assigned file at most once")
        planned.add(source.casefold())
        action = str(item.get("action") or "conflict").casefold()
        if action not in TARGET_ACTIONS:
            raise WorkflowError("ARCHIVE_TARGET_ACTION_INVALID", "Target action is invalid")
        destination = str(item.get("destination") or "").replace("\\", "/").strip()
        if action in {"create", "replace"} and not destination:
            raise WorkflowError("ARCHIVE_TARGET_DESTINATION_REQUIRED", "Create and replace actions require a destination")
        if destination:
            key = destination.casefold()
            if key in destinations:
                raise WorkflowError("ARCHIVE_TARGET_DUPLICATE", "Two files cannot write the same destination")
            destinations[key] = source
        result.append({**item, "source": source, "action": action, "destination": destination or None})
    return result


def file_snapshot_id(item: dict[str, Any]) -> str:
    canonical = json.dumps(
        {key: item.get(key) for key in ("path", "size", "mtime_ns", "target_signature")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def invalidated_files(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[str]:
    """Return only files whose source or selected target evidence changed."""

    before = {str(item.get("path") or "").casefold(): file_snapshot_id(item) for item in previous}
    after = {str(item.get("path") or "").casefold(): file_snapshot_id(item) for item in current}
    return sorted(
        {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    )


def safe_stop_after_current(
    *, current_file: str | None, invalidated: list[str], completed_files: list[str]
) -> dict[str, Any]:
    """Project a cooperative stop that never interrupts the current mux file."""

    completed = {item.casefold() for item in completed_files}
    affected = [item for item in invalidated if item.casefold() not in completed]
    return {
        "requested": bool(affected),
        "stop_after_current": str(current_file or "") if affected and current_file else None,
        "invalidated_files": affected,
        "next_action": "recheck_affected_files" if affected else "continue",
    }


def cleanup_preview(
    *,
    staging_directory: str | Path | None,
    source_directory: str | Path,
    formal_directories: list[str | Path],
    exclusive_source_directory: bool,
    shared_parent_directory: bool,
    delivery_confirmed: bool,
) -> dict[str, Any]:
    """Describe deletable roots; never return a formal destination as deletable."""

    source = Path(source_directory).resolve(strict=False)
    formal = [Path(item).resolve(strict=False) for item in formal_directories]
    staging = Path(staging_directory).resolve(strict=False) if staging_directory else None
    actions: list[dict[str, Any]] = []
    if staging is not None and not any(_overlaps(staging, item) for item in [source, *formal]):
        actions.append(
            {
                "kind": "staging",
                "path": str(staging),
                "allowed": True,
                "reason": "",
                **_directory_snapshot(staging),
            }
        )
    source_allowed = (
        delivery_confirmed
        and exclusive_source_directory
        and not shared_parent_directory
        and not any(_overlaps(source, item) for item in formal)
    )
    actions.append(
        {
            "kind": "source_directory",
            "path": str(source),
            "allowed": source_allowed,
            "reason": "" if source_allowed else "source directory is shared, unconfirmed, or not exclusively owned",
            **_directory_snapshot(source),
        }
    )
    result = {"status": "OK", "actions": actions, "formal_directories": [str(item) for item in formal]}
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**result, "preview_version": hashlib.sha256(canonical).hexdigest()}


def execute_cleanup(preview: dict[str, Any], selected_kinds: list[str]) -> dict[str, Any]:
    """Delete only explicitly selected, pre-authorized roots from a fresh preview."""

    selected = set(selected_kinds)
    allowed_kinds = {"staging", "source_directory"}
    if not selected.issubset(allowed_kinds):
        raise WorkflowError("ARCHIVE_CLEANUP_SELECTION_INVALID", "Cleanup selection is invalid")
    formal = {str(Path(item).resolve(strict=False)).casefold() for item in preview.get("formal_directories", [])}
    completed = []
    for action in preview.get("actions", []):
        if not isinstance(action, dict) or action.get("kind") not in selected:
            continue
        path = Path(str(action.get("path") or "")).resolve(strict=False)
        if not action.get("allowed"):
            raise WorkflowError("ARCHIVE_CLEANUP_NOT_ALLOWED", "Selected cleanup action is not authorized")
        if str(path).casefold() in formal:
            raise WorkflowError("ARCHIVE_FORMAL_MEDIA_DELETE_FORBIDDEN", "Formal media files can never be cleanup targets")
        if path.exists():
            if not path.is_dir():
                raise WorkflowError("ARCHIVE_CLEANUP_TARGET_INVALID", "Cleanup targets must be whole directories")
            shutil.rmtree(path)
        completed.append({"kind": action["kind"], "path": str(path), "status": "deleted"})
    return {"status": "COMPLETE", "completed": completed}
