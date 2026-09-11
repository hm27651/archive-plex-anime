"""Conservative, name-only draft grouping. No metadata lookup or filesystem writes."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Any


SEASON = re.compile(
    r"(?:^|[\s._\-\[（(])(?:s(?:eason)?[\s._-]*(\d{1,2})|第([零一二三四五六七八九十两\d]+)季)(?=$|[\s._\-\]）)])",
    re.I,
)
EXTRAS = re.compile(r"(?:sps?|specials?|ovas?|oads?|extras?|fonts?|scans?|cds?|菜单|特典|字体|扫图)", re.I)
MOVIE = re.compile(r"(?:movies?|films?|剧场版|劇場版)(?:[\s._-].*)?", re.I)
GENERIC = re.compile(r"(?:part|disc|disk|bd|cd)[\s._-]*\d+|第[一二三四五六七八九十\d]+部分|正片|视频", re.I)
RELEASE_GROUP = re.compile(r"^\[([^\]]*(?:\bstudio\b|\bkissaten\b|\bsubs?\b|字幕组)[^\]]*)\]\s*", re.I)
TECH_TAG = re.compile(r"\[([\w .+_-]+)\]")
TECH_CONTENT = re.compile(r"(?:(?:ma)?\d+(?:p|i|bit)|x26[45]|h[ .]?26[45]|hevc|avc|flac|aac|bd(?:rip)?|dts|truehd|[ .+_-])+", re.I)


def clean_title(name: str) -> str:
    name = RELEASE_GROUP.sub("", name.strip())
    name = TECH_TAG.sub(lambda m: "" if TECH_CONTENT.fullmatch(m[1]) else m[0], name)
    name = re.sub(r"(?:[ ._-]+(?:\d{3,4}[pi]|[xh]26[45]|hevc|flac|aac|(?:8|10)bit))+$", "", name.strip(), flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" ._-")


def season_name(name: str) -> tuple[str, int | None]:
    cleaned = clean_title(name)
    match = SEASON.search(cleaned)
    if not match:
        return cleaned, None
    value = match[1] or match[2]
    if value.isdecimal():
        number = int(value)
    else:
        digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
        digits["两"] = 2
        if "十" in value:
            tens, units = value.split("十", 1)
            number = digits.get(tens, 1) * 10 + digits.get(units, 0)
        else:
            number = digits.get(value)
    return (cleaned[:match.start()] + " " + cleaned[match.end():]).strip(" ._-[]()（）"), number


def season_hint(path: str) -> int:
    parts = PurePosixPath(path).parts
    for part in reversed(parts[:-1]):
        _, number = season_name(part)
        if number is not None:
            return number
        if EXTRAS.fullmatch(part):
            return 0
    match = re.search(r"(?:^|[ ._-])S(\d{1,2})E\d+", parts[-1], re.I)
    return int(match[1]) if match else 1


def group_source_scope(scope: dict[str, Any], branch: str) -> dict[str, Any]:
    parent = PurePosixPath(scope["source_relative_path"])
    title = clean_title(parent.name)
    rows = [{**row, "season": season_hint(row["path"]) if branch == "tv" else None} for row in scope["files"]]
    source_scope = {**scope, "files": rows}
    directories: dict[str, list[dict[str, Any]]] = {}
    root_rows, extras = [], []
    for row in rows:
        parts = PurePosixPath(row["path"]).parts
        if len(parts) == 1:
            root_rows.append(row)
        elif EXTRAS.fullmatch(parts[0]):
            extras.append(row)
        else:
            directories.setdefault(parts[0], []).append(row)

    def draft(label, files, kind=branch, relative=parent):
        return {"title": label, "relative_path": str(relative), "branch": kind, "files": files}

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    labels: dict[tuple[str, str], str] = {}
    raw_groups, seasons = [], []
    season_counts: Counter = Counter()
    ambiguous = bool(root_rows and directories)
    for directory, files in directories.items():
        base, season = season_name(directory)
        kind = "movie" if branch == "tv" and MOVIE.fullmatch(clean_title(directory)) else branch
        if branch == "tv" and season is not None:
            label = base or title
            seasons.append(season)
        else:
            label = clean_title(directory)
            ambiguous |= bool(GENERIC.fullmatch(label) or MOVIE.fullmatch(label))
        key = (kind, label.casefold())
        if branch == "tv" and season is not None:
            season_counts[(key, season)] += 1
        buckets.setdefault(key, []).extend(files)
        labels[key] = label
        raw_groups.append(draft(label or title, [
            {**row, "path": str(PurePosixPath(*PurePosixPath(row["path"]).parts[1:]))}
            for row in files
        ], kind, parent / directory))
    duplicates = sorted({number for (_, number), count in season_counts.items() if count > 1})
    ambiguous |= bool(duplicates)
    groups = [draft(labels[key], files, key[0]) for key, files in buckets.items()]
    if root_rows:
        groups.insert(0, draft(title, root_rows))
        raw_groups.insert(0, draft(title, root_rows))
    if len(groups) <= 1:
        groups = [draft(groups[0]["title"] if groups else title, rows, groups[0]["branch"] if groups else branch)]
    # Movie cdN parts are one movie, not separate titles.
    if branch == "movie" and directories and all(re.fullmatch(r"cd[ ._-]*\d+", d, re.I) for d in directories):
        groups, ambiguous = [draft(title, rows)], False
    mixed = len({group["branch"] for group in groups}) > 1
    mode = "confirm" if ambiguous else "single" if len(groups) == 1 else "split"
    reason = (
        "发现同一季的多个版本，请选择要处理的文件。" if duplicates else
        "目录名称不足以确定归属，请确认分组。" if mode == "confirm" else
        "按季度目录归为同一部 TV 作品，季集对应稍后确认。" if mode == "single" and seasons else
        "按文件夹名称建议分别处理，请确认。" if mode == "split" else
        "默认作为一部作品处理。"
    )
    for collection in (groups, raw_groups):
        for index, item in enumerate(collection, 1):
            item["id"] = f"work-{index}"
    return {
        "source_scope": source_scope,
        "split_suggestions": groups if mode == "split" else [],
        "source_grouping": {
            "mode": mode, "reason": reason, "title": groups[0]["title"] if len(groups) == 1 else title,
            "seasons": sorted(set(seasons)), "duplicate_seasons": duplicates,
            "groups": groups, "directory_groups": raw_groups,
            "merge_allowed": not mixed,
            "unassigned_count": len(extras) if len(groups) > 1 else 0,
        },
        "target_directory_suggestion": {"relative_path": parent.name, "title": title},
    }
