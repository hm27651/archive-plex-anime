"""Public capability catalog and deterministic workflow selection rules.

Users select capabilities or a preset.  This module is the only place that
maps those choices to the private step state machine and final output sinks.
It performs no filesystem or tool discovery.
"""

from __future__ import annotations

from typing import Any, Iterable


PRESET_VERSION = 1
ENTRYPOINTS = ("cli", "skill", "hub")
PRESETS = ("complete-archive", "replacement", "archive-only", "local-only")

CAPABILITY_ORDER = (
    "inspect",
    "metadata",
    "movie-audio",
    "subtitle",
    "remux",
    "subtitle-package",
    "video-delivery",
    "subtitle-delivery",
    "kdocs-tracker",
    "cleanup",
)

CAPABILITIES: dict[str, dict[str, Any]] = {
    "inspect": {"label": "媒体前检", "branches": ("tv", "movie")},
    "metadata": {"label": "元数据识别", "branches": ("tv", "movie"), "requires": ("inspect",)},
    "movie-audio": {"label": "Movie 原盘音轨", "branches": ("movie",), "requires": ("inspect",)},
    "subtitle": {"label": "字幕与字体处理", "branches": ("tv", "movie"), "requires": ("inspect",)},
    "remux": {"label": "MKV 重新封装", "branches": ("tv", "movie"), "requires": ("inspect",)},
    "subtitle-package": {
        "label": "字幕 ZIP 打包",
        "branches": ("tv", "movie"),
        "requires": ("inspect",),
    },
    "video-delivery": {
        "label": "视频入库",
        "branches": ("tv", "movie"),
        "requires": ("inspect",),
        "sink": "video",
    },
    "subtitle-delivery": {
        "label": "字幕 ZIP 归档",
        "branches": ("tv", "movie"),
        "requires": ("inspect", "subtitle-package"),
        "sink": "subtitle_zip",
    },
    "kdocs-tracker": {
        "label": "KDocs 维护表",
        "branches": ("tv", "movie"),
        "requires": ("inspect",),
        "sink": "tracker",
    },
    "cleanup": {"label": "安全清理", "branches": ("tv", "movie"), "requires": ("inspect",)},
}

ENTRYPOINT_HIDDEN = {
    "cli": frozenset(),
    "skill": frozenset(),
    "hub": frozenset({"kdocs-tracker"}),
}

PRESET_CAPABILITIES = {
    "complete-archive": CAPABILITY_ORDER,
    "replacement": CAPABILITY_ORDER,
    "archive-only": (
        "inspect",
        "metadata",
        "video-delivery",
        "subtitle-delivery",
        "kdocs-tracker",
        "cleanup",
    ),
    # local-only intentionally has no fixed processing set.  The legacy
    # --steps values, or requested_capabilities, are added by the caller.
    "local-only": ("inspect",),
}

LOCAL_ONLY_CAPABILITIES = frozenset(
    {"inspect", "metadata", "movie-audio", "subtitle", "remux", "subtitle-package"}
)
ARCHIVE_ONLY_CAPABILITIES = frozenset(
    {
        "inspect",
        "metadata",
        "subtitle-package",
        "video-delivery",
        "subtitle-delivery",
        "kdocs-tracker",
        "cleanup",
    }
)
ARCHIVE_ONLY_EXPLICIT_UNSUPPORTED = frozenset({"movie-audio", "subtitle", "remux", "subtitle-package"})
FINAL_CAPABILITIES = frozenset({"video-delivery", "subtitle-delivery", "kdocs-tracker"})

CAPABILITY_TO_STEP = {
    "inspect": "inspect",
    "movie-audio": "movie-audio",
    "subtitle": "subtitle",
    "remux": "remux",
    "subtitle-package": "package",
}
STEP_TO_CAPABILITY = {
    "movie-audio": "movie-audio",
    "subtitle": "subtitle",
    "remux": "remux",
    "package": "subtitle-package",
}
STEP_ORDER = ("inspect", "movie-audio", "subtitle", "remux", "package", "review", "finalize", "cleanup")


def _ordered(values: Iterable[str]) -> list[str]:
    selected = set(values)
    return [name for name in CAPABILITY_ORDER if name in selected]


def visible_capabilities(entrypoint: str, branch: str | None = None) -> list[str]:
    hidden = ENTRYPOINT_HIDDEN.get(entrypoint, frozenset())
    return [
        name
        for name in CAPABILITY_ORDER
        if name not in hidden and (branch is None or branch in CAPABILITIES[name]["branches"])
    ]


def capability_catalog(entrypoint: str = "cli", branch: str | None = None) -> dict[str, Any]:
    if entrypoint not in ENTRYPOINTS:
        raise ValueError(f"unknown entrypoint: {entrypoint}")
    visible = visible_capabilities(entrypoint, branch)
    return {
        "preset_version": PRESET_VERSION,
        "entrypoint": entrypoint,
        "branch": branch,
        "capabilities": [
            {
                "id": name,
                "label": CAPABILITIES[name]["label"],
                "branches": list(CAPABILITIES[name]["branches"]),
                "requires": list(CAPABILITIES[name].get("requires", ())),
                "sink": CAPABILITIES[name].get("sink"),
            }
            for name in visible
        ],
        "presets": [
            {
                "id": preset,
                "capabilities": [name for name in PRESET_CAPABILITIES[preset] if name in visible],
            }
            for preset in PRESETS
        ],
    }


def legacy_steps_to_capabilities(steps: Iterable[str] | None) -> list[str]:
    return _ordered(STEP_TO_CAPABILITY[step] for step in steps or () if step in STEP_TO_CAPABILITY)


def capabilities_to_legacy_steps(capabilities: Iterable[str] | None) -> list[str]:
    selected = set(capabilities or ())
    return [
        step
        for step in ("movie-audio", "subtitle", "remux", "package")
        if STEP_TO_CAPABILITY[step] in selected
    ]


def _dependency_closure(selected: set[str], available: set[str] | None, *, preset: str) -> set[str]:
    changed = True
    while changed:
        changed = False
        for name in tuple(selected):
            for dependency in CAPABILITIES.get(name, {}).get("requires", ()):
                if dependency not in selected:
                    selected.add(dependency)
                    changed = True
        # These dependencies depend on the inspected media plan.  They are
        # added only when the corresponding internal work actually exists.
        if available is not None and "remux" in selected:
            for dependency in ("movie-audio", "subtitle"):
                if dependency in available and dependency not in selected:
                    selected.add(dependency)
                    changed = True
        if (
            available is not None
            and preset != "archive-only"
            and "subtitle-package" in selected
            and "subtitle" in available
            and "subtitle" not in selected
        ):
            selected.add("subtitle")
            changed = True
        if available is not None and "video-delivery" in selected and "remux" in available and "remux" not in selected:
            selected.add("remux")
            changed = True
    return selected


def resolve_capabilities(
    *,
    selection_mode: str,
    preset: str,
    requested: Iterable[str] | None,
    entrypoint: str,
    branch: str,
    available: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Resolve a public selection into capabilities, steps, sinks and issues."""

    requested_list = [str(value).strip() for value in requested or () if str(value).strip()]
    requested_set = set(requested_list)
    issues: list[dict[str, Any]] = []
    if selection_mode not in {"preset", "custom"}:
        issues.append({"code": "SELECTION_MODE_UNKNOWN", "selection_mode": selection_mode})
    if preset not in PRESETS:
        issues.append({"code": "PRESET_UNKNOWN", "preset": preset})
    if entrypoint not in ENTRYPOINTS:
        issues.append({"code": "ENTRYPOINT_UNKNOWN", "entrypoint": entrypoint})
    if branch not in {"tv", "movie"}:
        issues.append({"code": "BRANCH_REQUIRED", "branch": branch})

    known = set(CAPABILITIES)
    for name in sorted(requested_set - known):
        issues.append({"code": "CAPABILITY_UNKNOWN", "capability": name})

    visible = set(visible_capabilities(entrypoint, branch)) if entrypoint in ENTRYPOINTS else set()
    for name in sorted((requested_set & known) - visible):
        issues.append(
            {
                "code": "CAPABILITY_NOT_VISIBLE",
                "capability": name,
                "entrypoint": entrypoint,
            }
        )

    if selection_mode == "preset":
        selected = set(PRESET_CAPABILITIES.get(preset, ()))
        if preset == "local-only":
            selected.update(requested_set)
    else:
        selected = set(requested_set)
        if not selected or selected <= {"inspect", "metadata"}:
            issues.append(
                {
                    "code": "CUSTOM_EXECUTABLE_CAPABILITY_REQUIRED",
                    "detail": "inspect/metadata can be used through a preset but are not a standalone custom workflow",
                }
            )

    selected &= known
    selected &= visible
    if preset == "local-only":
        for name in sorted(selected - LOCAL_ONLY_CAPABILITIES):
            issues.append({"code": "LOCAL_CAPABILITY_UNSUPPORTED", "capability": name})
        selected &= LOCAL_ONLY_CAPABILITIES
    if preset == "archive-only":
        explicit_unsupported = requested_set & ARCHIVE_ONLY_EXPLICIT_UNSUPPORTED
        for name in sorted(explicit_unsupported | (selected - ARCHIVE_ONLY_CAPABILITIES)):
            issues.append({"code": "ARCHIVE_ONLY_CAPABILITY_UNSUPPORTED", "capability": name})
        selected -= explicit_unsupported
        selected &= ARCHIVE_ONLY_CAPABILITIES

    selected = _dependency_closure(selected, set(available) if available is not None else None, preset=preset)
    auto_added = selected - requested_set if selection_mode == "custom" else set()

    if "cleanup" in selected and not (selected & FINAL_CAPABILITIES):
        issues.append({"code": "CLEANUP_DELIVERY_REQUIRED", "capability": "cleanup"})

    unavailable: set[str] = set()
    if available is not None:
        available_set = set(available)
        unavailable = selected - available_set
        # Presets are adaptive: media-specific optional capabilities disappear.
        # A custom request is explicit and must report why it cannot run.
        if selection_mode == "custom":
            for name in _ordered(unavailable):
                issues.append({"code": "CAPABILITY_UNAVAILABLE", "capability": name})
        selected &= available_set
        auto_added &= available_set

    resolved = _ordered(selected)
    sinks = [
        CAPABILITIES[name]["sink"]
        for name in resolved
        if CAPABILITIES[name].get("sink")
    ]
    steps = [CAPABILITY_TO_STEP[name] for name in resolved if name in CAPABILITY_TO_STEP]
    if sinks:
        steps.extend(["review", "finalize"])
    if "cleanup" in resolved:
        steps.append("cleanup")
    steps = [step for step in STEP_ORDER if step in set(steps)]

    return {
        "requested_capabilities": _ordered(requested_set & known),
        "resolved_capabilities": resolved,
        "auto_added_capabilities": _ordered(auto_added),
        "unavailable_capabilities": _ordered(unavailable),
        "selected_steps": steps,
        "final_sinks": sinks,
        "issues": issues,
    }


def available_capabilities(plan: dict[str, Any], manifest: dict[str, Any]) -> set[str]:
    """Derive media-specific capabilities from an already generated plan."""

    available = {"inspect", "metadata"}
    if plan.get("movieAudioPlans"):
        available.add("movie-audio")
    if plan.get("subtitleGroups") or plan.get("renameJobs"):
        available.add("subtitle")
    if plan.get("remuxJobs"):
        available.add("remux")
    if plan.get("package"):
        available.add("subtitle-package")
    final = plan.get("final", {})
    if final.get("video"):
        available.add("video-delivery")
    if final.get("zip"):
        available.add("subtitle-delivery")
    tracker_state = manifest.get("discovery", {}).get("libraryTarget", {}).get("trackerState", {})
    if tracker_state.get("status") == "OK":
        available.add("kdocs-tracker")
    if available & {"video-delivery", "subtitle-delivery", "kdocs-tracker"}:
        available.add("cleanup")
    return available


def apply_final_sinks(plan: dict[str, Any], sinks: Iterable[str]) -> None:
    """Remove unselected final actions before the plan is sealed."""

    selected = set(sinks)
    final = plan.setdefault("final", {})
    if "video" not in selected:
        final["video"] = []
        final.pop("tvDirectoryCandidate", None)
        final.pop("directoryOperations", None)
    if "subtitle_zip" not in selected:
        final["zip"] = []
    final["tracker"] = "tracker" in selected
