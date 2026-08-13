from __future__ import annotations

import json

from common import backend_cache_path, backend_command, read_text, result


def run(context):
    extra = ["--direct-output"]
    if "review" in context["state"].get("selected_steps", []):
        extra.append("--defer-output-validation")
    output = backend_command(context["work_dir"], "package", extra, execute=True, approved_plan="subtitle ZIP confirmed by archive workflow")
    if output.get("status") == "SKIPPED":
        manifest = json.loads(read_text(backend_cache_path(context["work_dir"])))
        if not manifest.get("discovery", {}).get("subtitles"):
            return result("COMPLETE", "no subtitles to package", entries=0)
        return result("NEEDS_USER", "subtitle ZIP requires a confirmed package plan")
    return result("COMPLETE", "subtitle ZIP complete", entries=len(output.get("zip", {}).get("entries", [])))
