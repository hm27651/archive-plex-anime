from __future__ import annotations

from common import backend_command, result


def run(context):
    output = backend_command(
        context["work_dir"],
        "movie-audio",
        [],
        execute=True,
        approved_plan="Movie original-disc audio replacement confirmed by archive workflow",
    )
    return result(
        "COMPLETE",
        "Movie original-disc audio processing complete",
        output=output.get("output"),
        reused=bool(output.get("reused")),
    )
