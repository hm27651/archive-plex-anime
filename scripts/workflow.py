"""Step-oriented entry point for archive-plex-anime."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from archive_rules import (
    BACKEND_CACHE_SCHEMA,
    RULES_VERSION,
    STATE_SCHEMA,
    WORKFLOW_REVISION,
    resolve_path,
    state_path,
)
from capabilities import (
    PRESET_VERSION,
    PRESETS,
    capabilities_to_legacy_steps,
    capability_catalog,
    legacy_steps_to_capabilities,
    resolve_capabilities,
)
from common import WorkflowIssue, backend_cache_path, backend_command, configure_utf8_stdio, load_config, load_state, read_text, save_state
from media_plan import build_plan, local_only_request_issues
from plan_common import update_release_history


TASKS = set(PRESETS)
METADATA_DECISION_KEYS = {
    "enabled", "mode", "query", "tmdb_id", "tmdb_type", "tvdb_id", "language",
    "episode_order", "year", "season_bindings",
}
METADATA_BINDING_KEYS = {"tmdb_id", "tmdb_season"}
BACKEND_PUBLIC_STEPS = {
    "movie-audio": "movie-audio",
    "prepare-fonts": "subtitle",
    "subset": "subtitle",
    "rename": "subtitle",
    "remux": "remux",
    "package": "package",
    "verify-local": "review",
    "final-prepare": "review",
    "final-video": "finalize",
    "final-zip": "finalize",
    "final-tracker": "finalize",
    "cleanup": "cleanup",
}


def work_dir(args: argparse.Namespace) -> Path:
    return Path(args.work_dir or Path.cwd()).resolve()


def load_decisions(args: argparse.Namespace, input_stream=None) -> dict:
    use_stdin = bool(getattr(args, "decisions_stdin", False))
    if not use_stdin:
        return {}
    stream = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read()
    try:
        decision_text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError as exc:
        raise WorkflowIssue("NEEDS_USER", "--decisions-stdin must be UTF-8 JSON") from exc
    if not decision_text.strip():
        raise WorkflowIssue("NEEDS_USER", "--decisions-stdin is empty")
    try:
        supplied = json.loads(decision_text)
    except json.JSONDecodeError as exc:
        raise WorkflowIssue("NEEDS_USER", "--decisions-stdin must contain a UTF-8 JSON object") from exc
    if not isinstance(supplied, dict):
        raise WorkflowIssue("NEEDS_USER", "decisions must be a UTF-8 JSON object")
    metadata = supplied.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - METADATA_DECISION_KEYS:
            raise WorkflowIssue("NEEDS_USER", "metadata decisions contain unsupported or secret-bearing fields")
        bindings = metadata.get("season_bindings")
        if bindings is not None:
            if not isinstance(bindings, dict) or any(
                not isinstance(value, dict) or set(value) - METADATA_BINDING_KEYS
                for value in bindings.values()
            ):
                raise WorkflowIssue("NEEDS_USER", "metadata season_bindings are invalid")
    return supplied


def init_state(args: argparse.Namespace) -> dict:
    work = work_dir(args)
    if not work.is_dir():
        raise WorkflowIssue("FAILED", f"task directory not found: {work}")
    config = load_config()
    root_key = "workRoot" if args.branch == "tv" else "movieWorkRoot"
    root_value = config.get("paths", {}).get(root_key)
    if root_value and work == resolve_path(str(root_value)):
        raise WorkflowIssue("FAILED", f"configured work root is not a task directory: {work}")
    task = args.task
    decisions = load_decisions(args)
    raw_steps = getattr(args, "steps", None)
    raw_capabilities = getattr(args, "capabilities", None)
    if raw_steps and raw_capabilities:
        raise WorkflowIssue("NEEDS_USER", "--steps and --capabilities cannot be used together")
    requested = [item.strip() for item in raw_steps.split(",") if item.strip()] if raw_steps else None
    requested_capabilities = (
        [item.strip() for item in raw_capabilities.split(",") if item.strip()]
        if raw_capabilities
        else legacy_steps_to_capabilities(requested)
    )
    selection_mode = "custom" if raw_capabilities else "preset"
    entrypoint = str(getattr(args, "entrypoint", None) or "cli")
    if task == "local-only" and requested:
        request_issues = local_only_request_issues(requested)
        if request_issues:
            raise WorkflowIssue("NEEDS_USER", json.dumps({"issues": request_issues}, ensure_ascii=False))
    selection = resolve_capabilities(
        selection_mode=selection_mode,
        preset=task,
        requested=requested_capabilities,
        entrypoint=entrypoint,
        branch=args.branch,
    )
    if selection_mode == "custom" and not selection["final_sinks"] and task in {"complete-archive", "replacement"}:
        task = "local-only"
        selection = resolve_capabilities(
            selection_mode=selection_mode,
            preset=task,
            requested=requested_capabilities,
            entrypoint=entrypoint,
            branch=args.branch,
        )
    if selection["issues"]:
        raise WorkflowIssue("NEEDS_USER", json.dumps({"issues": selection["issues"]}, ensure_ascii=False))
    if selection_mode == "custom" and task == "local-only":
        requested = capabilities_to_legacy_steps(requested_capabilities)
    state = {
        "schema": STATE_SCHEMA,
        "rules_version": RULES_VERSION,
        "work_dir": str(work),
        "branch": args.branch,
        "task": task,
        "selection_mode": selection_mode,
        "preset": task if selection_mode == "preset" else None,
        "preset_version": PRESET_VERSION,
        "entrypoint": entrypoint,
        "requested_steps": requested,
        "requested_capabilities": selection["requested_capabilities"],
        "resolved_capabilities": selection["resolved_capabilities"],
        "auto_added_capabilities": selection["auto_added_capabilities"],
        "unavailable_capabilities": [],
        "selected_steps": ["inspect"],
        "requested_final_sinks": selection["final_sinks"],
        "final_sinks": [],
        "completed_steps": [],
        "approvals": {"preflight": False, "final": False},
        "decisions": decisions,
    }
    save_state(work, state)
    return {"status": "COMPLETE", "summary": f"initialized {task}", "state": state}


def invalidate_public_from(state: dict, step: str) -> str | None:
    """Clear public progress from one selected step and all of its dependants."""

    public_step = BACKEND_PUBLIC_STEPS.get(step, step)
    selected = list(state.get("selected_steps", []))
    if public_step not in selected:
        return None
    position = selected.index(public_step)
    affected = set(selected[position:])
    state["completed_steps"] = [item for item in state.get("completed_steps", []) if item not in affected]
    approvals = state.setdefault("approvals", {})
    if public_step == "inspect":
        approvals["preflight"] = False
    approvals["final"] = False
    state.pop("final_target", None)
    state.pop("final_results", None)
    state.pop("approved_final_digest", None)
    return public_step


def load_task_state(work: Path) -> dict:
    return load_state(work)


def inspect_cache_ready(work: Path) -> bool:
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(read_text(manifest_path))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schemaVersion") != BACKEND_CACHE_SCHEMA:
        return False
    if manifest.get("workflowRevision") != WORKFLOW_REVISION:
        return False
    work_path = manifest.get("workPath")
    if not isinstance(work_path, str) or not work_path.strip():
        return False
    if resolve_path(work_path) != work.resolve(strict=False):
        return False
    inspect_status = manifest.get("stages", {}).get("inspect", {}).get("status")
    return inspect_status in {"READY_FOR_PREFLIGHT", "NEEDS_USER"}


def run_step(args: argparse.Namespace) -> dict:
    work = work_dir(args)
    state = load_task_state(work)
    if not state:
        raise RuntimeError(f"state file missing: {state_path(work)}")
    step = args.command
    if step not in state.get("selected_steps", []):
        raise RuntimeError(f"step not selected: {step}")
    approvals = state.get("approvals", {})
    if step not in {"inspect"} and not approvals.get("preflight", False):
        raise RuntimeError("preflight approval required before local processing")
    if step in {"finalize", "cleanup"} and not approvals.get("final", False):
        raise RuntimeError("final approval required before final write or cleanup")
    if step in state.get("completed_steps", []) and not args.rerun:
        return {"status": "SKIPPED", "summary": f"step already complete: {step}"}
    if args.rerun:
        invalidate_public_from(state, step)
        save_state(work, state)
    position = state.get("selected_steps", []).index(step)
    missing_dependencies = [
        item
        for item in state.get("selected_steps", [])[:position]
        if item not in state.get("completed_steps", [])
    ]
    if missing_dependencies:
        raise RuntimeError(f"step dependencies incomplete: {missing_dependencies}")
    if step not in {"inspect", "cleanup"} and not backend_cache_path(work).is_file():
        rebuild_backend(work, state)
    module_name = "movie_audio" if step == "movie-audio" else step
    module = importlib.import_module(f"steps.{module_name}")
    output = module.run({"work_dir": work, "state": state, "args": args})
    if output.get("status") == "NEEDS_USER" and state.get("approvals", {}).get("preflight"):
        output = {**output, "status": "FAILED"}
    inspect_checkpoint = step == "inspect" and output.get("status") == "NEEDS_USER" and inspect_cache_ready(work)
    if output.get("status") in {"COMPLETE", "SUCCESS"} or inspect_checkpoint:
        # Steps such as review/finalize checkpoint state in a child process.
        # Reload before marking the step complete so those updates are retained.
        state = load_state(work) or state
        completed = [item for item in state.get("completed_steps", []) if item != step]
        completed.append(step)
        state["completed_steps"] = completed
        if step != "cleanup":
            save_state(work, state)
    return output


def internal_plan_ready(work: Path, selected_steps: list[str]) -> bool:
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(read_text(manifest_path))
    except (OSError, json.JSONDecodeError):
        return False
    plan = manifest.get("plan", {})
    final = plan.get("final", {})
    has_final_action = bool(final.get("video") or final.get("zip") or final.get("tracker") is True)
    if not plan or ("review" in selected_steps and not has_final_action):
        return False
    has_subtitles = bool(manifest.get("discovery", {}).get("subtitles"))
    if "subtitle" in selected_steps and has_subtitles and not plan.get("renameJobs"):
        return False
    if "remux" in selected_steps and not plan.get("remuxJobs"):
        return False
    if "package" in selected_steps and has_subtitles and not plan.get("package"):
        return False
    return True


def inspect_backend(work: Path, state: dict, *, rerun: bool = False) -> dict:
    decisions = state.get("decisions", {})
    extra = ["--work-path", str(work), "--task-mode", state.get("task", "complete-archive")]
    if rerun:
        extra.append("--rerun")
    if decisions.get("title"):
        extra.extend(["--title", str(decisions["title"])])
    if isinstance(decisions.get("metadata"), dict):
        extra.extend(["--metadata-json", json.dumps(decisions["metadata"], ensure_ascii=False, separators=(",", ":"))])
    if state.get("branch") == "movie" and decisions.get("retain_embedded_subtitles"):
        extra.append("--retain-embedded-subtitles")
    for requested in state.get("requested_steps") or []:
        extra.extend(["--requested-step", str(requested)])
    for capability in state.get("resolved_capabilities") or []:
        extra.extend(["--selected-capability", str(capability)])
    for sink in state.get("requested_final_sinks") or state.get("final_sinks") or []:
        extra.extend(["--selected-final-sink", str(sink)])
    if "kdocs-tracker" in state.get("resolved_capabilities", []) and state.get("entrypoint") != "hub":
        extra.append("--kdocs-tracker")
    movie_audio_pairs = decisions.get("movie_audio_pairs")
    disc_source = decisions.get("disc_source") or decisions.get("m2ts")
    if state.get("branch") == "movie" and (movie_audio_pairs or disc_source or decisions.get("movie_audio_replacement")):
        extra.append("--movie-audio-replacement")
        if movie_audio_pairs:
            extra.extend(["--movie-audio-pairs-json", json.dumps(movie_audio_pairs, ensure_ascii=False, separators=(",", ":"))])
        elif disc_source:
            extra.extend(["--disc-source", str(disc_source)])
            video_source = decisions.get("video_source") or decisions.get("old_mkv")
            if video_source:
                extra.extend(["--video-source", str(video_source)])
    return backend_command(work, "inspect", extra)


def configure_backend(work: Path, state: dict) -> dict:
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        raise WorkflowIssue("NEEDS_USER", "inspection cache is missing; rerun inspect before approval")
    manifest = json.loads(read_text(manifest_path))
    generated = build_plan(work, manifest, state)
    if generated["issues"]:
        status = "FAILED" if any(str(item.get("code", "")).startswith("CONTRACT_") for item in generated["issues"]) else "NEEDS_USER"
        raise WorkflowIssue(status, json.dumps({"issues": generated["issues"]}, ensure_ascii=False))
    if generated.get("resolved_release_groups"):
        state.setdefault("decisions", {})["release_group_by_season"] = generated["resolved_release_groups"]
    elif generated.get("resolved_release_group"):
        state.setdefault("decisions", {})["release_group"] = generated["resolved_release_group"]
    state["selected_steps"] = generated["selected_steps"]
    for key in (
        "requested_capabilities",
        "resolved_capabilities",
        "auto_added_capabilities",
        "unavailable_capabilities",
        "final_sinks",
    ):
        if key in generated:
            state[key] = generated[key]
    output = backend_command(
        work,
        "configure",
        ["--plan-stdin"],
        execute=True,
        approved_plan="preflight confirmed",
        stdin_json=generated["plan"],
    )
    if output.get("invalidatedFrom"):
        invalidate_public_from(state, str(output["invalidatedFrom"]))
    state["completed_steps"] = [item for item in state.get("completed_steps", []) if item in state["selected_steps"]]
    if generated.get("resolved_release_groups") and generated.get("release_labels_by_season"):
        for season, labels in generated["release_labels_by_season"].items():
            group = generated["resolved_release_groups"].get(f"S{season}")
            if labels and group:
                update_release_history(labels, str(group))
    elif generated.get("release_labels") and generated.get("resolved_release_group"):
        update_release_history(generated["release_labels"], str(generated["resolved_release_group"]))
    save_state(work, state)
    return output


def rebuild_backend(work: Path, state: dict) -> None:
    inspect_backend(work, state)
    if state.get("approvals", {}).get("preflight"):
        configure_backend(work, state)
        restart = restore_backend_results(work, state)
        if restart:
            raise WorkflowIssue("FAILED", f"execution cache rebuilt; resume from step: {restart}")
        return
    raise WorkflowIssue("NEEDS_USER", "execution cache is missing and cannot be rebuilt from this task state")


def restore_backend_results(work: Path, state: dict) -> str | None:
    """Invalidate local progress after cache loss instead of guessing numbered outputs."""

    selected = list(state.get("selected_steps", []))
    completed = set(state.get("completed_steps", []))
    local_order = [step for step in ("movie-audio", "subtitle", "remux", "package", "review") if step in selected]
    restart = next((step for step in local_order if step in completed), None)
    if restart is None:
        return None
    invalidate_public_from(state, restart)
    save_state(work, state)
    return restart


def approve(args: argparse.Namespace) -> dict:
    work = work_dir(args)
    state = load_task_state(work)
    if not state:
        raise RuntimeError(f"state file missing: {state_path(work)}")
    if args.kind == "preflight":
        supplied = load_decisions(args)
        if "inspect" not in state.get("completed_steps", []):
            raise WorkflowIssue("NEEDS_USER", "inspect must complete before preflight approval")
        manifest_path = backend_cache_path(work)
        if not manifest_path.is_file():
            raise WorkflowIssue("NEEDS_USER", "inspection cache is missing; rerun inspect")
        manifest = json.loads(read_text(manifest_path))
        metadata = manifest.get("discovery", {}).get("metadata", {})
        suggested = metadata.get("suggestedDecisions", {}) if isinstance(metadata, dict) else {}
        decisions = state.setdefault("decisions", {})
        protected = set(decisions) | set(supplied)
        suggested_metadata = suggested.get("metadata") if isinstance(suggested.get("metadata"), dict) else {}
        current_metadata = decisions.get("metadata") if isinstance(decisions.get("metadata"), dict) else {}
        supplied_metadata = supplied.get("metadata") if isinstance(supplied, dict) else None
        if suggested_metadata or current_metadata or isinstance(supplied_metadata, dict):
            decisions["metadata"] = {
                **suggested_metadata,
                **current_metadata,
                **(supplied_metadata if isinstance(supplied_metadata, dict) else {}),
            }
        for key, value in supplied.items():
            if key != "metadata":
                if key == "episode_map" and isinstance(value, dict):
                    current_map = decisions.get("episode_map") if isinstance(decisions.get("episode_map"), dict) else {}
                    decisions[key] = {**current_map, **value}
                else:
                    decisions[key] = value
        refresh_metadata = (
            isinstance(supplied_metadata, dict)
            or metadata.get("status") == "NEEDS_USER"
            or "title" in supplied
        )
        if refresh_metadata:
            inspect_backend(work, state, rerun=True)
            manifest = json.loads(read_text(manifest_path))
            metadata = manifest.get("discovery", {}).get("metadata", {})
            if metadata.get("status") == "NEEDS_USER":
                raise WorkflowIssue("NEEDS_USER", json.dumps({"issues": metadata.get("issues", [])}, ensure_ascii=False))
            refreshed = metadata.get("suggestedDecisions", {})
            for key, value in refreshed.items():
                if key == "metadata" and isinstance(value, dict):
                    current = decisions.get("metadata") if isinstance(decisions.get("metadata"), dict) else {}
                    decisions["metadata"] = {**value, **current}
                elif key == "episode_map" and isinstance(value, dict):
                    current_map = decisions.get("episode_map") if isinstance(decisions.get("episode_map"), dict) else {}
                    decisions[key] = {**value, **current_map}
                elif key not in protected:
                    decisions[key] = value
        else:
            for key, value in suggested.items():
                if key == "episode_map" and isinstance(value, dict):
                    current_map = decisions.get("episode_map") if isinstance(decisions.get("episode_map"), dict) else {}
                    decisions[key] = {**value, **current_map}
                elif key != "metadata" and key not in protected:
                    decisions[key] = value
        wants_movie_audio = state.get("branch") == "movie" and (
            state.get("decisions", {}).get("movie_audio_pairs")
            or state.get("decisions", {}).get("disc_source")
            or state.get("decisions", {}).get("m2ts")
            or state.get("decisions", {}).get("movie_audio_replacement")
        )
        if wants_movie_audio and not manifest.get("discovery", {}).get("movieAudioPreflights"):
            inspect_backend(work, state, rerun=True)
        configure_backend(work, state)
        if not internal_plan_ready(work, state.get("selected_steps", [])):
            raise WorkflowIssue("NEEDS_USER", "confirmed internal remux/package plan is missing")
        state.setdefault("approvals", {})["preflight"] = True
    else:
        if "review" not in state.get("completed_steps", []):
            raise WorkflowIssue("NEEDS_USER", "review must complete before final approval")
        target = state.setdefault("final_target", {})
        for key in ("library", "video_root", "zip", "tracker_column", "operation", "batch_id"):
            value = getattr(args, key, None)
            if value:
                if target.get(key) and str(target[key]) != str(value):
                    raise WorkflowIssue("NEEDS_USER", f"final target changed for {key}: {target[key]!r} -> {value!r}")
                target[key] = value
        if "final_sinks" in state:
            sinks = set(state.get("final_sinks", []))
        else:
            sinks = {"video"}
            if target.get("zip"):
                sinks.add("subtitle_zip")
            if state.get("task") != "local-only":
                sinks.add("tracker")
        required = {"library", "operation", "batch_id", "batch_digest"}
        if "video" in sinks:
            required.add("video_root")
        if "subtitle_zip" in sinks:
            required.add("zip")
        if "tracker" in sinks:
            required.add("tracker_column")
        missing = sorted(key for key in required if not target.get(key))
        if missing:
            raise WorkflowIssue("NEEDS_USER", f"final target is incomplete: {missing}")
        state.setdefault("approvals", {})["final"] = True
        state["approved_final_digest"] = str(target["batch_digest"])
        if target.get("batch_id"):
            state.setdefault("decisions", {})["batch_id"] = target["batch_id"]
    save_state(work, state)
    return {"status": "COMPLETE", "summary": f"{args.kind} approval recorded", "state": state}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plex archive workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--work-dir")
    init.add_argument("--branch", choices=["tv", "movie"], required=True)
    init.add_argument("--task", choices=sorted(TASKS), default="complete-archive")
    init.add_argument("--steps")
    init.add_argument("--capabilities", help="comma-separated public capabilities; enables custom selection mode")
    init.add_argument("--entrypoint", choices=["cli", "skill", "hub"], default="cli")
    init.add_argument("--decisions-stdin", action="store_true", help="read UTF-8 decisions JSON from stdin")
    catalog = sub.add_parser("capabilities")
    catalog.add_argument("--entrypoint", choices=["cli", "skill", "hub"], default="cli")
    catalog.add_argument("--branch", choices=["tv", "movie"])
    status = sub.add_parser("status")
    status.add_argument("--work-dir")
    approval = sub.add_parser("approve-preflight")
    approval.add_argument("--work-dir")
    approval.add_argument("--decisions-stdin", action="store_true", help="read UTF-8 decisions JSON from stdin")
    final_approval = sub.add_parser("approve-final")
    final_approval.add_argument("--work-dir")
    for key in ("library", "video-root", "zip", "tracker-column", "operation", "batch-id"):
        final_approval.add_argument(f"--{key}", dest=key.replace("-", "_"))
    for name in ("inspect", "subtitle", "movie-audio", "remux", "package", "review", "finalize", "cleanup"):
        item = sub.add_parser(name)
        item.add_argument("--work-dir")
        item.add_argument("--rerun", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    try:
        args = build_parser().parse_args(argv)
        if args.command == "init":
            output = init_state(args)
        elif args.command == "capabilities":
            output = {"status": "OK", **capability_catalog(args.entrypoint, args.branch)}
        elif args.command == "status":
            output = {"status": "OK", "state": load_task_state(work_dir(args))}
        elif args.command == "approve-preflight":
            args.kind = "preflight"
            output = approve(args)
        elif args.command == "approve-final":
            args.kind = "final"
            output = approve(args)
        else:
            output = run_step(args)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output.get("status") not in {"FAILED", "NEEDS_USER"} else 2
    except WorkflowIssue as exc:
        print(json.dumps({"status": exc.status, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
