"""Unified deterministic planner for TV and Movie archive tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archive_rules import LOCAL_ONLY_REQUESTABLE_STEPS, validate_plan
from movie_plan import build_movie_archive_only_plan, build_movie_plan
from tv_plan import build_tv_archive_only_plan, build_tv_plan


STEP_ORDER = ["inspect", "movie-audio", "subtitle", "remux", "package", "review", "finalize", "cleanup"]
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
    available = {
        "inspect",
        *( ["movie-audio"] if plan.get("movieAudioPlans") else [] ),
        *( ["subtitle"] if plan.get("subtitleGroups") else [] ),
        *( ["remux"] if plan.get("remuxJobs") else [] ),
        *( ["package"] if plan.get("package") else [] ),
    }
    if task == "local-only":
        wanted = set(requested or [])
        wanted.add("inspect")
        if "package" in wanted and "subtitle" in available:
            wanted.add("subtitle")
        if "remux" in wanted and "subtitle" in available:
            wanted.add("subtitle")
        if "remux" in wanted and "movie-audio" in available:
            wanted.add("movie-audio")
        return [step for step in STEP_ORDER if step in available and step in wanted]
    return [
        step
        for step in STEP_ORDER
        if step in available or step in {"review", "finalize", "cleanup"}
    ]


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
    issues.extend(generated.get("issues", []))
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
    if plan:
        issues.extend(validate_plan(work.resolve(), branch, task, plan))
    selected = _selected_steps(task, plan, state.get("requested_steps"))
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
    if task != "local-only" and not plan.get("final", {}).get("video"):
        issues.append({"code": "EXECUTABLE_PLAN_REQUIRED"})
    elif selected == ["inspect"]:
        issues.append({"code": "EXECUTABLE_PLAN_REQUIRED"})
    return {**generated, "issues": issues, "selected_steps": selected, "metadata": metadata}
