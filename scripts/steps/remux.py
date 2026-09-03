from __future__ import annotations

from archive_rules import temporary_path
from common import backend_command, read_json, result


def run(context):
    progress_path = temporary_path(context["work_dir"], "hub-progress", "remux.json")
    progress_path.unlink(missing_ok=True)
    extra = ["--direct-output", "--progress-file", str(progress_path)]
    if "review" in context["state"].get("selected_steps", []):
        extra.append("--defer-output-validation")
    callback = getattr(context["args"], "progress", None)
    last_progress: dict = {}

    def relay_progress() -> None:
        nonlocal last_progress
        if not callable(callback) or not progress_path.is_file():
            return
        try:
            progress = read_json(progress_path)
        except Exception:
            return
        if not isinstance(progress, dict) or progress == last_progress:
            return
        last_progress = progress
        callback(progress)

    try:
        output = backend_command(
            context["work_dir"],
            "remux",
            extra,
            execute=True,
            approved_plan="remux confirmed by archive workflow",
            progress=relay_progress,
        )
    finally:
        relay_progress()
        progress_path.unlink(missing_ok=True)
    if output.get("status") == "SKIPPED":
        return result("NEEDS_USER", "remux requires a confirmed internal track plan")
    return result("COMPLETE", "serial MKV remux complete", files=len(output.get("files", [])))
