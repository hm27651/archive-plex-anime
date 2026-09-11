"""Bind explicit Hub delivery intent to the user-selected work directory."""

from copy import deepcopy
from pathlib import Path, PurePosixPath

from internal.errors import WorkflowError


def bind_video_targets(jobs: list[dict], directory: Path, mode: str, *, frozen: bool = False) -> list[dict]:
    """New-entry never consumes replacement choices, even in an existing series."""
    directory = directory.resolve(strict=False)
    if mode == "replace" and not directory.is_dir():
        raise WorkflowError("MANUAL_REPLACEMENT_TARGET_MISSING", f"洗版作品文件夹不存在：{directory}")
    result = []
    destinations = set()
    for original in jobs:
        job = deepcopy(original)
        text = str(job.get("relativePath") or "").replace("\\", "/")
        relative = PurePosixPath(text)
        if relative.is_absolute() or len(relative.parts) < 2 or any(p in {"", ".", ".."} or ":" in p for p in text.split("/")):
            raise WorkflowError("LIBRARY_TARGET_PATH_INVALID", f"无效的入库相对路径：{text}")
        # Planner paths start with the generated title; the chosen folder wins.
        destination = (directory / Path(*relative.parts[1:])).resolve(strict=False)
        if not destination.is_relative_to(directory) or destination == directory:
            raise WorkflowError("LIBRARY_TARGET_PATH_INVALID", f"入库路径超出作品文件夹：{destination}")
        if destination in destinations:
            raise WorkflowError("LIBRARY_CREATE_TARGET_EXISTS", f"多个视频使用同一入库文件名：{destination}")
        destinations.add(destination)
        for parent in [directory, *destination.parents]:
            if parent.is_relative_to(directory) and (parent.exists() or parent.is_symlink()) and not parent.is_dir():
                raise WorkflowError("LIBRARY_TARGET_PATH_INVALID", f"入库目录位置被文件占用：{parent}")
        exists = destination.exists() or destination.is_symlink()
        operation = "create" if mode == "create" else (
            str(job.get("operation")) if frozen else "replace" if exists else "create"
        )
        if operation not in {"create", "replace"}:
            raise WorkflowError("LIBRARY_TARGET_PATH_INVALID", "缺少已确认的入库动作。")
        if operation == "create" and exists:
            raise WorkflowError("LIBRARY_CREATE_TARGET_EXISTS", f"新入库文件已存在，请改名或选择洗版入库：{destination}")
        if operation == "replace" and not destination.is_file():
            raise WorkflowError("FINAL_REPLACE_TARGET_MISSING", f"待替换视频不存在：{destination}")
        job.update(destination=str(destination), operation=operation)
        result.append(job)
    return result
