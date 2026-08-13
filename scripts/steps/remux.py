from __future__ import annotations

from common import backend_command, result


def run(context):
    extra = ["--direct-output"]
    if "review" in context["state"].get("selected_steps", []):
        extra.append("--defer-output-validation")
    output = backend_command(context["work_dir"], "remux", extra, execute=True, approved_plan="remux confirmed by archive workflow")
    if output.get("status") == "SKIPPED":
        return result("NEEDS_USER", "remux requires a confirmed internal track plan")
    return result("COMPLETE", "serial MKV remux complete", files=len(output.get("files", [])))
