"""NDJSON adapter for Bangumi Media Hub; media rules remain in workflow.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from argparse import Namespace
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import workflow
from archive_rules import RULES_VERSION, backend_cache_path, task_output_root, temporary_path
from common import WorkflowIssue, configure_utf8_stdio, load_config, read_json, write_json_atomic
from execution_protocol import (
    ProtocolError,
    PROTOCOL_VERSION,
    empty_artifacts,
    event,
    is_hub_forbidden_key,
    normalized_status,
    protocol_descriptor,
    request_digest,
    resolve_work_dir,
    validate_request,
)
from internal.metadata_client import (
    JsonHttpClient,
    MetadataHttpError,
    TmdbClient,
    TvdbClient,
    credential_presence,
)
from internal.metadata_match import inspect_metadata
from internal.hub_task_contract import cleanup_preview, execute_cleanup
from toolchain import ToolchainError
from tv_plan import season_number, source_episode


_ACTIVE_PROGRESS: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "archive_hub_progress",
    default=None,
)


def _reviewed_video_root(state: dict[str, Any], storage_root: str | None) -> str:
    """Return the exact reviewed work target after checking its selected storage boundary."""

    final_target = state.get("final_target") if isinstance(state.get("final_target"), dict) else {}
    reviewed = str(final_target.get("video_root") or "").strip()
    if not storage_root or not reviewed:
        raise ProtocolError(
            "PROTOCOL_FINAL_TARGET_INVALID",
            "reviewed video target or selected storage root is missing",
        )
    selected_path = Path(storage_root).resolve(strict=False)
    reviewed_path = Path(reviewed).resolve(strict=False)
    try:
        reviewed_path.relative_to(selected_path)
    except ValueError as exc:
        raise ProtocolError(
            "PROTOCOL_FINAL_TARGET_INVALID",
            "reviewed video target is outside the selected storage root",
        ) from exc
    return reviewed


def _arguments(request: dict[str, Any], work: Path | None) -> Namespace:
    payload = request["payload"]
    command = request["command"]
    common = {"work_dir": str(work) if work is not None else None}
    if command == "initialize":
        capabilities = payload.get("capabilities")
        decisions = dict(payload.get("decisions", {}))
        task_contract = {
            key: payload[key]
            for key in (
                "task_scope",
                "remote_identity",
                "target_plan",
                "target_directory_suggestion",
            )
            if key in payload
        }
        if task_contract:
            decisions["hub_task_contract"] = task_contract
        return Namespace(
            **common,
            branch=request["path_snapshot"]["branch"],
            task=payload.get("preset", "complete-archive"),
            steps=None,
            capabilities=",".join(capabilities) if isinstance(capabilities, list) else None,
            entrypoint="hub",
            decisions_stdin=False,
            decisions_payload=decisions,
        )
    if command == "approve_preflight":
        return Namespace(
            **common,
            kind="preflight",
            decisions_stdin=False,
            decisions_payload=payload.get("decisions", {}),
        )
    if command == "approve_final":
        raise ProtocolError("PROTOCOL_REQUEST_INVALID", "approve_final arguments require the current task state")
    if command == "run_step":
        return Namespace(**common, command=payload.get("step"), rerun=bool(payload.get("rerun", False)))
    return Namespace(**common)


def _workflow_options(branch: str | None, *, recommended: str = "replacement") -> dict[str, Any]:
    catalog = workflow.capability_catalog("hub", branch)
    return {
        **catalog,
        "recommended_scenario_id": recommended,
        "recommendation_reason": (
            "没有可用的正式存储位置，建议只生成本地产物。"
            if recommended == "local-only"
            else "已连接正式存储位置，建议先生成并验收洗版产物。"
        ),
    }


def _recommend(payload: dict[str, Any]) -> dict[str, Any]:
    has_storage = bool(payload.get("has_storage"))
    recommended = "replacement" if has_storage else "local-only"
    options = _workflow_options(payload.get("branch"), recommended=recommended)
    return {
        "status": "OK",
        "workflow_options": options,
        "metadata": {
            "enabled": bool(payload.get("metadata_enabled")),
            "capability_id": "metadata",
        },
        "subtitle_archive": {
            "available": bool(payload.get("has_subtitle_archive")),
            "fallback": "local-package",
        },
    }


def _metadata_check(payload: dict[str, Any]) -> dict[str, Any]:
    providers = payload.get("providers") or ["tmdb", "tvdb"]
    proxy = str(payload.get("proxy") or "").strip()
    language = str(payload.get("language") or "zh-CN")
    presence = credential_presence()
    results: dict[str, dict[str, Any]] = {}
    for provider in providers:
        configured = bool(presence.get(provider))
        if not configured:
            results[provider] = {"status": "not_configured", "configured": False, "code": ""}
            continue
        try:
            http = JsonHttpClient(proxy=proxy or None, timeout=10, retries=0)
            if provider == "tmdb":
                TmdbClient(http).search("tv", "One Piece", language=language)
            else:
                TvdbClient(http).search("One Piece", "tv")
            results[provider] = {"status": "ready", "configured": True, "code": ""}
        except MetadataHttpError as exc:
            if exc.code.endswith("AUTH_FAILED"):
                status = "auth_failed"
            elif exc.code == "METADATA_NETWORK_UNAVAILABLE":
                status = "proxy_failed" if proxy else "network_failed"
            else:
                status = "network_failed"
            results[provider] = {"status": status, "configured": True, "code": exc.code}
    return {"status": "OK", "providers": results}


def _relative_path(item: dict[str, Any], work: Path) -> str:
    file_value = item.get("file") if isinstance(item.get("file"), dict) else {}
    raw = str(file_value.get("relativePath") or file_value.get("path") or item.get("path") or "")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.relative_to(work).as_posix()
        except ValueError:
            return path.as_posix()
    return raw.replace("\\", "/")


def _media_rows(work: Path, discovery: dict[str, Any], decisions: dict[str, Any]) -> list[dict[str, Any]]:
    videos = [item for item in discovery.get("videos", []) if isinstance(item, dict)]
    subtitles = [item for item in discovery.get("subtitles", []) if isinstance(item, dict)]
    explicit = decisions.get("episode_map") if isinstance(decisions.get("episode_map"), dict) else {}
    rows: dict[str, dict[str, Any]] = {}

    def identity(item: dict[str, Any]) -> tuple[int | None, int | None, str]:
        relative = _relative_path(item, work)
        mapped = str(explicit.get(relative) or "")
        matched = re.fullmatch(r"S(\d{2})E(\d{2,3})", mapped, re.IGNORECASE)
        path_value = item.get("file", {}).get("path") if isinstance(item.get("file"), dict) else item.get("path")
        path = Path(str(path_value or relative))
        if matched:
            return int(matched.group(1)), int(matched.group(2)), relative
        episode = source_episode(path)
        season = season_number(path, work, decisions) if episode is not None else None
        return season, episode, relative

    def row_for(season: int | None, episode: int | None, relative: str) -> dict[str, Any]:
        row_id = f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else f"unmatched:{relative}"
        return rows.setdefault(
            row_id,
            {
                "row_id": row_id,
                "season": season,
                "episode": episode,
                "episode_label": row_id if not row_id.startswith("unmatched:") else "未对应",
                "videos": [],
                "subtitles": [],
                "audio_tracks": [],
                "font_status": "unknown",
                "match_status": "matched" if episode is not None else "unmatched",
                "issues": [],
            },
        )

    for item in videos:
        season, episode, relative = identity(item)
        row = row_for(season, episode, relative)
        row["videos"].append({"path": relative, "status": item.get("status", "")})
        for track in item.get("tracks", []):
            if isinstance(track, dict) and "audio" in str(track.get("type") or track.get("@type") or "").casefold():
                row["audio_tracks"].append(
                    {
                        "id": track.get("id") or track.get("streamorder"),
                        "language": track.get("language") or track.get("Language") or "",
                        "codec": track.get("codec") or track.get("Format") or "",
                        "title": track.get("title") or track.get("Title") or "",
                    }
                )
    for item in subtitles:
        season, episode, relative = identity(item)
        row = row_for(season, episode, relative)
        row["subtitles"].append({"path": relative, "group": item.get("group", ""), "status": item.get("status", "")})
        if episode is None:
            row["issues"].append({"code": "SUBTITLE_EPISODE_UNMATCHED", "path": relative})
    missing_fonts = discovery.get("missingFonts", [])
    for row in rows.values():
        row["font_status"] = "missing" if missing_fonts else "ready"
        if not row["videos"]:
            row["match_status"] = "unmatched"
            row["issues"].append({"code": "VIDEO_EPISODE_UNMATCHED"})
    return sorted(
        rows.values(),
        key=lambda item: (
            item["season"] is None,
            item["season"] if item["season"] is not None else 999,
            item["episode"] if item["episode"] is not None else 9999,
            item["row_id"],
        ),
    )


def _decision_requests(analysis: dict[str, Any], output: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = analysis.get("metadata") if isinstance(analysis.get("metadata"), dict) else {}
    preflight = output.get("preflight") if isinstance(output.get("preflight"), dict) else {}
    issues = []
    for candidate in (output.get("issues"), preflight.get("issues"), metadata.get("issues")):
        if isinstance(candidate, list):
            issues.extend(item for item in candidate if isinstance(item, dict))
    labels = {
        "RELEASE_GROUP_REQUIRED": ("确认压制组", "text", "release_group"),
        "SUBTITLE_ARCHIVE_ROOT_REQUIRED": ("选择字幕 ZIP 去向", "choice", "subtitle_archive_mode"),
        "METADATA_QUERY_REQUIRED": ("确认元数据搜索词", "text", "metadata.query"),
        "METADATA_CANDIDATE_REQUIRED": ("选择匹配的作品", "choice", "metadata.tmdb_id"),
        "TVDB_ID_REQUIRED": ("填写 TVDB ID", "number", "metadata.tvdb_id"),
        "SUBTITLE_EPISODE_UNMATCHED": ("修正字幕对应", "mapping", "episode_map"),
        "REPLACEMENT_TARGET_REQUIRED": ("选择现有作品位置", "directory", "library_target"),
        "MANUAL_REPLACEMENT_TARGET_INVALID": ("重新选择现有作品位置", "directory", "library_target"),
        "MANUAL_REPLACEMENT_TARGET_MISSING": ("重新选择现有作品位置", "directory", "library_target"),
        "STAGING_OUTPUT_REQUIRED": ("选择任务输出位置", "directory", "staging"),
        "CAPABILITY_UNAVAILABLE": ("恢复所需功能", "notice", ""),
    }
    result = []
    seen: set[str] = set()
    for index, issue in enumerate(issues, start=1):
        code = str(issue.get("code") or "ARCHIVE_DECISION_REQUIRED")
        capability = str(issue.get("capability") or "")
        if code == "CAPABILITY_UNAVAILABLE" and capability == "metadata":
            metadata_mode = str(metadata.get("mode") or "auto")
            if metadata_mode != "required":
                # Automatic metadata lookup is best effort. A temporary network or
                # proxy failure must not masquerade as a user decision or block the
                # local preflight workflow.
                continue
            issue = {
                **issue,
                "message": "已选择必须获取在线剧集信息；请检查元数据连接，或改用自动/离线模式。",
            }
        if code in seen and code != "SUBTITLE_EPISODE_UNMATCHED":
            continue
        seen.add(code)
        label, kind, field = labels.get(code, ("还有一项信息需要确认", "notice", ""))
        if code == "CAPABILITY_UNAVAILABLE" and capability == "metadata":
            label, kind, field = "恢复在线剧集信息", "notice", "metadata.mode"
        result.append(
            {
                "id": f"{code.lower()}-{index}",
                "code": code,
                "label": label,
                "kind": kind,
                "field": field,
                "required": True,
                "details": issue,
            }
        )
    return result


def _mapping_preview(work: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        raise ProtocolError("MAPPING_PREVIEW_REQUIRES_PREFLIGHT", "mapping preview requires an existing preflight")
    manifest = read_json(manifest_path)
    discovery = manifest.get("discovery", {}) if isinstance(manifest, dict) else {}
    decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
    scope = payload.get("scope", "all")
    strategy = payload.get("strategy")
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    selected: list[tuple[str, Path]] = []
    if scope in {"all", "videos"}:
        selected.extend(("video", Path(str(item.get("file", {}).get("path") or ""))) for item in discovery.get("videos", []) if isinstance(item, dict))
    if scope in {"all", "subtitles"}:
        selected.extend(("subtitle", Path(str(item.get("file", {}).get("path") or ""))) for item in discovery.get("subtitles", []) if isinstance(item, dict))
    selected = [(kind, path) for kind, path in selected if str(path)]
    patch: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    offset = int(parameters.get("offset") or 0)
    start = max(1, int(parameters.get("start_episode") or 1))
    order_indexes = {"video": 0, "subtitle": 0}
    for kind, path in sorted(selected, key=lambda item: (item[0], item[1].as_posix().casefold())):
        if strategy in {"filename", "offset"}:
            episode = source_episode(path)
        else:
            episode = start + order_indexes[kind]
            order_indexes[kind] += 1
        if episode is None:
            conflicts.append({"path": path.name, "code": "EPISODE_NUMBER_REQUIRED", "kind": kind})
            continue
        season = int(parameters.get("season") or season_number(path, work, decisions))
        target = episode + offset
        if target < 1:
            conflicts.append({"path": path.name, "code": "EPISODE_TARGET_INVALID", "kind": kind})
            continue
        try:
            relative = path.relative_to(work).as_posix()
        except ValueError:
            relative = path.as_posix()
        patch[relative] = f"S{season:02d}E{target:02d}"
    return {
        "status": "OK",
        "strategy": strategy,
        "scope": scope,
        "episode_map_patch": patch,
        "changes": [{"path": path, "target": target} for path, target in patch.items()],
        "conflicts": conflicts,
    }


def _inspect_analysis(work: Path, output: dict[str, Any]) -> dict[str, Any]:
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        return {"summary": output.get("summary", {}), "route": output.get("route", {})}
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        return {"summary": output.get("summary", {}), "route": output.get("route", {})}
    discovery = manifest.get("discovery") if isinstance(manifest, dict) else {}
    if not isinstance(discovery, dict):
        discovery = {}
    analysis = {
        "summary": output.get("summary", {}),
        "route": output.get("route", {}),
        "videos": discovery.get("videos", []),
        "subtitles": discovery.get("subtitles", []),
        "font_requirements": discovery.get("fontRequirements", []),
        "font_availability": discovery.get("fontAvailability", []),
        "missing_fonts": discovery.get("missingFonts", []),
        "embedded_subtitles": discovery.get("embeddedSubtitles", {}),
        "movie_audio": discovery.get("movieAudioPreflights", []),
        "library_target": discovery.get("libraryTarget"),
        "metadata": discovery.get("metadata", {}),
    }
    state = _workflow_state(work, output)
    decisions = state.get("decisions") if isinstance(state.get("decisions"), dict) else {}
    analysis["media_rows"] = _media_rows(work, discovery, decisions)
    analysis["metadata_evidence"] = {
        key: analysis["metadata"].get(key)
        for key in (
            "status",
            "selected",
            "query",
            "querySource",
            "tmdb",
            "tvdb",
            "episodes",
            "episodeOrder",
            "suggestedDecisions",
        )
        if isinstance(analysis["metadata"], dict) and key in analysis["metadata"]
    }
    analysis["decision_requests"] = _decision_requests(analysis, output)
    request_labels = [str(item.get("label") or "") for item in analysis["decision_requests"]]
    analysis["next_action"] = {
        "id": "repreflight" if analysis["decision_requests"] else "confirm_preflight",
        "label": "查看还要完成的内容" if analysis["decision_requests"] else "确认并生成暂存产物",
        "enabled": True,
        "reason": (
            f"请先完成：{'、'.join(request_labels[:3])}"
            if analysis["decision_requests"]
            else ""
        ),
    }
    return analysis


def _review_analysis(output: dict[str, Any]) -> dict[str, Any]:
    result = output if isinstance(output, dict) else {}
    return {
        **result,
        "review_items": [
            {"id": "mediainfo", "label": "MediaInfo 前后对比", "required": True, "evidence": result.get("mediaInfo") or result.get("mediainfo") or {}},
            {"id": "subtitles", "label": "字幕与字体检查", "required": True, "evidence": result.get("subtitles") or {}},
            {"id": "audio", "label": "音轨检查", "required": True, "evidence": result.get("audio") or {}},
            {"id": "outputs", "label": "输出文件检查", "required": True, "evidence": result.get("artifacts") or result.get("summary") or {}},
            {"id": "target", "label": "正式写入目标", "required": True, "evidence": result.get("final_target") or {}},
        ],
    }


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    command = request["command"]
    payload = request["payload"]
    if command == "capabilities":
        return {"status": "OK", "workflow_options": _workflow_options(payload.get("branch"))}
    if command == "recommend":
        return _recommend(payload)
    if command == "metadata_check":
        return _metadata_check(payload)
    work = resolve_work_dir(request["path_snapshot"])
    if command == "metadata_preview":
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
        metadata = decisions.get("metadata") if isinstance(decisions.get("metadata"), dict) else {}
        branch = request["path_snapshot"]["branch"]
        result = inspect_metadata(
            work,
            load_config(),
            {"branch": "anime" if branch == "tv" else "movie"},
            [],
            metadata,
            local_season_numbers=payload.get("local_seasons") or None,
        )
        return {
            "status": "OK",
            "metadata": _sanitize_hub_value(result),
            "read_only": True,
            "media_scanned": False,
            "task_state_changed": False,
        }
    if command == "mapping_preview":
        return _mapping_preview(work, payload)
    if command in {"cleanup_preview", "cleanup_execute"}:
        state = workflow.load_task_state(work)
        decisions = state.get("decisions") if isinstance(state.get("decisions"), dict) else {}
        contract = decisions.get("hub_task_contract") if isinstance(decisions.get("hub_task_contract"), dict) else {}
        scope = contract.get("task_scope") if isinstance(contract.get("task_scope"), dict) else {}
        source_relative = str(scope.get("source_relative_path") or ".").replace("\\", "/")
        source_root = Path(request["path_snapshot"]["work_root"]).resolve(strict=False)
        source = (
            work
            if source_relative == "."
            else (source_root / Path(*source_relative.split("/"))).resolve(strict=False)
        )
        try:
            source.relative_to(work)
        except ValueError as exc:
            raise ProtocolError(
                "ARCHIVE_TASK_SCOPE_INVALID",
                "cleanup source must stay inside the task directory",
                category="decision",
            ) from exc
        formal = [
            *request["path_snapshot"]["storage_roots"].values(),
            *request["path_snapshot"]["subtitle_roots"].values(),
        ]
        preview = cleanup_preview(
            staging_directory=task_output_root(work),
            source_directory=source,
            formal_directories=formal,
            exclusive_source_directory=bool(scope.get("exclusive_source_directory", False)),
            shared_parent_directory=bool(scope.get("shared_parent_directory", False)),
            delivery_confirmed="finalize" in set(state.get("completed_steps") or []),
        )
        if command == "cleanup_preview":
            return preview
        if payload["preview_version"] != preview["preview_version"]:
            raise ProtocolError(
                "ARCHIVE_CLEANUP_PREVIEW_STALE",
                "cleanup paths or authorization changed; request a fresh preview",
                category="decision",
                details={"current_preview_version": preview["preview_version"]},
            )
        return execute_cleanup(preview, payload["selected_kinds"])
    if command == "initialize":
        capabilities = payload.get("capabilities")
        preset = payload.get("preset", "complete-archive")
        selection_mode = "custom" if isinstance(capabilities, list) else "preset"
        selection = workflow.resolve_capabilities(
            selection_mode=selection_mode,
            preset=preset,
            requested=capabilities,
            entrypoint="hub",
            branch=request["path_snapshot"]["branch"],
        )
        if selection_mode == "custom" and not selection["final_sinks"] and preset in {"complete-archive", "replacement"}:
            preset = "local-only"
            selection = workflow.resolve_capabilities(
                selection_mode=selection_mode,
                preset=preset,
                requested=capabilities,
                entrypoint="hub",
                branch=request["path_snapshot"]["branch"],
            )
        expected_sinks = payload.get("final_sinks")
        if expected_sinks is not None and set(expected_sinks) != set(selection["final_sinks"]):
            raise ProtocolError(
                "PROTOCOL_FINAL_SINK_MISMATCH",
                "resolved final_sinks differ from the Hub request",
                details={"requested": expected_sinks, "resolved": selection["final_sinks"]},
            )
        args = _arguments(request, work)
        args.task = preset
        output = workflow.init_state(args)
        actual_sinks = output.get("state", {}).get("requested_final_sinks", [])
        if expected_sinks is not None and set(expected_sinks) != set(actual_sinks):
            raise ProtocolError(
                "PROTOCOL_FINAL_SINK_MISMATCH",
                "resolved final_sinks differ from the Hub request",
                details={"requested": expected_sinks, "resolved": actual_sinks},
            )
        return output
    if command == "status":
        return {"status": "OK", "state": workflow.load_task_state(work)}
    if command == "approve_final":
        state = workflow.load_task_state(work)
        target = payload.get("final_target") if isinstance(payload.get("final_target"), dict) else {}
        storage_id = target.get("storage_id")
        branch = request["path_snapshot"]["branch"]
        storage_roots = request["path_snapshot"]["storage_roots"]
        final_sinks = set(state.get("final_sinks") or state.get("requested_final_sinks") or [])
        video_root = (
            _reviewed_video_root(state, storage_roots.get(storage_id))
            if "video" in final_sinks
            else None
        )
        library_number = str(storage_id or "").removeprefix("storage_")
        library = f"{'Anime' if branch == 'tv' else 'Movie'}{library_number}" if video_root else None
        title = str(state.get("decisions", {}).get("title") or "").strip()
        subtitle_root = request["path_snapshot"]["subtitle_roots"].get(branch)
        zip_target = str((Path(subtitle_root) / f"{title}.zip").resolve(strict=False)) if "subtitle_zip" in final_sinks and subtitle_root and title else None
        args = Namespace(
            work_dir=str(work),
            kind="final",
            library=library,
            video_root=video_root,
            zip=zip_target,
            tracker_column=None,
            operation=target.get("operation"),
            batch_id=target.get("batch_id"),
            target_actions=target.get("target_actions"),
        )
        return workflow.approve(args)
    args = _arguments(request, work)
    args.progress = _ACTIVE_PROGRESS.get()
    if command == "approve_preflight":
        return workflow.approve(args)
    if command == "run_step":
        output = workflow.run_step(args)
        if payload.get("step") == "inspect":
            return {**output, "analysis": _inspect_analysis(work, output)}
        if payload.get("step") == "review":
            return {**output, "analysis": _review_analysis(output)}
        return output
    raise ProtocolError("PROTOCOL_COMMAND_UNSUPPORTED", "command is not supported")


def _issues_from_output(output: dict[str, Any], status: str) -> list[dict[str, Any]]:
    raw = output.get("issues")
    if isinstance(raw, list):
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            details = item.get("details")
            normalized.append(
                {
                    "code": str(item.get("code") or "ARCHIVE_WORKFLOW_ISSUE"),
                    "category": str(item.get("category") or "execution"),
                    "retryable": bool(item.get("retryable", False)),
                    "message": str(item.get("message") or item.get("summary") or "archive workflow issue"),
                    "details": details if isinstance(details, dict) else {},
                }
            )
        return normalized
    if status not in {"needs_input", "failed"}:
        return []
    return [
        {
            "code": str(output.get("code") or "ARCHIVE_WORKFLOW_FAILED"),
            "category": "decision" if status == "needs_input" else "execution",
            "retryable": False,
            "message": str(output.get("error") or output.get("summary") or "archive workflow failed"),
            "details": {},
        }
    ]


def _progress_from_output(stage: str, status: str, output: dict[str, Any] | None) -> dict[str, Any]:
    result = output if isinstance(output, dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    total = 0
    for key in ("files", "videos", "episodes", "subtitles", "jobs", "artifacts"):
        for source in (summary, result):
            value = source.get(key)
            if isinstance(value, int) and value > total:
                total = value
    completed = total if status in {"succeeded", "warning", "skipped"} else 0
    current = str(result.get("current_file") or result.get("currentItem") or "")
    return {
        "stage": stage,
        "completed_items": completed,
        "total_items": total,
        "current_item": current,
    }


def _next_action_from_status(stage: str, status: str, output: dict[str, Any] | None) -> dict[str, Any]:
    if status == "needs_input":
        return {"id": "repreflight", "label": "补充信息并重新前检", "enabled": True, "reason": "仍有待补决定"}
    if status == "failed":
        return {"id": "retry", "label": "从检查点重试", "enabled": True, "reason": "当前步骤执行失败"}
    if stage == "inspect" and status in {"succeeded", "warning"}:
        return {"id": "confirm_preflight", "label": "确认并生成暂存产物", "enabled": True, "reason": ""}
    if stage == "review" and status in {"succeeded", "warning"}:
        return {"id": "confirm_final", "label": "完成验收并确认写入", "enabled": True, "reason": ""}
    return {"id": "none", "label": "查看结果", "enabled": status not in {"accepted", "running"}, "reason": ""}


def _protocol_cache_root() -> Path:
    configured = str(os.environ.get("ARCHIVE_PROTOCOL_CACHE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(tempfile.gettempdir()) / f"archive-plex-anime-protocol-{PROTOCOL_VERSION}-rules-{RULES_VERSION}"


def _command_cache(work: Path | None, command_id: str) -> Path:
    name = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    if work is None:
        return _protocol_cache_root() / f"{name}.json"
    return temporary_path(work, "protocol-commands", f"{name}.json")


@contextmanager
def _exclusive_command_lock(cache: Path):
    lock_path = cache.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _workflow_state(work: Path | None, output: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(output, dict) and isinstance(output.get("state"), dict):
        return output["state"]
    if work is None:
        return {}
    try:
        state = workflow.load_task_state(work)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _sanitize_hub_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_hub_value(item)
            for key, item in value.items()
            if not is_hub_forbidden_key(key)
        }
    if isinstance(value, list):
        return [_sanitize_hub_value(item) for item in value]
    return value


def _artifact_item(
    artifact_id: str,
    kind: str,
    path: str,
    state: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "path": path,
        "state": state,
        "metadata": metadata or {},
    }


def _artifact_projection(work: Path | None, state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    artifacts = empty_artifacts()
    if work is None:
        return artifacts
    artifacts["inputs"].append(
        _artifact_item(
            "input-task-directory",
            "task_directory",
            str(work),
            "available" if work.is_dir() else "planned",
            {"branch": state.get("branch")},
        )
    )
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        return artifacts
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        return artifacts
    if not isinstance(manifest, dict):
        return artifacts
    final = manifest.get("finalPreparation", {}).get("final") or manifest.get("plan", {}).get("final", {})
    if not isinstance(final, dict):
        return artifacts
    checkpoints = state.get("final_results") if isinstance(state.get("final_results"), dict) else {}
    final_target = state.get("final_target") if isinstance(state.get("final_target"), dict) else {}
    for manifest_kind, artifact_kind in (("video", "video"), ("zip", "subtitle_zip")):
        jobs = final.get(manifest_kind)
        if not isinstance(jobs, list):
            continue
        completed = checkpoints.get(manifest_kind) if isinstance(checkpoints.get(manifest_kind), dict) else {}
        for index, job in enumerate(jobs, start=1):
            if not isinstance(job, dict):
                continue
            source = str(job.get("source") or "")
            destination = str(job.get("destination") or "")
            if source:
                artifacts["staged"].append(
                    _artifact_item(
                        f"staged-{manifest_kind}-{index}",
                        artifact_kind,
                        source,
                        "ready" if Path(source).is_file() else "planned",
                    )
                )
            if destination:
                checkpoint = completed.get(destination) if isinstance(completed, dict) else None
                verified = (
                    isinstance(checkpoint, dict)
                    and checkpoint.get("status") == "COMPLETE"
                    and Path(destination).is_file()
                )
                artifacts["final"].append(
                    _artifact_item(
                        f"final-{manifest_kind}-{index}",
                        artifact_kind,
                        destination,
                        "verified" if verified else "planned",
                        {"library": final_target.get("library")},
                    )
                )
    return artifacts


def _checkpoint_projection(state: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for index, step in enumerate(state.get("completed_steps") or [], start=1):
        checkpoints.append(
            {
                "checkpoint_id": f"stage-{index}-{step}",
                "stage": str(step),
                "status": "completed",
                "resumable": True,
                "details": {},
            }
        )
    final_results = state.get("final_results") if isinstance(state.get("final_results"), dict) else {}
    attempts = final_results.get("attempts") if isinstance(final_results.get("attempts"), dict) else {}
    checkpoint_index = 0
    for kind in ("video", "zip"):
        pending = attempts.get(kind) if isinstance(attempts.get(kind), dict) else {}
        for destination, item in pending.items():
            if isinstance(item, dict) and item.get("status") == "IN_PROGRESS":
                checkpoint_index += 1
                checkpoints.append(
                    {
                        "checkpoint_id": f"final-in-progress-{checkpoint_index}",
                        "stage": "finalize",
                        "status": "in_progress",
                        "resumable": True,
                        "details": {"kind": kind, "destination": str(destination)},
                    }
                )
        completed = final_results.get(kind) if isinstance(final_results.get(kind), dict) else {}
        for destination, item in completed.items():
            if isinstance(item, dict) and item.get("status") == "COMPLETE":
                checkpoint_index += 1
                checkpoints.append(
                    {
                        "checkpoint_id": f"final-completed-{checkpoint_index}",
                        "stage": "finalize",
                        "status": "completed",
                        "resumable": True,
                        "details": {
                            "kind": kind,
                            "destination": str(destination),
                            "size": item.get("size"),
                        },
                    }
                )
    return checkpoints


def _replay_cached_events(
    cache: Path,
    request: dict[str, Any],
    digest: str,
) -> list[dict[str, Any]] | None:
    if not cache.is_file():
        return None
    stored = read_json(cache)
    if not isinstance(stored, dict) or stored.get("request_sha256") != digest:
        raise ProtocolError("PROTOCOL_COMMAND_ID_REUSED", "command_id was already used for a different request")
    events = stored.get("events")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise ProtocolError("PROTOCOL_COMMAND_CACHE_INVALID", "stored protocol command result is invalid")
    cache_state = stored.get("state")
    if cache_state == "running":
        stage = request["payload"].get("step") if request["command"] == "run_step" else request["command"]
        interrupted = event(
            request,
            sequence=len(events) + 1,
            stage=stage,
            status="failed",
            message="previous command process ended before a final event was recorded",
            issues=[
                {
                    "code": "PROTOCOL_COMMAND_INTERRUPTED",
                    "category": "execution",
                    "retryable": False,
                    "message": "the original command did not record a final event and will not be executed again",
                    "details": {},
                }
            ],
            checkpoints=[
                {
                    "checkpoint_id": "command-interrupted",
                    "stage": stage,
                    "status": "interrupted",
                    "resumable": False,
                    "details": {},
                }
            ],
        )
        events.append(interrupted)
        write_json_atomic(cache, {"state": "completed", "request_sha256": digest, "events": events})
    elif cache_state != "completed":
        raise ProtocolError("PROTOCOL_COMMAND_CACHE_INVALID", "stored protocol command state is invalid")
    return events


def execute(
    request_value: Any,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    request = validate_request(request_value)
    work = None if request["command"] in {"capabilities", "recommend", "metadata_check"} else resolve_work_dir(request["path_snapshot"])
    digest = request_digest(request)
    cache_work = None if request["command"] in {"cleanup_preview", "cleanup_execute"} else work
    cache = _command_cache(cache_work, request["command_id"])
    with _exclusive_command_lock(cache):
        cached = _replay_cached_events(cache, request, digest)
        if cached is not None:
            if on_event is not None:
                for item in cached:
                    on_event(item)
            return cached

        stage = request["payload"].get("step") if request["command"] == "run_step" else request["command"]
        events = [
            event(request, sequence=1, stage=stage, status="accepted", message="command accepted"),
            event(request, sequence=2, stage=stage, status="running", message="command running"),
        ]
        latest_progress: dict[str, Any] | None = None
        write_json_atomic(cache, {"state": "running", "request_sha256": digest, "events": events})
        if on_event is not None:
            for item in events:
                on_event(item)

        def record_progress(value: dict[str, Any]) -> None:
            nonlocal latest_progress
            if not isinstance(value, dict):
                return
            progress = {
                "stage": str(value.get("stage") or stage),
                "completed_items": int(value.get("completed_items") or 0),
                "total_items": int(value.get("total_items") or 0),
                "current_item": str(value.get("current_item") or ""),
            }
            for key in ("reused_items", "remaining_items", "remaining_bytes", "available_bytes"):
                if key in value:
                    progress[key] = max(int(value.get(key) or 0), 0)
            if "action" in value:
                progress["action"] = str(value.get("action") or "")
            latest_progress = progress
            progress_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status="running",
                message=str(value.get("message") or "file progress updated"),
                artifacts=_artifact_projection(work, _workflow_state(work)),
                checkpoints=_checkpoint_projection(_workflow_state(work)),
                progress=progress,
            )
            events.append(progress_event)
            write_json_atomic(cache, {"state": "running", "request_sha256": digest, "events": events})
            if on_event is not None:
                on_event(progress_event)

        output: dict[str, Any] | None = None
        try:
            progress_token = _ACTIVE_PROGRESS.set(record_progress)
            try:
                output = _sanitize_hub_value(_dispatch(request))
            finally:
                _ACTIVE_PROGRESS.reset(progress_token)
            status = normalized_status(output.get("status"))
            summary = str(output.get("summary") or output.get("error") or status)
            state = _workflow_state(work, output)
            final_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status=status,
                message=summary,
                issues=_issues_from_output(output, status),
                artifacts=_artifact_projection(work, state),
                checkpoints=_checkpoint_projection(state),
                progress=_progress_from_output(stage, status, output),
                next_action=_next_action_from_status(stage, status, output),
                result=output,
            )
        except ProtocolError as exc:
            final_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status="failed",
                message=str(exc),
                issues=[exc.issue()],
                artifacts=_artifact_projection(work, _workflow_state(work)),
                checkpoints=_checkpoint_projection(_workflow_state(work)),
                next_action=(
                    {
                        "id": "refresh_cleanup_preview",
                        "label": "重新预览清理内容",
                        "enabled": True,
                        "reason": "目录内容或清理授权已变化",
                    }
                    if exc.code == "ARCHIVE_CLEANUP_PREVIEW_STALE"
                    else None
                ),
            )
        except WorkflowIssue as exc:
            details: dict[str, Any] = dict(exc.details)
            try:
                parsed = json.loads(str(exc))
                if isinstance(parsed, dict):
                    details = {**parsed, **details}
            except json.JSONDecodeError:
                pass
            issue = {
                "code": exc.code
                or ("ARCHIVE_DECISION_REQUIRED" if exc.status == "NEEDS_USER" else "ARCHIVE_WORKFLOW_FAILED"),
                "category": "decision" if exc.status == "NEEDS_USER" else "execution",
                "retryable": exc.retryable,
                "message": str(exc),
                "details": details,
            }
            status = "needs_input" if exc.status == "NEEDS_USER" else "failed"
            state = _workflow_state(work)
            final_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status=status,
                message=str(exc),
                issues=[issue],
                artifacts=_artifact_projection(work, state),
                checkpoints=_checkpoint_projection(state),
                progress=latest_progress,
                next_action=(
                    {
                        "id": "recheck_affected_files",
                        "label": "重新检查受影响文件",
                        "enabled": True,
                        "reason": "当前文件已完成并保存检查点",
                    }
                    if exc.code == "ARCHIVE_SAFE_STOP_REQUESTED"
                    else None
                ),
            )
        except ToolchainError as exc:
            issue = {
                "code": exc.code,
                "category": "configuration",
                "retryable": False,
                "message": str(exc),
                "details": {},
            }
            state = _workflow_state(work)
            final_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status="failed",
                message=str(exc),
                issues=[issue],
                artifacts=_artifact_projection(work, state),
                checkpoints=_checkpoint_projection(state),
            )
        except Exception as exc:
            issue = {
                "code": "ARCHIVE_ENGINE_FAILED",
                "category": "execution",
                "retryable": False,
                "message": str(exc),
                "details": {},
            }
            state = _workflow_state(work)
            final_event = event(
                request,
                sequence=len(events) + 1,
                stage=stage,
                status="failed",
                message=str(exc),
                issues=[issue],
                artifacts=_artifact_projection(work, state),
                checkpoints=_checkpoint_projection(state),
            )

        events.append(final_event)
        write_json_atomic(cache, {"state": "completed", "request_sha256": digest, "events": events})
        if on_event is not None:
            on_event(final_event)
        return events


def _invalid_event(request: Any, exc: ProtocolError) -> dict[str, Any]:
    base = request if isinstance(request, dict) else {}
    safe = {
        "task_id": base.get("task_id", "invalid"),
        "run_id": base.get("run_id", "invalid"),
        "command_id": base.get("command_id", "invalid"),
    }
    return event(safe, sequence=1, stage="protocol", status="failed", message=str(exc), issues=[exc.issue()])


def _emit(events: list[dict[str, Any]]) -> None:
    for item in events:
        print(json.dumps(item, ensure_ascii=False, separators=(",", ":")), flush=True)


def _emit_one(item: dict[str, Any]) -> None:
    _emit([item])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="archive-plex-anime Hub execution protocol")
    sub = parser.add_subparsers(dest="command", required=True)
    describe = sub.add_parser("describe")
    describe.add_argument("--output")
    sub.add_parser("execute")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "describe":
        descriptor = protocol_descriptor()
        if args.output:
            write_json_atomic(Path(args.output).resolve(strict=False), descriptor)
        print(json.dumps(descriptor, ensure_ascii=False, indent=2))
        return 0
    raw = sys.stdin.buffer.read()
    request: Any = {}
    try:
        request = json.loads(raw.decode("utf-8-sig"))
        events = execute(request, on_event=_emit_one)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        error = ProtocolError("PROTOCOL_REQUEST_INVALID", "stdin must contain one UTF-8 JSON object", details={"error": str(exc)})
        events = [_invalid_event(request, error)]
        _emit(events)
    except ProtocolError as exc:
        events = [_invalid_event(request, exc)]
        _emit(events)
    return 0 if events[-1]["status"] not in {"failed", "needs_input"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
