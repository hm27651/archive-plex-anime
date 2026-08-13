"""TV/Movie library target lookup without workflow or mutation state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


WEBRIP_MARK = "（WebRip）"
WEBRIP_SUFFIX = re.compile(r"\s*[（(]\s*(?:S\d{1,3}\s+)?WebRip\s*[）)]\s*$", re.IGNORECASE)
LIBRARIES = {
    "tv": ("Anime1", "Anime2", "Anime3"),
    "movie": ("Movie1", "Movie2", "Movie3"),
}


def normalize_title(value: str, branch: str) -> str:
    text = str(value)
    if branch == "tv":
        text = WEBRIP_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_webrip_marked(value: str) -> bool:
    return WEBRIP_SUFFIX.search(str(value)) is not None


def tracker_candidates(entries: list[dict[str, Any]], title: str, branch: str) -> list[dict[str, Any]]:
    libraries = set(LIBRARIES[branch])
    normalized = normalize_title(title, branch).casefold()
    return [
        {
            **entry,
            "normalizedTitle": normalize_title(entry.get("title", ""), branch),
            "webrip": branch == "tv" and is_webrip_marked(entry.get("title", "")),
        }
        for entry in entries
        if entry.get("column") in libraries
        and normalize_title(entry.get("title", ""), branch).casefold() == normalized
    ]


def nas_candidates(library_roots: dict[str, Path], title: str, branch: str) -> list[dict[str, Any]]:
    normalized = normalize_title(title, branch).casefold()
    found: list[dict[str, Any]] = []
    for library in LIBRARIES[branch]:
        root = library_roots.get(library)
        if root is None or not root.is_dir():
            continue
        for directory in root.iterdir():
            if not directory.is_dir() or normalize_title(directory.name, branch).casefold() != normalized:
                continue
            item: dict[str, Any] = {
                "library": library,
                "path": str(directory.resolve()),
                "name": directory.name,
                "webrip": branch == "tv" and is_webrip_marked(directory.name),
            }
            if branch == "tv":
                item["seasons"] = [
                    {
                        "path": str(child.resolve()),
                        "name": child.name,
                        "webrip": is_webrip_marked(child.name),
                    }
                    for child in directory.iterdir()
                    if child.is_dir()
                ]
            else:
                single = directory / f"{title}.mkv"
                stacked = sorted(directory.glob(f"{title}.cd[1-9]*.mkv"), key=lambda value: value.name.casefold())
                movies = [single] if single.is_file() else stacked
                item["mkvs"] = [str(movie.resolve()) for movie in movies if movie.is_file()]
                item["mkv"] = item["mkvs"][0] if len(item["mkvs"]) == 1 else ""
            found.append(item)
    return found


def resolve_target(
    branch: str,
    tracker: list[dict[str, Any]],
    nas: list[dict[str, Any]],
    preferred_library: str,
) -> dict[str, Any]:
    if not tracker and not nas:
        return {"status": "OK", "mode": "create", "library": preferred_library}
    tracker_libraries = {item["column"] for item in tracker}
    nas_libraries = {item["library"] for item in nas}
    if len(tracker) > 1 or len(nas) > 1 or (tracker_libraries and nas_libraries and tracker_libraries != nas_libraries):
        return {
            "status": "NEEDS_USER",
            "code": "LIBRARY_TARGET_AMBIGUOUS",
            "branch": branch,
            "tracker": tracker,
            "nas": nas,
        }
    if not tracker or not nas:
        return {
            "status": "NEEDS_USER",
            "code": "LIBRARY_TARGET_ORPHAN",
            "branch": branch,
            "tracker": tracker,
            "nas": nas,
        }
    selected_tracker, selected_nas = tracker[0], nas[0]
    library = selected_tracker["column"]
    if branch == "movie" and not selected_nas.get("mkv") and not selected_nas.get("mkvs"):
        return {
            "status": "OK",
            "mode": "create",
            "library": library,
            "tracker": selected_tracker,
            "nas": selected_nas,
        }
    webrip = branch == "tv" and bool(
        selected_tracker.get("webrip")
        or selected_nas.get("webrip")
        or any(item.get("webrip") for item in selected_nas.get("seasons", []))
    )
    return {
        "status": "OK",
        "mode": "tv-webrip-to-bdrip" if webrip else "replace",
        "library": library,
        "tracker": selected_tracker,
        "nas": selected_nas,
    }


def build_tv_directory_operations(candidate: dict[str, Any]) -> list[dict[str, str]]:
    source = Path(candidate["path"])
    clean_root = source.with_name(normalize_title(source.name, "tv"))
    operations: list[dict[str, str]] = []
    if source != clean_root:
        operations.append({"kind": "rename" if not clean_root.exists() else "merge", "source": str(source), "target": str(clean_root)})
    for season in candidate.get("seasons", []):
        season_source = Path(season["path"])
        clean_season = clean_root / normalize_title(season_source.name, "tv")
        if is_webrip_marked(season_source.name):
            operations.append({"kind": "rename" if not clean_season.exists() else "merge", "source": str(season_source), "target": str(clean_season)})
    return operations
