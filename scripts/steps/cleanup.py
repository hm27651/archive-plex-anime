from __future__ import annotations

import json
import os
import shutil
import stat

from archive_rules import is_under, resolve_path
from common import backend_cache_path, load_config, load_state, read_text, result


def _retry_readonly_removal(function, path, exc_info):
    error = exc_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def run(context):
    work = context["work_dir"]
    state = context["state"]
    if "finalize" not in state.get("completed_steps", []):
        return result("NEEDS_USER", "cleanup requires completed finalize step")
    if "video" not in state.get("final_sinks", []):
        return result("FAILED", "cleanup requires video delivery in final_sinks")
    resolved = work.resolve()
    config = load_config()
    root_key = "workRoot" if state.get("branch") == "tv" else "movieWorkRoot" if state.get("branch") == "movie" else ""
    root_value = config.get("paths", {}).get(root_key)
    root = resolve_path(str(root_value or ""))
    if not root_key or not root_value or not is_under(resolved, root) or resolved == root:
        return result("FAILED", f"task directory is outside the configured {state.get('branch')} work root: {resolved}")
    if resolved.parent == resolved or len(resolved.parts) < 3:
        return result("FAILED", f"unsafe cleanup path: {resolved}")
    manifest_path = backend_cache_path(work)
    if not manifest_path.is_file():
        return result("FAILED", "cleanup requires the final internal plan")
    manifest = json.loads(read_text(manifest_path))
    final = manifest.get("finalPreparation", {}).get("final", {})
    video_jobs = final.get("video", [])
    if not isinstance(video_jobs, list) or not video_jobs:
        return result("FAILED", "cleanup requires at least one completed video delivery")
    checkpoints = load_state(work).get("final_results", {})
    batch = str(final.get("batchId") or "")
    if not batch or checkpoints.get("batch_id") != batch:
        return result("FAILED", "cleanup final batch checkpoint mismatch")
    for kind in ("video", "zip"):
        for job in final.get(kind, []):
            checkpoint = checkpoints.get(kind, {}).get(str(job.get("destination")), {})
            if checkpoint.get("status") != "COMPLETE":
                return result("FAILED", f"cleanup requires completed {kind} checkpoint: {job.get('destination')}")
            source = resolve_path(str(job.get("source") or ""))
            destination = resolve_path(str(job.get("destination") or ""))
            if is_under(destination, resolved):
                return result("FAILED", f"cleanup target overlaps the task directory: {destination}")
            expected = checkpoint.get("source_size")
            if (
                not source.is_file()
                or not destination.is_file()
                or expected is None
                or source.stat().st_size != expected
                or destination.stat().st_size != expected
                or checkpoint.get("size") != expected
            ):
                return result("FAILED", f"cleanup destination checkpoint is no longer valid: {destination}")
    tracker_required = (
        "tracker" in state.get("final_sinks", [])
        if "final_sinks" in state
        else state.get("task") != "local-only"
    )
    if tracker_required and checkpoints.get("tracker", {}).get("status") != "COMPLETE":
        return result("FAILED", "cleanup requires completed tracker checkpoint")
    shutil.rmtree(resolved, onerror=_retry_readonly_removal)
    return result("COMPLETE", "task directory deleted")
