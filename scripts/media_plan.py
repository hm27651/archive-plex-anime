"""Unified deterministic planner for TV and Movie archive tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archive_rules import LOCAL_ONLY_REQUESTABLE_STEPS, validate_plan
from capabilities import (
    apply_final_sinks,
    available_capabilities,
    legacy_steps_to_capabilities,
    resolve_capabilities,
)
from movie_plan import build_movie_archive_only_plan, build_movie_plan
from tv_plan import build_tv_archive_only_plan, build_tv_plan


ROUTE_BRANCH = {"tv": "anime", "movie": "movie"}
LOCAL_ONLY_UNSUPPORTED_STEPS = {"review"}


def local_only_request_issues(requested: list[str] | None) -> list[dict[str, Any]]:
    selected = {str(step) for step in requested or []}
    requestable = set(LOCAL_ONLY_REQUESTABLE_STEPS)
    issues = [
        {
            "code": "LOCAL_STEP_UNSUPPORTED",
            "step": step,
            "detail": "local-only steps validate their outputs immediately; review is reserved for final-target tasks",
        }
        for step in sorted(selected & LOCAL_ONLY_UNSUPPORTED_STEPS)
    ]
    issues.extend(
        {"code": "LOCAL_STEP_UNKNOWN", "step": step}
        for step in sorted(selected - requestable - LOCAL_ONLY_UNSUPPORTED_STEPS)
    )
    return issues


def _selected_steps(task: str, plan: dict[str, Any], requested: list[str] | None) -> list[str]:
    """Compatibility wrapper for callers that still supply local-only steps."""

    synthetic_manifest = {"discovery": {"libraryTarget": {}}}
    available = available_capabilities(plan, synthetic_manifest)
    resolved = resolve_capabilities(
        selection_mode="preset",
        preset=task,
        requested=legacy_steps_to_capabilities(requested),
        entrypoint="cli",
        branch="movie" if plan.get("movieAudioPlans") else "tv",
        available=available,
    )
    return resolved["selected_steps"]


def build_plan(work: Path, manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    branch = str(state.get("branch") or "")
    task = str(state.get("task") or "complete-archive")
    decisions = state.get("decisions", {})
    issues: list[dict[str, Any]] = []
    issues.extend(manifest.get("discovery", {}).get("fontIssues", []))
    metadata = manifest.get("discovery", {}).get("metadata", {})
    if isinstance(metadata, dict):
        issues.extend(metadata.get("issues", []))
    route = manifest.get("route", {})
    if route.get("status") != "OK" or route.get("branch") != ROUTE_BRANCH.get(branch):
        issues.append(
            {
                "code": "WORK_BRANCH_MISMATCH",
                "stateBranch": branch,
                "route": route,
            }
        )
    if task == "archive-only":
        if branch == "tv":
            generated = build_tv_archive_only_plan(work, manifest, decisions)
        elif branch == "movie":
            generated = build_movie_archive_only_plan(work, manifest, decisions)
        else:
            generated = {"plan": {}, "issues": [{"code": "BRANCH_REQUIRED"}], "summary": {}}
    elif branch == "tv":
        generated = build_tv_plan(
            work,
            manifest,
            decisions,
            completed_steps=set(state.get("completed_steps", [])),
        )
    elif branch == "movie":
        generated = build_movie_plan(
            work,
            manifest,
            decisions,
            completed_steps=set(state.get("completed_steps", [])),
        )
    else:
        generated = {"plan": {}, "issues": [{"code": "BRANCH_REQUIRED"}], "summary": {}}
    generated_issues = generated.get("issues", [])
    requested_final_sinks = state.get("requested_final_sinks")
    if isinstance(requested_final_sinks, list) and "subtitle_zip" not in requested_final_sinks:
        generated_issues = [
            issue
            for issue in generated_issues
            if issue.get("code") != "SUBTITLE_ARCHIVE_ROOT_REQUIRED"
        ]
    issues.extend(generated_issues)
    if (
        state.get("entrypoint") == "hub"
        and task != "local-only"
        and not isinstance(decisions.get("staging"), dict)
    ):
        issues.append({"code": "STAGING_OUTPUT_REQUIRED"})
    plan = generated.get("plan", {})
    library_target = manifest.get("discovery", {}).get("libraryTarget")
    if plan and task != "local-only" and library_target:
        resolution = library_target.get("resolution", {})
        if resolution.get("status") != "OK":
            issues.append({"code": resolution.get("code") or "LIBRARY_TARGET_REQUIRED", "target": library_target})
        else:
            requested_library = str(decisions.get("library") or "").strip()
            resolved_library = str(resolution.get("library") or "")
            if resolution.get("mode") == "create" and requested_library:
                resolved_library = requested_library
            elif requested_library and requested_library != resolved_library:
                issues.append({"code": "LIBRARY_DECISION_CONFLICT", "requested": requested_library, "resolved": resolved_library})
            if task == "replacement" and resolution.get("mode") == "create":
                issues.append({"code": "REPLACEMENT_TARGET_REQUIRED", "library": resolved_library})
            plan["preferredLibrary"] = resolved_library
            plan["libraryTarget"] = {**resolution, "library": resolved_library}
            plan.setdefault("final", {})["mode"] = resolution.get("mode") or plan.get("final", {}).get("mode")
    selection_mode = str(state.get("selection_mode") or "preset")
    entrypoint = str(state.get("entrypoint") or "cli")
    requested_capabilities = state.get("requested_capabilities")
    if requested_capabilities is None:
        requested_capabilities = legacy_steps_to_capabilities(state.get("requested_steps"))
    resolution = resolve_capabilities(
        selection_mode=selection_mode,
        preset=str(state.get("preset") or task),
        requested=requested_capabilities,
        entrypoint=entrypoint,
        branch=branch,
        available=available_capabilities(plan, manifest),
    )
    issues.extend(resolution["issues"])
    apply_final_sinks(plan, resolution["final_sinks"])
    if plan:
        issues.extend(validate_plan(work.resolve(), branch, task, plan))
    selected = resolution["selected_steps"]
    if task == "local-only":
        requested = list(state.get("requested_steps") or [])
        valid = set(LOCAL_ONLY_REQUESTABLE_STEPS)
        available = {
            "inspect",
            *(["movie-audio"] if plan.get("movieAudioPlans") else []),
            *(["subtitle"] if plan.get("subtitleGroups") else []),
            *(["remux"] if plan.get("remuxJobs") else []),
            *(["package"] if plan.get("package") else []),
        }
        issues.extend(local_only_request_issues(requested))
        for step in sorted((set(requested) & valid) - available):
            issues.append({"code": "LOCAL_STEP_UNAVAILABLE", "step": step})
    if task != "local-only" and not resolution["final_sinks"] and selection_mode == "preset":
        issues.append({"code": "EXECUTABLE_PLAN_REQUIRED"})
    elif selected == ["inspect"] and selection_mode == "preset":
        issues.append({"code": "EXECUTABLE_PLAN_REQUIRED"})
    return {
        **generated,
        "issues": issues,
        "selected_steps": selected,
        "metadata": metadata,
        "requested_capabilities": resolution["requested_capabilities"],
        "resolved_capabilities": resolution["resolved_capabilities"],
        "auto_added_capabilities": resolution["auto_added_capabilities"],
        "unavailable_capabilities": resolution["unavailable_capabilities"],
        "final_sinks": resolution["final_sinks"],
    }
