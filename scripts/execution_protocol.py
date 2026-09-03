"""Versioned machine contract between Bangumi Media Hub and archive workflow."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from archive_rules import RULES_VERSION, is_under
from internal.hub_task_contract import (
    validate_remote_identity,
    validate_target_plan,
    validate_task_scope,
)


PROTOCOL_VERSION = "1.4"
COMPATIBLE_PROTOCOL_VERSIONS = ("1.3", PROTOCOL_VERSION)
PROTOCOL_SCHEMA_VERSION = 1
ENGINE_NAME = "archive-plex-anime"
ENGINE_VERSION = "2026-09-01-single-work-task-v1"
COMMANDS = (
    "capabilities",
    "recommend",
    "metadata_check",
    "metadata_preview",
    "initialize",
    "status",
    "approve_preflight",
    "approve_final",
    "mapping_preview",
    "cleanup_preview",
    "cleanup_execute",
    "run_step",
)
PATHLESS_COMMANDS = ("capabilities", "recommend", "metadata_check")
EVENT_STATUSES = (
    "accepted",
    "running",
    "needs_input",
    "succeeded",
    "warning",
    "failed",
    "skipped",
)
HUB_FINAL_SINKS = ("video", "subtitle_zip")
HUB_STEPS = ("inspect", "movie-audio", "subtitle", "remux", "package", "review", "finalize", "cleanup")
HUB_PRESETS = ("complete-archive", "replacement", "archive-only", "local-only")
COMMAND_PAYLOAD_FIELDS = {
    "capabilities": ("branch",),
    "recommend": ("branch", "has_storage", "has_subtitle_archive", "metadata_enabled"),
    "metadata_check": ("providers", "proxy", "language"),
    "metadata_preview": ("decisions", "local_seasons"),
    "initialize": (
        "preset",
        "capabilities",
        "decisions",
        "final_sinks",
        "task_scope",
        "remote_identity",
        "target_plan",
        "target_directory_suggestion",
    ),
    "status": (),
    "approve_preflight": ("decisions",),
    "approve_final": ("final_target",),
    "mapping_preview": ("strategy", "scope", "parameters", "decisions"),
    "cleanup_preview": (),
    "cleanup_execute": ("preview_version", "selected_kinds"),
    "run_step": ("step", "rerun"),
}
APPROVE_FINAL_TARGET_FIELDS = ("storage_id", "target_actions")
ISSUE_REQUIRED = ("code", "category", "retryable", "message", "details")
ARTIFACT_GROUPS = ("inputs", "staged", "final")
ARTIFACT_REQUIRED = ("artifact_id", "kind", "path", "state", "metadata")
ARTIFACT_STATES = ("available", "planned", "ready", "verified")
CHECKPOINT_REQUIRED = ("checkpoint_id", "stage", "status", "resumable", "details")
CHECKPOINT_STATUSES = ("in_progress", "completed", "interrupted")
EVENT_OPTIONAL = ("result",)
HUB_FORBIDDEN_KEYS = (
    "api_key",
    "apikey",
    "archive_tmdb_api_key",
    "archive_tmdb_token",
    "archive_tvdb_api_key",
    "archive_tvdb_pin",
    "auth_token",
    "bearer_token",
    "credential",
    "credentials",
    "kdocs",
    "kdocs_config",
    "kdocs_path",
    "kdocs_tracker",
    "password",
    "pin",
    "secret",
    "spreadsheet_id",
    "tmdb_api_key",
    "tmdb_token",
    "token",
    "tracker_column",
    "tracker_file",
    "tracker_path",
    "tracker_sheet",
    "tvdb_api_key",
    "tvdb_pin",
    "worksheet",
    "worksheet_id",
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProtocolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "validation",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.details = details or {}

    def issue(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "message": str(self),
            "details": self.details,
        }


def _contract() -> dict[str, Any]:
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "compatible_protocol_versions": list(COMPATIBLE_PROTOCOL_VERSIONS),
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "rules_version": RULES_VERSION,
        "transport": {
            "request": "single UTF-8 JSON object on stdin",
            "events": "UTF-8 NDJSON on stdout",
            "diagnostics": "stderr only; never parsed as protocol data",
        },
        "commands": list(COMMANDS),
        "pathless_commands": list(PATHLESS_COMMANDS),
        "event_statuses": list(EVENT_STATUSES),
        "hub_final_sinks": list(HUB_FINAL_SINKS),
        "hub_steps": list(HUB_STEPS),
        "command_payload_fields": {
            command: list(fields) for command, fields in COMMAND_PAYLOAD_FIELDS.items()
        },
        "approve_final_target_fields": list(APPROVE_FINAL_TARGET_FIELDS),
        "hub_forbidden_keys": list(HUB_FORBIDDEN_KEYS),
        "request_required": [
            "protocol_version",
            "expected_rules_version",
            "task_id",
            "run_id",
            "command_id",
            "command",
            "payload",
        ],
        "event_required": [
            "protocol_version",
            "engine_version",
            "rules_version",
            "task_id",
            "run_id",
            "command_id",
            "sequence",
            "stage",
            "status",
            "occurred_at",
            "message",
            "issues",
            "artifacts",
            "checkpoints",
            "progress",
            "next_action",
        ],
        "event_optional": list(EVENT_OPTIONAL),
        "path_snapshot_required": [
            "snapshot_id",
            "mode",
            "branch",
            "work_root",
            "task_relative_path",
            "storage_roots",
            "subtitle_roots",
        ],
        "path_snapshot_optional": ["staging_root"],
        "issue_required": list(ISSUE_REQUIRED),
        "artifact_groups": list(ARTIFACT_GROUPS),
        "artifact_required": list(ARTIFACT_REQUIRED),
        "artifact_states": list(ARTIFACT_STATES),
        "checkpoint_required": list(CHECKPOINT_REQUIRED),
        "checkpoint_statuses": list(CHECKPOINT_STATUSES),
        "progress_required": ["stage", "completed_items", "total_items", "current_item"],
        "progress_optional": [
            "reused_items",
            "remaining_items",
            "remaining_bytes",
            "available_bytes",
            "action",
        ],
        "next_action_required": ["id", "label", "enabled", "reason"],
        "idempotency": "same command_id and request replays the original event sequence; changed request is rejected",
        "cooperative_control": {
            "stop_after_current_file": "control/stop-after-current",
            "semantics": "the active file is checkpointed before the command stops and affected files are rechecked",
        },
    }


def protocol_descriptor() -> dict[str, Any]:
    contract = _contract()
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**contract, "source_version": hashlib.sha256(canonical).hexdigest()}


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("PROTOCOL_REQUEST_INVALID", f"{field} must be an object", details={"field": field})
    return value


def _identifier(value: Any, field: str) -> str:
    text = str(value or "")
    if not _IDENTIFIER.fullmatch(text):
        raise ProtocolError("PROTOCOL_REQUEST_INVALID", f"{field} is invalid", details={"field": field})
    return text


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def is_hub_forbidden_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    return normalized in set(HUB_FORBIDDEN_KEYS) or normalized.startswith("kdocs_")


def reject_hub_forbidden_fields(value: Any, *, field: str = "payload") -> None:
    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if is_hub_forbidden_key(key):
                    raise ProtocolError(
                        "PROTOCOL_HUB_CAPABILITY_FORBIDDEN",
                        "Hub requests must not contain credentials or KDocs configuration",
                        details={"field": f"{path}.{key}"},
                    )
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, field)


def empty_artifacts() -> dict[str, list[dict[str, Any]]]:
    return {group: [] for group in ARTIFACT_GROUPS}


def validate_artifacts(value: Any) -> dict[str, list[dict[str, Any]]]:
    artifacts = _object(value, "artifacts")
    if set(artifacts) != set(ARTIFACT_GROUPS):
        raise ProtocolError("PROTOCOL_EVENT_INVALID", "artifact groups do not match the contract")
    required = set(ARTIFACT_REQUIRED)
    for group in ARTIFACT_GROUPS:
        items = artifacts[group]
        if not isinstance(items, list):
            raise ProtocolError("PROTOCOL_EVENT_INVALID", f"artifacts.{group} must be an array")
        for item in items:
            if not isinstance(item, dict) or set(item) != required:
                raise ProtocolError("PROTOCOL_EVENT_INVALID", f"artifacts.{group} item is invalid")
            _identifier(item.get("artifact_id"), f"artifacts.{group}.artifact_id")
            if not isinstance(item.get("kind"), str) or not item["kind"]:
                raise ProtocolError("PROTOCOL_EVENT_INVALID", f"artifacts.{group}.kind is invalid")
            if not isinstance(item.get("path"), str) or not item["path"]:
                raise ProtocolError("PROTOCOL_EVENT_INVALID", f"artifacts.{group}.path is invalid")
            if item.get("state") not in ARTIFACT_STATES or not isinstance(item.get("metadata"), dict):
                raise ProtocolError("PROTOCOL_EVENT_INVALID", f"artifacts.{group} state or metadata is invalid")
    return artifacts


def validate_checkpoints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProtocolError("PROTOCOL_EVENT_INVALID", "checkpoints must be an array")
    required = set(CHECKPOINT_REQUIRED)
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ProtocolError("PROTOCOL_EVENT_INVALID", "checkpoint item is invalid")
        _identifier(item.get("checkpoint_id"), "checkpoint.checkpoint_id")
        if not isinstance(item.get("stage"), str) or not item["stage"]:
            raise ProtocolError("PROTOCOL_EVENT_INVALID", "checkpoint stage is invalid")
        if item.get("status") not in CHECKPOINT_STATUSES or type(item.get("resumable")) is not bool:
            raise ProtocolError("PROTOCOL_EVENT_INVALID", "checkpoint status is invalid")
        if not isinstance(item.get("details"), dict):
            raise ProtocolError("PROTOCOL_EVENT_INVALID", "checkpoint details must be an object")
    return value


def _absolute_directory(value: Any, field: str) -> Path:
    text = str(value or "")
    path = Path(text).expanduser()
    if not text or not path.is_absolute():
        raise ProtocolError("PROTOCOL_PATH_INVALID", f"{field} must be an absolute path", details={"field": field})
    return path.resolve(strict=False)


def validate_path_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _object(value, "path_snapshot")
    required = set(protocol_descriptor()["path_snapshot_required"])
    optional = set(protocol_descriptor().get("path_snapshot_optional", []))
    missing = sorted(required - set(snapshot))
    allowed = required | optional
    extra = sorted(set(snapshot) - allowed)
    if missing or extra:
        raise ProtocolError(
            "PROTOCOL_PATH_INVALID",
            "path_snapshot fields do not match the contract",
            details={"missing": missing, "extra": extra},
        )
    _identifier(snapshot.get("snapshot_id"), "path_snapshot.snapshot_id")
    if snapshot.get("mode") not in {"mounted", "native"}:
        raise ProtocolError("PROTOCOL_PATH_INVALID", "path_snapshot.mode is invalid")
    if snapshot.get("branch") not in {"tv", "movie"}:
        raise ProtocolError("PROTOCOL_PATH_INVALID", "path_snapshot.branch is invalid")
    work_root = _absolute_directory(snapshot.get("work_root"), "path_snapshot.work_root")
    relative_text = str(snapshot.get("task_relative_path") or "").replace("\\", "/")
    relative = PurePosixPath(relative_text)
    if (
        not relative_text
        or relative.is_absolute()
        or relative_text in {".", ".."}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ProtocolError("PROTOCOL_PATH_INVALID", "task_relative_path must identify one task below work_root")
    work_dir = (work_root / Path(*relative.parts)).resolve(strict=False)
    if work_dir == work_root or not is_under(work_dir, work_root):
        raise ProtocolError("PROTOCOL_PATH_INVALID", "task directory escapes work_root")
    for field in ("storage_roots", "subtitle_roots"):
        roots = _object(snapshot.get(field), f"path_snapshot.{field}")
        for key, path in roots.items():
            _identifier(key, f"path_snapshot.{field} key")
            target = _absolute_directory(path, f"path_snapshot.{field}.{key}")
            if is_under(target, work_root) or is_under(work_root, target):
                raise ProtocolError("PROTOCOL_PATH_INVALID", "formal targets must be separate from work_root")
    staging_text = str(snapshot.get("staging_root") or "").strip()
    if staging_text:
        staging_root = _absolute_directory(staging_text, "path_snapshot.staging_root")
        if is_under(staging_root, work_root) or is_under(work_root, staging_root):
            raise ProtocolError("PROTOCOL_PATH_INVALID", "staging_root must be separate from work_root")
        for field in ("storage_roots", "subtitle_roots"):
            for value in snapshot[field].values():
                target = Path(str(value)).resolve(strict=False)
                if is_under(staging_root, target) or is_under(target, staging_root):
                    raise ProtocolError("PROTOCOL_PATH_INVALID", "staging_root must be separate from formal targets")
    if set(snapshot["storage_roots"]) - {"storage_1", "storage_2", "storage_3"}:
        raise ProtocolError("PROTOCOL_PATH_INVALID", "storage_roots contains an unsupported slot")
    if set(snapshot["subtitle_roots"]) - {snapshot["branch"]}:
        raise ProtocolError("PROTOCOL_PATH_INVALID", "subtitle_roots does not match the selected branch")
    return {
        **snapshot,
        "work_root": str(work_root),
        "task_relative_path": relative.as_posix(),
        "staging_root": staging_text,
    }


def resolve_work_dir(snapshot: dict[str, Any], *, require_exists: bool = True) -> Path:
    validated = validate_path_snapshot(snapshot)
    root = Path(validated["work_root"])
    work = (root / Path(*PurePosixPath(validated["task_relative_path"]).parts)).resolve(strict=False)
    if require_exists and not work.is_dir():
        raise ProtocolError(
            "PROTOCOL_TASK_DIRECTORY_MISSING",
            "task directory does not exist",
            details={"task_relative_path": validated["task_relative_path"]},
        )
    return work


def validate_request(value: Any) -> dict[str, Any]:
    request = _object(value, "request")
    required = set(protocol_descriptor()["request_required"])
    allowed = required | {"path_snapshot"}
    missing = sorted(required - set(request))
    extra = sorted(set(request) - allowed)
    if missing or extra:
        raise ProtocolError(
            "PROTOCOL_REQUEST_INVALID",
            "request fields do not match the contract",
            details={"missing": missing, "extra": extra},
        )
    if request.get("protocol_version") not in COMPATIBLE_PROTOCOL_VERSIONS:
        raise ProtocolError(
            "PROTOCOL_VERSION_UNSUPPORTED",
            "protocol_version is not supported",
            details={"supported": list(COMPATIBLE_PROTOCOL_VERSIONS), "received": request.get("protocol_version")},
        )
    if request.get("expected_rules_version") != RULES_VERSION:
        raise ProtocolError(
            "RULES_VERSION_MISMATCH",
            "archive rules version does not match the request",
            details={"supported": RULES_VERSION, "received": request.get("expected_rules_version")},
        )
    for field in ("task_id", "run_id", "command_id"):
        _identifier(request.get(field), field)
    command = str(request.get("command") or "")
    if command not in COMMANDS:
        raise ProtocolError("PROTOCOL_COMMAND_UNSUPPORTED", "command is not supported", details={"command": command})
    payload = _object(request.get("payload"), "payload")
    reject_hub_forbidden_fields(payload)
    payload_extra = sorted(set(payload) - set(COMMAND_PAYLOAD_FIELDS[command]))
    if payload_extra:
        raise ProtocolError(
            "PROTOCOL_REQUEST_INVALID",
            "payload contains unsupported fields",
            details={"command": command, "extra": payload_extra},
        )
    snapshot = request.get("path_snapshot")
    if command in PATHLESS_COMMANDS:
        if snapshot is not None:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", f"{command} must not include path_snapshot")
        branch = payload.get("branch")
        if command in {"capabilities", "recommend"} and branch is not None and branch not in {"tv", "movie"}:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.branch is invalid")
    else:
        validate_path_snapshot(snapshot)
    sinks = payload.get("final_sinks")
    if sinks is not None:
        if not isinstance(sinks, list) or any(item not in HUB_FINAL_SINKS for item in sinks) or len(sinks) != len(set(sinks)):
            raise ProtocolError("PROTOCOL_FINAL_SINK_INVALID", "Hub final_sinks may only contain video and subtitle_zip")
    if command == "run_step":
        if payload.get("step") not in HUB_STEPS:
            raise ProtocolError("PROTOCOL_STEP_INVALID", "payload.step is not supported")
        if "rerun" in payload and type(payload["rerun"]) is not bool:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.rerun must be boolean")
    if command == "metadata_check":
        providers = payload.get("providers", ["tmdb", "tvdb"])
        if (
            not isinstance(providers, list)
            or not providers
            or any(item not in {"tmdb", "tvdb"} for item in providers)
            or len(providers) != len(set(providers))
        ):
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.providers is invalid")
    if command == "metadata_preview":
        _object(payload.get("decisions", {}), "payload.decisions")
        local_seasons = payload.get("local_seasons", [])
        if (
            not isinstance(local_seasons, list)
            or any(type(item) is not int or item < 0 or item > 99 for item in local_seasons)
            or len(local_seasons) != len(set(local_seasons))
        ):
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.local_seasons is invalid")
    if command == "mapping_preview":
        if payload.get("strategy") not in {"filename", "order", "offset"}:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.strategy is invalid")
        if payload.get("scope", "all") not in {"all", "videos", "subtitles"}:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.scope is invalid")
        _object(payload.get("parameters", {}), "payload.parameters")
        _object(payload.get("decisions", {}), "payload.decisions")
    if command == "cleanup_execute":
        preview_version = str(payload.get("preview_version") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", preview_version):
            raise ProtocolError("PROTOCOL_CLEANUP_PREVIEW_INVALID", "preview_version must be a SHA-256 digest")
        selected = payload.get("selected_kinds")
        if (
            not isinstance(selected, list)
            or not selected
            or any(item not in {"staging", "source_directory"} for item in selected)
            or len(selected) != len(set(selected))
        ):
            raise ProtocolError("PROTOCOL_CLEANUP_SELECTION_INVALID", "selected_kinds is invalid")
    if command == "initialize":
        preset = payload.get("preset", "complete-archive")
        if preset not in HUB_PRESETS:
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.preset is invalid")
        capabilities = payload.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item for item in capabilities):
                raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.capabilities must be an array of names")
            if "kdocs-tracker" in capabilities:
                raise ProtocolError("PROTOCOL_HUB_CAPABILITY_FORBIDDEN", "Hub requests must not contain KDocs")
        _object(payload.get("decisions", {}), "payload.decisions")
        task_scope = payload.get("task_scope")
        if task_scope is not None:
            checked_scope = validate_task_scope(task_scope)
            remote_identity = payload.get("remote_identity")
            if remote_identity is not None:
                validate_remote_identity(remote_identity, branch=request["path_snapshot"]["branch"])
            target_plan = payload.get("target_plan")
            if target_plan is not None:
                validate_target_plan(target_plan, checked_scope["files"])
        elif any(key in payload for key in ("remote_identity", "target_plan", "target_directory_suggestion")):
            raise ProtocolError(
                "ARCHIVE_TASK_SCOPE_REQUIRED",
                "task_scope is required when the single-work task contract is used",
                category="decision",
            )
    if command == "approve_preflight":
        _object(payload.get("decisions", {}), "payload.decisions")
    if command == "approve_final":
        target = _object(payload.get("final_target", {}), "payload.final_target")
        allowed_target = set(APPROVE_FINAL_TARGET_FIELDS)
        target_extra = sorted(set(target) - allowed_target)
        if target_extra:
            code = (
                "PROTOCOL_HUB_CAPABILITY_FORBIDDEN"
                if {"tracker_column", "kdocs", "kdocs_tracker"} & set(target_extra)
                else "PROTOCOL_REQUEST_INVALID"
            )
            raise ProtocolError(code, "payload.final_target contains unsupported fields", details={"extra": target_extra})
        if target.get("storage_id") is not None and target["storage_id"] not in request["path_snapshot"]["storage_roots"]:
            raise ProtocolError("PROTOCOL_FINAL_TARGET_INVALID", "storage_id is not available in the path snapshot")
        actions = target.get("target_actions")
        if actions is not None and (
            not isinstance(actions, dict)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in actions.items()
            )
        ):
            raise ProtocolError("PROTOCOL_REQUEST_INVALID", "payload.final_target.target_actions is invalid")
    return request


def normalized_status(status: Any) -> str:
    value = str(status or "FAILED").upper()
    return {
        "OK": "succeeded",
        "COMPLETE": "succeeded",
        "SUCCESS": "succeeded",
        "NEEDS_USER": "needs_input",
        "DECISION_REQUIRED": "needs_input",
        "WARNING": "warning",
        "SKIPPED": "skipped",
        "FAILED": "failed",
    }.get(value, "failed")


def event(
    request: dict[str, Any],
    *,
    sequence: int,
    stage: str,
    status: str,
    message: str,
    issues: list[dict[str, Any]] | None = None,
    artifacts: dict[str, Any] | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
    progress: dict[str, Any] | None = None,
    next_action: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in EVENT_STATUSES:
        raise ProtocolError("PROTOCOL_EVENT_INVALID", "event status is invalid")
    checked_artifacts = validate_artifacts(artifacts if artifacts is not None else empty_artifacts())
    checked_checkpoints = validate_checkpoints(checkpoints or [])
    checked_progress = progress or {
        "stage": stage,
        "completed_items": 1 if status in {"succeeded", "warning", "skipped"} else 0,
        "total_items": 1,
        "current_item": "" if status in {"succeeded", "warning", "skipped", "failed", "needs_input"} else stage,
    }
    progress_required = {"stage", "completed_items", "total_items", "current_item"}
    progress_optional = {
        "reused_items",
        "remaining_items",
        "remaining_bytes",
        "available_bytes",
        "action",
    }
    if not progress_required.issubset(checked_progress) or set(checked_progress) - progress_required - progress_optional:
        raise ProtocolError("PROTOCOL_EVENT_INVALID", "progress fields do not match the contract")
    checked_action = next_action or {
        "id": "none",
        "label": "等待当前步骤完成" if status in {"accepted", "running"} else "查看结果",
        "enabled": status not in {"accepted", "running"},
        "reason": "",
    }
    if set(checked_action) != {"id", "label", "enabled", "reason"}:
        raise ProtocolError("PROTOCOL_EVENT_INVALID", "next_action fields do not match the contract")
    value = {
        "protocol_version": PROTOCOL_VERSION,
        "engine_version": ENGINE_VERSION,
        "rules_version": RULES_VERSION,
        "task_id": str(request.get("task_id") or "invalid"),
        "run_id": str(request.get("run_id") or "invalid"),
        "command_id": str(request.get("command_id") or "invalid"),
        "sequence": sequence,
        "stage": stage,
        "status": status,
        "occurred_at": datetime.now(UTC).isoformat(),
        "message": message,
        "issues": issues or [],
        "artifacts": checked_artifacts,
        "checkpoints": checked_checkpoints,
        "progress": checked_progress,
        "next_action": checked_action,
    }
    if result is not None:
        value["result"] = result
    return value


def request_digest(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
