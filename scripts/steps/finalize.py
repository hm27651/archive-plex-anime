from __future__ import annotations

import json
import sys

from common import backend_cache_path, backend_command, load_state, read_text, result


class FinalizeProgress:
    def __init__(self, work, batch: str) -> None:
        self.work = work
        self.batch = batch
        self.last = None
        self.video_total = 0
        self.zip_total = 0
        self.has_tracker = False
        try:
            manifest = json.loads(read_text(backend_cache_path(work)))
            final = manifest.get("finalPreparation", {}).get("final") or manifest.get("plan", {}).get("final", {})
            self.video_total = len(final.get("video", []))
            self.zip_total = len(final.get("zip", []))
            self.has_tracker = bool(final.get("trackerPlan"))
        except (OSError, json.JSONDecodeError):
            pass

    def snapshot(self) -> dict:
        state = load_state(self.work)
        checkpoint = state.get("final_results", {})
        if checkpoint.get("batch_id") != self.batch:
            checkpoint = {}
        return {
            "video_complete": sum(1 for item in checkpoint.get("video", {}).values() if item.get("status") == "COMPLETE"),
            "video_total": self.video_total,
            "zip_complete": sum(1 for item in checkpoint.get("zip", {}).values() if item.get("status") == "COMPLETE"),
            "zip_total": self.zip_total,
            "tracker": checkpoint.get("tracker", {}).get("status") == "COMPLETE",
            "tracker_required": self.has_tracker,
        }

    def __call__(self) -> None:
        current = self.snapshot()
        marker = (current["video_complete"], current["zip_complete"], current["tracker"])
        if marker == self.last:
            return
        self.last = marker
        zip_status = "无需" if not current["zip_total"] else "完成" if current["zip_complete"] >= current["zip_total"] else "等待"
        tracker_status = "无需" if not current["tracker_required"] else "完成" if current["tracker"] else "等待"
        print(
            f"[finalize] 视频 {current['video_complete']}/{current['video_total']} | ZIP {zip_status} | 维护表 {tracker_status}",
            file=sys.stderr,
            flush=True,
        )


def run(context):
    decisions = context["state"].get("decisions", {})
    batch = decisions.get("approved_batch") or decisions.get("batch_id")
    digest = context["state"].get("approved_final_digest")
    if not batch or not digest:
        return result("NEEDS_USER", "final batch approval, batch id, and digest are required")
    progress = FinalizeProgress(context["work_dir"], str(batch))
    output = backend_command(
        context["work_dir"],
        "finalize",
        ["--approved-batch", str(batch), "--approved-digest", str(digest)],
        execute=True,
        approved_plan="final batch confirmed by archive workflow",
        progress=progress,
    )
    return result(
        "COMPLETE",
        "final archive batch complete",
        warnings=list(output.get("warnings", [])),
        writes=progress.snapshot(),
    )
