from __future__ import annotations

from common import backend_command, result


def run(context):
    work = context["work_dir"]
    approved = "subtitle processing confirmed by archive workflow"
    prepare = backend_command(work, "prepare-fonts", [], execute=True, approved_plan=approved)
    subset = backend_command(work, "subset", [], execute=True, approved_plan=approved)
    rename = backend_command(work, "rename", ["--direct-output"], execute=True, approved_plan=approved)
    if subset.get("status") not in {"COMPLETE", "SKIPPED"}:
        return result("FAILED", "subtitle subsetting failed")
    if subset.get("status") == "COMPLETE" and rename.get("status") == "SKIPPED":
        return result("NEEDS_USER", "subtitle rename plan is missing")
    files = len(rename.get("files", [])) if isinstance(rename, dict) else 0
    groups = len(subset.get("groups", [])) if isinstance(subset, dict) else 0
    return result("COMPLETE", "subtitle processing complete", files=files, groups=groups, imported_fonts=len(prepare.get("imports", [])))
