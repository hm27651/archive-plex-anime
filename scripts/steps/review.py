from __future__ import annotations

import os
from pathlib import Path

from common import backend_command, result, save_state


def video_item_root(job, branch):
    destination = Path(str(job["destination"]))
    levels = 2 if branch == "tv" else 1
    root = destination
    for _ in range(levels):
        root = root.parent
    return root


def run(context):
    work = context["work_dir"]
    state = context["state"]
    local = backend_command(work, "verify-local", [])
    final = backend_command(work, "prepare-final", [])
    final_payload = final.get("final", {}) if isinstance(final, dict) else {}
    if final_payload:
        branch = state.get("branch")
        video_roots = [
            str(video_item_root(item, branch))
            for item in final_payload.get("video", [])
            if item.get("destination")
        ]
        video_root = ""
        if video_roots:
            video_root = str(Path(os.path.commonpath(video_roots)))
        zip_jobs = final_payload.get("zip", [])
        tracker_plan = final_payload.get("trackerPlan") or {}
        state["final_target"] = {
            "library": final.get("library"),
            "video_root": video_root,
            "zip": str(zip_jobs[0].get("destination", "")) if zip_jobs else "",
            "tracker_column": str(tracker_plan.get("column") or "") if tracker_plan else "",
            "operation": final.get("mode"),
            "batch_id": final.get("batchId"),
            "batch_digest": final.get("batchDigest") or final_payload.get("batchDigest"),
        }
        state.setdefault("approvals", {})["final"] = False
        state.pop("approved_final_digest", None)
        state.setdefault("decisions", {})["batch_id"] = final.get("batchId")
        save_state(work, state)
    zip_result = local.get("zip") if isinstance(local, dict) else None
    artifacts = {
        "videos": len(local.get("videos", [])) if isinstance(local, dict) else 0,
        "zip": bool(zip_result),
        "zip_entries": len(zip_result.get("entries", [])) if isinstance(zip_result, dict) else 0,
    }
    warnings = list(local.get("warnings", [])) if isinstance(local, dict) else []
    return result(
        "COMPLETE",
        "local artifact and final target review complete",
        warnings=warnings,
        artifacts=artifacts,
        final_target=state.get("final_target", {}),
    )
