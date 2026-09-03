"""Static invariants shared by the Plex archive planners.

This module is intentionally pure: it does not scan media, NAS storage, fonts,
or the tracker.  It only builds and validates deterministic task targets.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable


STATE_SCHEMA = 8
BACKEND_CACHE_SCHEMA = 19
RULES_VERSION = 19
WORKFLOW_REVISION = "2026-08-25-hub-risk-fixes-v1"
STATE_NAME = ".archive-state.json"
BACKEND_CACHE_NAME = "execution-cache.json"
TEMP_DIR_NAME = ".archive-temp"
LOG_DIR_NAME = ".archive-logs"
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
ALLOWED_HIDDEN_NAMES = {STATE_NAME, TEMP_DIR_NAME, LOG_DIR_NAME}
LOCAL_ONLY_REQUESTABLE_STEPS = ("movie-audio", "subtitle", "remux", "package")


def resolve_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.fspath(value))).expanduser().resolve(strict=False)


def state_path(work: Path) -> Path:
    return work / STATE_NAME


def task_output_root(work: Path) -> Path:
    configured = os.environ.get("ARCHIVE_TASK_OUTPUT_ROOT", "").strip()
    return resolve_path(configured) if configured else work / TEMP_DIR_NAME


def artifact_output_root(work: Path) -> Path:
    """Return the visible local-artifact root selected by the current task."""

    configured = os.environ.get("ARCHIVE_TASK_OUTPUT_ROOT", "").strip()
    return resolve_path(configured) if configured else work


def backend_cache_path(work: Path) -> Path:
    return task_output_root(work) / BACKEND_CACHE_NAME


def temporary_path(work: Path, category: str, name: str | None = None) -> Path:
    root = task_output_root(work) / category
    return root / name if name else root


def log_path(work: Path, name: str | None = None) -> Path:
    root = work / LOG_DIR_NAME
    return root / name if name else root


def numbered_candidate(path: Path, number: int) -> Path:
    if number < 0:
        raise ValueError("output number must be non-negative")
    return path if number == 0 else path.with_name(f"{path.stem} ({number}){path.suffix}")


def numbered_output_path(path: Path, reserved: Iterable[Path] = ()) -> Path:
    occupied = {os.path.normcase(str(item.resolve(strict=False))) for item in reserved}
    number = 0
    while True:
        candidate = numbered_candidate(path, number)
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in occupied and not candidate.exists():
            return candidate
        number += 1


def is_numbered_variant(path: Path, standard: Path) -> bool:
    if path.parent.resolve(strict=False) != standard.parent.resolve(strict=False) or path.suffix.casefold() != standard.suffix.casefold():
        return False
    return bool(re.fullmatch(rf"{re.escape(standard.stem)}(?: \([1-9]\d*\))?", path.stem, re.IGNORECASE))


def state_is_current(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema") == STATE_SCHEMA and value.get("rules_version") == RULES_VERSION


def is_under(path: Path, root: Path) -> bool:
    candidate = os.path.normcase(str(path.resolve(strict=False))).rstrip("\\/")
    parent = os.path.normcase(str(root.resolve(strict=False))).rstrip("\\/")
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        return False


def route_branch(work: Path, config: dict[str, Any]) -> dict[str, Any]:
    routes: list[tuple[str, str, Path]] = []
    for branch, key in (("anime", "workRoot"), ("movie", "movieWorkRoot")):
        value = config.get("paths", {}).get(key)
        if not value:
            continue
        root = resolve_path(value)
        if work.resolve(strict=False) == root:
            return {"status": "NEEDS_USER", "code": "WORK_ROOT_NOT_TASK", "branch": branch, "rootKey": key, "root": str(root)}
        if is_under(work, root):
            routes.append((branch, key, root))
    if len(routes) > 1:
        return {"status": "NEEDS_USER", "code": "OVERLAPPING_WORK_ROOTS", "matches": [item[0] for item in routes]}
    if not routes:
        return {"status": "UNRESOLVED", "code": "OUTSIDE_CONFIGURED_WORK_ROOTS"}
    branch, key, root = routes[0]
    return {"status": "OK", "branch": branch, "rootKey": key, "root": str(root)}


def tv_video_path(work: Path, title: str, season: int, episode: int) -> Path:
    return work / f"S{season}" / f"{title}.S{season:02d}E{episode:02d}.mkv"


def tv_subtitle_path(work: Path, title: str, season: int, episode: int, group: str) -> Path:
    return work / f"S{season}" / group / f"{title}.S{season:02d}E{episode:02d}.ass"


def tv_video_relative(title: str, season: int, episode: int) -> str:
    return (Path(title) / f"S{season}" / f"{title}.S{season:02d}E{episode:02d}.mkv").as_posix()


MOVIE_STACK_RE = re.compile(r"(?:^|\.)(cd[1-9]\d*)(?=\.|$)", re.IGNORECASE)


def movie_stack_suffix(value: str | os.PathLike[str]) -> str:
    match = MOVIE_STACK_RE.search(Path(value).name)
    return match.group(1).casefold() if match else ""


def movie_video_path(work: Path, title: str, stack: str = "") -> Path:
    suffix = f".{stack.casefold()}" if stack else ""
    return work / f"{title}{suffix}.mkv"


def movie_subtitle_path(work: Path, title: str, language: str, group: str, stack: str = "") -> Path:
    suffix = f".{stack.casefold()}" if stack else ""
    return work / f"{title}{suffix}.{language}.{group}.ass"


def movie_video_relative(title: str, stack: str = "") -> str:
    suffix = f".{stack.casefold()}" if stack else ""
    return (Path(title) / f"{title}{suffix}.mkv").as_posix()


def package_path(work: Path, title: str) -> Path:
    return work / f"{title}.zip"


def _safe_relative(path: Path, root: Path) -> Path | None:
    if not is_under(path, root):
        return None
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return None


def _target_issue(code: str, path: Any, detail: str = "") -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "path": str(path)}
    if detail:
        issue["detail"] = detail
    return issue


def _validate_local_target(work: Path, value: Any, *, allow_depth: set[int]) -> tuple[Path | None, dict[str, Any] | None]:
    path = Path(str(value)).resolve(strict=False)
    relative = _safe_relative(path, work)
    if relative is None:
        return None, _target_issue("CONTRACT_LOCAL_PATH_ESCAPE", path)
    if len(relative.parts) not in allow_depth:
        return relative, _target_issue("CONTRACT_LOCAL_DEPTH", path, f"depth={len(relative.parts)}")
    if any(part in ALLOWED_HIDDEN_NAMES for part in relative.parts):
        return relative, _target_issue("CONTRACT_USER_ARTIFACT_HIDDEN", path)
    return relative, None


def _expected_track_issues(tracks: Any, owner: Any) -> list[dict[str, Any]]:
    required = {"type", "language", "name", "default", "forced"}
    issues: list[dict[str, Any]] = []
    if not isinstance(tracks, list) or not tracks:
        return [{"code": "CONTRACT_TRACK_EXPECTATION_INCOMPLETE", "owner": str(owner), "detail": "expectedTracks must be a non-empty array"}]
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            issues.append({"code": "CONTRACT_TRACK_EXPECTATION_INCOMPLETE", "owner": str(owner), "track": index, "detail": "track must be an object"})
            continue
        missing = sorted(required - set(track))
        if track.get("type") == "audio" and "channels" not in track:
            missing.append("channels")
        if missing or track.get("type") not in {"video", "audio", "subtitles"} or type(track.get("default")) is not bool or type(track.get("forced")) is not bool:
            issues.append({
                "code": "CONTRACT_TRACK_EXPECTATION_INCOMPLETE",
                "owner": str(owner),
                "track": index,
                "missing": sorted(set(missing)),
            })
    return issues


def validate_plan(work: Path, branch: str, task: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return contract violations without performing any I/O scans."""

    issues: list[dict[str, Any]] = []
    title = str(plan.get("title") or "").strip()
    if not title or Path(title).name != title:
        return [{"code": "CONTRACT_TITLE_INVALID", "title": title}]

    tv_video_re = re.compile(rf"^{re.escape(title)}\.S(\d{{2}})E\d{{2,3}}\.mkv$", re.IGNORECASE)
    tv_ass_re = re.compile(rf"^{re.escape(title)}\.S(\d{{2}})E\d{{2,3}}\.ass$", re.IGNORECASE)
    movie_ass_re = re.compile(rf"^{re.escape(title)}(?:\.cd[1-9]\d*)?\.(JASC|JATC|SC|TC)\..+\.ass$", re.IGNORECASE)
    movie_video_re = re.compile(rf"^{re.escape(title)}(?:\.cd[1-9]\d*)?\.mkv$", re.IGNORECASE)
    remux_targets: set[str] = set()
    zip_targets: set[str] = set()
    final_targets: set[str] = set()
    output_root = artifact_output_root(work)

    for job in plan.get("renameJobs", []):
        relative, issue = _validate_local_target(output_root, job.get("target", ""), allow_depth={3} if branch == "tv" else {1})
        if issue:
            issues.append(issue)
            continue
        if branch == "tv":
            match = tv_ass_re.match(relative.name)
            season = re.fullmatch(r"S(\d{1,2})", relative.parts[0], re.IGNORECASE)
            if not match or not season or int(match.group(1)) != int(season.group(1)) or not relative.parts[1]:
                issues.append(_target_issue("CONTRACT_TV_SUBTITLE_PATH", relative))
        elif not movie_ass_re.match(relative.name):
            issues.append(_target_issue("CONTRACT_MOVIE_SUBTITLE_PATH", relative))

    for job in plan.get("remuxJobs", []):
        if not job.get("arguments") or not job.get("expectedTracks"):
            issues.append({"code": "CONTRACT_REMUX_EXPECTATION_REQUIRED", "output": str(job.get("output") or "")})
        issues.extend(_expected_track_issues(job.get("expectedTracks"), job.get("output", "")))
        relative, issue = _validate_local_target(output_root, job.get("output", ""), allow_depth={2} if branch == "tv" else {1})
        if issue:
            issues.append(issue)
            continue
        target_key = os.path.normcase(str((output_root / relative).resolve(strict=False)))
        if target_key in remux_targets:
            issues.append(_target_issue("CONTRACT_REMUX_TARGET_DUPLICATE", relative))
        remux_targets.add(target_key)
        if branch == "tv":
            match = tv_video_re.match(relative.name)
            season = re.fullmatch(r"S(\d{1,2})", relative.parts[0], re.IGNORECASE)
            if not match or not season or int(match.group(1)) != int(season.group(1)):
                issues.append(_target_issue("CONTRACT_TV_VIDEO_PATH", relative))
        elif not movie_video_re.match(relative.name):
            issues.append(_target_issue("CONTRACT_MOVIE_VIDEO_PATH", relative))

    package = plan.get("package")
    if package:
        relative, issue = _validate_local_target(output_root, package.get("output", ""), allow_depth={1})
        if issue:
            issues.append(issue)
        elif relative.name.casefold() != f"{title}.zip".casefold():
            issues.append(_target_issue("CONTRACT_PACKAGE_NAME", relative))
        for entry in package.get("entries", []):
            arcname = Path(str(entry.get("arcname") or "").replace("\\", "/"))
            if not arcname.parts or arcname.is_absolute() or ".." in arcname.parts:
                issues.append(_target_issue("CONTRACT_ZIP_ENTRY_UNSAFE", arcname))
                continue
            arc_key = arcname.as_posix().casefold()
            if arc_key in zip_targets:
                issues.append(_target_issue("CONTRACT_ZIP_ENTRY_DUPLICATE", arcname))
            zip_targets.add(arc_key)
            if branch == "tv":
                match = tv_ass_re.match(arcname.name)
                season = re.fullmatch(r"S(\d{1,2})", arcname.parts[0], re.IGNORECASE) if len(arcname.parts) == 3 else None
                if not match or not season or int(match.group(1)) != int(season.group(1)) or not arcname.parts[1]:
                    issues.append(_target_issue("CONTRACT_TV_ZIP_ENTRY", arcname))
            elif len(arcname.parts) != 1 or not movie_ass_re.match(arcname.name):
                issues.append(_target_issue("CONTRACT_MOVIE_ZIP_ENTRY", arcname))

    for job in plan.get("final", {}).get("video", []):
        if not job.get("expectedTracks"):
            issues.append({"code": "CONTRACT_FINAL_EXPECTATION_REQUIRED", "source": str(job.get("source") or "")})
        issues.extend(_expected_track_issues(job.get("expectedTracks"), job.get("relativePath", "")))
        relative = Path(str(job.get("relativePath") or "").replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (title,):
            issues.append(_target_issue("CONTRACT_FINAL_PATH_UNSAFE", relative))
            continue
        final_key = relative.as_posix().casefold()
        if final_key in final_targets:
            issues.append(_target_issue("CONTRACT_FINAL_TARGET_DUPLICATE", relative))
        final_targets.add(final_key)
        if branch == "tv":
            match = tv_video_re.match(relative.name)
            season = re.fullmatch(r"S(\d{1,2})", relative.parts[1], re.IGNORECASE) if len(relative.parts) == 3 else None
            if not match or not season or int(match.group(1)) != int(season.group(1)):
                issues.append(_target_issue("CONTRACT_TV_FINAL_PATH", relative))
        elif len(relative.parts) != 2 or not movie_video_re.match(relative.name):
            issues.append(_target_issue("CONTRACT_MOVIE_FINAL_PATH", relative))

    return issues
