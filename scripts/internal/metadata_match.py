"""Pure normalization and candidate selection for archive metadata lookup."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from os.path import commonprefix
from pathlib import Path
from typing import Any

from internal.metadata_client import JsonHttpClient, MetadataHttpError, TmdbClient, TvdbClient, credential_presence


TECHNICAL_TOKEN = re.compile(
    r"\b(?:"
    r"2160p|1080p|720p|576p|480p|\d{3,4}x\d{3,4}p?|"
    r"hevc|avc|h\.?26[45]|x26[45]|hi10p|ma10p|yuv\d+p?\d*|"
    r"flac(?:x\d+)?|\d+flac|aac(?:x\d+)?|ac-?3|e-?ac-?3|dts(?:-?hd(?:\s*ma)?)?|truehd|atmos|opus|pcm|"
    r"web-?dl|webrip|bdrip|bluray|bdmv|remux|10bit|8bit|hdr|dolby\s*vision|dv|"
    r"assx\d+|\d+audio|v\d+"
    r")\b",
    re.IGNORECASE,
)
RELEASE_BRACKET = re.compile(r"^\[[^\]]+\]\s*")
EPISODE_TOKEN = re.compile(
    r"(?:s\d{1,2}e\d{1,3}|\b(?:ep?|e)\s*\d{1,3}(?:\s*-\s*(?:ep?|e)?\s*\d{1,3})?\b|"
    r"第\s*\d{1,3}\s*[话話集]|\[\s*\d{1,3}(?:v\d+)?\s*\])",
    re.IGNORECASE,
)
YEAR_TOKEN = re.compile(r"(?:^|\D)((?:19|20)\d{2})(?:\D|$)")
WINDOWS_NUMBER_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
STACK_TOKEN = re.compile(r"(?:^|[\s._-])cd\d+(?=$|[\s._-])", re.IGNORECASE)
KNOWN_EXTRA = re.compile(
    r"(?:^|[\s._\-\[(])(?:nced|ncop|menu|pv|spot|trailer|featurette|fonts?)\d*(?=$|[\s._\-)\]])",
    re.IGNORECASE,
)
BRACKET_CHUNK = re.compile(r"[\[(]([^\])]+)[\])]")
WEBRIP_SUFFIX = re.compile(r"（s\d+\s*webrip）$|（webrip）$", re.IGNORECASE)


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[\s._·・:：!！?？'\"“”‘’()（）\[\]【】{}<>《》\-–—~～]+", "", text)
    return text


def _display_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).replace("_", " ")
    text = re.sub(r"(?<=\w)\.(?=\w)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -._,，")
    if re.fullmatch(r"\[[^\]]+\]", text):
        text = text[1:-1].strip()
    return text


def _technical_tail(value: str) -> bool:
    return bool(TECHNICAL_TOKEN.search(value) or STACK_TOKEN.search(value))


def _clean_title_candidate(value: str, *, allow_numbered_segment: bool = False) -> str:
    text = unicodedata.normalize("NFKC", value).strip()
    text = WEBRIP_SUFFIX.sub("", text).strip()
    text = WINDOWS_NUMBER_SUFFIX.sub("", text).strip()

    leading = re.match(r"^\[([^\]]+)\]\s*(.*)$", text)
    if leading and leading.group(2) and not leading.group(2).lstrip().startswith("["):
        text = leading.group(2).strip()

    stack = STACK_TOKEN.search(text)
    if stack:
        text = text[: stack.start()]

    chunks = list(BRACKET_CHUNK.finditer(text))
    for chunk in chunks:
        content = chunk.group(1).strip()
        searchable_content = content.replace("_", " ")
        tail = text[chunk.end() :]
        is_year_boundary = bool(re.fullmatch(r"(?:19|20)\d{2}", content) and _technical_tail(tail))
        if (
            EPISODE_TOKEN.fullmatch(f"[{content}]")
            or TECHNICAL_TOKEN.search(searchable_content)
            or KNOWN_EXTRA.search(f" {content} ")
            or is_year_boundary
        ):
            text = text[: chunk.start()]
            break

    year = YEAR_TOKEN.search(text)
    if year and _technical_tail(text[year.end() :]):
        text = text[: year.start(1)]

    episode = EPISODE_TOKEN.search(text)
    if episode:
        text = text[: episode.start()]

    if allow_numbered_segment:
        segment = re.search(r"\s+\d{1,3}\s*[-–—]\s+", text)
        if segment:
            text = text[: segment.start()]

    technical = TECHNICAL_TOKEN.search(text)
    if technical:
        text = text[: technical.start()]

    return _display_title(text)


def _eligible_title_videos(work: Path, files: list[Path]) -> list[Path]:
    selected = []
    for path in files:
        if path.suffix.casefold() not in {".mkv", ".mp4"}:
            continue
        try:
            relative = path.relative_to(work)
        except ValueError:
            continue
        if any(part.casefold() == "bdmv" for part in relative.parts[:-1]) or KNOWN_EXTRA.search(path.stem):
            continue
        selected.append(path)
    return sorted(selected, key=lambda path: path.as_posix().casefold())


def _common_media_candidate(candidates: list[str]) -> str:
    unique: dict[str, str] = {}
    for candidate in candidates:
        folded = normalize_title(candidate)
        if folded:
            unique.setdefault(folded, candidate)
    if len(unique) == 1:
        return next(iter(unique.values()))
    if not unique:
        return ""
    prefix = commonprefix(list(unique.values())).rstrip(" -._:：")
    if prefix and not prefix[-1].isalnum() and not ("\u4e00" <= prefix[-1] <= "\u9fff"):
        prefix = prefix[:-1].rstrip()
    return prefix if len(normalize_title(prefix)) >= 4 else ""


def derive_metadata_query(work: Path, files: list[Path], *, explicit_query: str = "") -> dict[str, Any]:
    if explicit_query.strip():
        query = explicit_query.strip()
        return {"query": query, "source": "explicit-query", "candidates": [query], "issues": []}

    directory_raw = WEBRIP_SUFFIX.sub("", unicodedata.normalize("NFKC", work.name)).strip()
    directory = _clean_title_candidate(directory_raw)
    directory_clean = bool(directory and normalize_title(directory) == normalize_title(directory_raw))

    title_videos = _eligible_title_videos(work, files)
    media_values = [
        _clean_title_candidate(path.stem, allow_numbered_segment=len(title_videos) > 1)
        for path in title_videos
    ]
    media_values = [value for value in media_values if value]
    media = _common_media_candidate(media_values)

    candidates = sorted(
        {value for value in (media, directory) if value},
        key=lambda value: (value.casefold(), value),
    )
    if directory_clean:
        return {"query": directory, "source": "clean-directory", "candidates": candidates or [directory], "issues": []}
    if media and directory and normalize_title(media) != normalize_title(directory):
        return {
            "query": "",
            "source": "ambiguous-local-title",
            "candidates": candidates,
            "issues": [{"code": "METADATA_QUERY_REQUIRED", "candidates": candidates}],
        }
    if media:
        return {"query": media, "source": "media-common-title", "candidates": [media], "issues": []}
    if directory:
        return {"query": directory, "source": "cleaned-directory", "candidates": [directory], "issues": []}
    return {
        "query": "",
        "source": "unresolved",
        "candidates": [],
        "issues": [{"code": "METADATA_QUERY_REQUIRED", "candidates": []}],
    }


def query_from_work(work: Path, files: list[Path]) -> str:
    """Compatibility wrapper for callers that only need the selected query."""

    return str(derive_metadata_query(work, files).get("query") or work.name)


def _candidate_titles(item: dict[str, Any], media_type: str) -> list[str]:
    fields = ["name", "original_name"] if media_type == "tv" else ["title", "original_title"]
    return [str(item.get(field) or "").strip() for field in fields if str(item.get(field) or "").strip()]


def candidate_summary(item: dict[str, Any], media_type: str) -> dict[str, Any]:
    date = str(item.get("first_air_date") if media_type == "tv" else item.get("release_date") or "")
    titles = _candidate_titles(item, media_type)
    return {
        "provider": "tmdb",
        "mediaType": media_type,
        "id": int(item["id"]),
        "title": titles[0] if titles else "",
        "originalTitle": titles[1] if len(titles) > 1 else titles[0] if titles else "",
        "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
        "originalLanguage": item.get("original_language"),
    }


def rank_candidates(query: str, items: list[dict[str, Any]], media_type: str, *, year: int | None = None) -> list[dict[str, Any]]:
    folded = normalize_title(query)
    ranked = []
    for index, item in enumerate(items):
        try:
            summary = candidate_summary(item, media_type)
        except (KeyError, TypeError, ValueError):
            continue
        names = [normalize_title(value) for value in _candidate_titles(item, media_type)]
        exact = folded in names
        contains = bool(folded) and any(folded in value or value in folded for value in names)
        year_match = year is not None and summary["year"] == year
        score = (100 if exact else 60 if contains else 0) + (20 if year_match else 0) + max(0, 10 - index)
        ranked.append({**summary, "score": score, "exactTitle": exact, "yearMatch": year_match})
    return sorted(ranked, key=lambda value: (-value["score"], value.get("year") or 9999, value["id"]))[:5]


def unique_candidate(candidates: list[dict[str, Any]], *, explicit_id: int | None = None) -> dict[str, Any] | None:
    if explicit_id is not None:
        return next((item for item in candidates if item["id"] == explicit_id), None)
    if not candidates:
        return None
    exact = [item for item in candidates if item["exactTitle"]]
    if len(exact) == 1:
        return exact[0]
    exact_year = [item for item in exact if item["yearMatch"]]
    if len(exact_year) == 1:
        return exact_year[0]
    return None


def normalize_tmdb_details(details: dict[str, Any], media_type: str) -> dict[str, Any]:
    title = str((details.get("name") if media_type == "tv" else details.get("title")) or "")
    original = str((details.get("original_name") if media_type == "tv" else details.get("original_title")) or title)
    date = str(details.get("first_air_date") if media_type == "tv" else details.get("release_date") or "")
    alternatives = details.get("alternative_titles", {})
    raw_titles = alternatives.get("results" if media_type == "tv" else "titles", []) if isinstance(alternatives, dict) else []
    aliases = sorted({str(item.get("title") or "").strip() for item in raw_titles if isinstance(item, dict) and item.get("title")})
    external = details.get("external_ids", {}) if isinstance(details.get("external_ids"), dict) else {}
    seasons = []
    for item in details.get("seasons", []) if media_type == "tv" else []:
        if not isinstance(item, dict) or item.get("season_number") is None:
            continue
        seasons.append({
            "season": int(item["season_number"]),
            "episodeCount": int(item.get("episode_count") or 0),
            "name": str(item.get("name") or ""),
            "airDate": item.get("air_date"),
        })
    return {
        "provider": "tmdb",
        "mediaType": media_type,
        "id": int(details["id"]),
        "title": title,
        "originalTitle": original,
        "aliases": aliases,
        "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
        "tvdbId": int(external["tvdb_id"]) if str(external.get("tvdb_id") or "").isdigit() else None,
        "seasons": sorted(seasons, key=lambda value: value["season"]),
        "runtimeMinutes": details.get("runtime"),
    }


def tmdb_episode_summary(value: dict[str, Any]) -> list[dict[str, Any]]:
    episodes = []
    for item in value.get("episodes", []):
        if not isinstance(item, dict) or item.get("season_number") is None or item.get("episode_number") is None:
            continue
        episodes.append({
            "season": int(item["season_number"]),
            "episode": int(item["episode_number"]),
            "name": str(item.get("name") or ""),
            "airDate": item.get("air_date"),
        })
    return episodes


def metadata_issue(code: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {"code": code, "metadata": metadata}


def local_seasons(work: Path, files: list[Path]) -> list[int]:
    seasons: set[int] = set()
    for path in files:
        try:
            relative = path.relative_to(work)
        except ValueError:
            continue
        season = None
        for part in relative.parts[:-1]:
            match = re.fullmatch(r"(?:s|season\s*)(\d{1,2})", unicodedata.normalize("NFKC", part).strip(), re.IGNORECASE)
            if match:
                season = int(match.group(1))
        if season is None:
            match = re.search(r"s(\d{1,2})e\d{1,3}", unicodedata.normalize("NFKC", path.stem), re.IGNORECASE)
            season = int(match.group(1)) if match else 1
        seasons.add(season)
    return sorted(seasons or {1})


def _metadata_options(config: dict[str, Any], supplied: Any) -> dict[str, Any]:
    base = config.get("metadata", {}) if isinstance(config.get("metadata"), dict) else {}
    requested = supplied if isinstance(supplied, dict) else {}
    try:
        timeout = max(1.0, min(float(base.get("timeoutSeconds", 10)), 60.0))
    except (TypeError, ValueError):
        timeout = -1.0
    return {
        "enabled": bool(requested.get("enabled", base.get("enabled", False))),
        "mode": str(requested.get("mode", base.get("mode", "auto"))).casefold(),
        "query": str(requested.get("query") or "").strip(),
        "tmdbId": requested.get("tmdb_id"),
        "tvdbId": requested.get("tvdb_id"),
        "mediaType": str(requested.get("tmdb_type") or "").casefold(),
        "language": str(requested.get("language", base.get("language", "zh-CN"))),
        "episodeOrder": str(requested.get("episode_order", base.get("episodeOrder", "tmdb"))).casefold(),
        "year": requested.get("year"),
        "seasonBindings": requested.get("season_bindings") if isinstance(requested.get("season_bindings"), dict) else {},
        "proxy": base.get("proxy", ""),
        "timeout": timeout,
        "primary": str(base.get("primary", "tmdb")).casefold(),
        "secondary": str(base.get("secondary", "tvdb")).casefold(),
    }


def _safe_int(value: Any) -> int | None:
    try:
        selected = int(value)
        return selected if selected >= 0 else None
    except (TypeError, ValueError):
        return None


def _public_proxy(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if not parsed.hostname:
        return "configured"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port_value = parsed.port
    except ValueError:
        return "configured"
    port = f":{port_value}" if port_value else ""
    return f"{parsed.scheme or 'http'}://{host}{port}"


def _tvdb_episode_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        season = _safe_int(item.get("seasonNumber"))
        episode = _safe_int(item.get("number"))
        if season is None or episode is None:
            continue
        result.append({
            "season": season,
            "episode": episode,
            "absolute": _safe_int(item.get("absoluteNumber")),
            "name": str(item.get("name") or ""),
            "airDate": item.get("aired"),
        })
    return result


def _single_tmdb_series_id(bindings: dict[str, Any], selected_id: int) -> int:
    """Reject legacy per-season series mixing at the Archive trust boundary."""

    series_ids = {selected_id}
    for binding in bindings.values():
        if not isinstance(binding, dict):
            raise ValueError("invalid season binding")
        candidate = _safe_int(binding.get("tmdb_id"))
        if candidate is not None:
            series_ids.add(candidate)
        provider = str(binding.get("source") or "tmdb").casefold()
        if provider != "tmdb":
            raise ValueError("mixed metadata providers")
    if len(series_ids) != 1:
        raise ValueError("multiple TMDB series")
    return selected_id


def _path_season_episode(path: Path, work: Path) -> tuple[int, int] | None:
    try:
        relative = path.relative_to(work)
    except ValueError:
        return None
    normalized_stem = unicodedata.normalize("NFKC", path.stem)
    pair = re.search(r"s(\d{1,2})e(\d{1,3})", normalized_stem, re.IGNORECASE)
    if pair:
        return int(pair.group(1)), int(pair.group(2))
    season = 1
    for part in relative.parts[:-1]:
        season_match = re.fullmatch(r"(?:s|season\s*)(\d{1,2})", unicodedata.normalize("NFKC", part).strip(), re.IGNORECASE)
        if season_match:
            season = int(season_match.group(1))
    patterns = (
        re.compile(r"第\s*(\d{1,3})\s*[话話集]", re.IGNORECASE),
        re.compile(r"(?:^|[\s._\-\[(#])(?:ep?|e|#)?\s*(\d{1,3})(?:v\d+)?(?=$|[\s._\-)\]])", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(normalized_stem)
        if match:
            episode = int(match.group(1))
            if episode > 0 and episode not in {264, 265, 480, 576, 720, 1080, 2160}:
                return season, episode
    return None


def suggest_episode_map(work: Path, files: list[Path], episodes: list[dict[str, Any]]) -> dict[str, str]:
    available = {
        (int(item.get("localSeason", item.get("season", -1))), int(item.get("episode", -1)))
        for item in episodes
        if item.get("episode") is not None
    }
    mapping: dict[str, str] = {}
    for path in files:
        if path.suffix.casefold() not in {".mkv", ".mp4", ".ass", ".ssa"}:
            continue
        parsed = _path_season_episode(path, work)
        if parsed is None or parsed not in available:
            continue
        season, episode = parsed
        mapping[path.relative_to(work).as_posix()] = f"S{season:02d}E{episode:02d}"
    return dict(sorted(mapping.items(), key=lambda item: item[0].casefold()))


def inspect_metadata(
    work: Path,
    config: dict[str, Any],
    route: dict[str, Any],
    files: list[Path],
    supplied: Any,
    *,
    local_season_numbers: list[int] | None = None,
    http_factory: Any = JsonHttpClient,
) -> dict[str, Any]:
    """Return normalized suggestions only; raw responses and credentials stay private."""

    options = _metadata_options(config, supplied)
    branch = "tv" if route.get("branch") == "anime" else "movie" if route.get("branch") == "movie" else ""
    media_type = options["mediaType"] or branch
    query_selection = derive_metadata_query(work, files, explicit_query=options["query"])
    query = str(query_selection["query"])
    base = {
        "status": "OFF" if not options["enabled"] or options["mode"] == "off" else "PENDING",
        "mode": options["mode"],
        "query": query,
        "querySource": query_selection["source"],
        "queryCandidates": query_selection["candidates"],
        "mediaType": media_type,
        "language": options["language"],
        "episodeOrder": options["episodeOrder"],
        "proxy": _public_proxy(options["proxy"]),
        "credentials": credential_presence(),
        "candidates": [],
        "selected": None,
        "episodes": [],
        "tvdb": {"status": "NOT_USED"},
        "suggestedDecisions": {},
        "issues": [],
        "warnings": [],
    }
    if base["status"] == "OFF":
        return base
    if media_type not in {"tv", "movie"} or options["mode"] not in {"auto", "required"}:
        base["status"] = "NEEDS_USER"
        base["issues"].append({"code": "METADATA_OPTIONS_INVALID", "mediaType": media_type, "mode": options["mode"]})
        return base
    proxy = str(options["proxy"] or "").strip()
    parsed_proxy = urllib.parse.urlsplit(proxy) if proxy else None
    try:
        proxy_valid = not proxy or (parsed_proxy is not None and parsed_proxy.scheme in {"http", "https"} and bool(parsed_proxy.hostname) and parsed_proxy.port is not None)
    except ValueError:
        proxy_valid = False
    if options["primary"] != "tmdb" or options["secondary"] != "tvdb" or options["timeout"] < 0 or not proxy_valid:
        base["status"] = "NEEDS_USER"
        base["issues"].append({"code": "METADATA_CONFIG_INVALID"})
        return base

    explicit_tmdb = _safe_int(options["tmdbId"])
    if explicit_tmdb is None and query_selection["issues"]:
        base["status"] = "NEEDS_USER"
        base["issues"].extend(query_selection["issues"])
        return base

    http = http_factory(proxy=proxy or None, timeout=options["timeout"], retries=2)
    try:
        tmdb = TmdbClient(http)
        if explicit_tmdb is not None:
            details = tmdb.details(media_type, explicit_tmdb, language=options["language"])
            candidates = [{**candidate_summary(details, media_type), "score": 1000, "exactTitle": True, "yearMatch": True}]
            selected_candidate = candidates[0]
        else:
            searched = tmdb.search(media_type, query, language=options["language"], year=_safe_int(options["year"]))
            candidates = rank_candidates(query, searched, media_type, year=_safe_int(options["year"]))
            selected_candidate = unique_candidate(candidates)
            details = tmdb.details(media_type, selected_candidate["id"], language=options["language"]) if selected_candidate else None
    except MetadataHttpError as exc:
        base["status"] = "NEEDS_USER" if options["mode"] == "required" or not exc.transient else "WARNING"
        payload = {"code": exc.code, "provider": "tmdb", "detail": str(exc)}
        (base["issues"] if base["status"] == "NEEDS_USER" else base["warnings"]).append(payload)
        return base

    base["candidates"] = candidates
    if not selected_candidate or not isinstance(details, dict):
        base["status"] = "NEEDS_USER"
        base["issues"].append({"code": "METADATA_CANDIDATE_REQUIRED", "provider": "tmdb", "query": query, "candidates": candidates})
        return base

    selected = normalize_tmdb_details(details, media_type)
    base["selected"] = selected
    base["suggestedDecisions"] = {
        "title": selected["title"] or selected["originalTitle"],
        "metadata": {
            "mode": options["mode"],
            "query": query,
            "tmdb_id": selected["id"],
            "tmdb_type": media_type,
            "tvdb_id": _safe_int(options["tvdbId"]) or selected.get("tvdbId"),
            "language": options["language"],
            "episode_order": options["episodeOrder"],
            **({"season_bindings": options["seasonBindings"]} if options["seasonBindings"] else {}),
        },
    }

    if media_type == "tv":
        requested_seasons = (
            sorted(set(local_season_numbers))
            if local_season_numbers
            else local_seasons(work, files)
        )
        if options["episodeOrder"] == "tmdb":
            episodes = []
            bindings = options["seasonBindings"]
            try:
                _single_tmdb_series_id(bindings, selected["id"])
            except ValueError:
                base["status"] = "NEEDS_USER"
                base["issues"].append(
                    {
                        "code": "REMOTE_SERIES_SPLIT_REQUIRED",
                        "provider": "tmdb",
                        "detail": "全部季度与 S00 必须属于同一个 TMDB 系列；否则请整体切换 TVDB 或拆分作品任务",
                    }
                )
                return base
            available_seasons = {
                int(item["season"])
                for item in selected.get("seasons", [])
                if int(item.get("episodeCount") or 0) > 0
            }
            missing_tmdb = sorted(set(requested_seasons) - available_seasons)
            tvdb_fallback_id = _safe_int(options["tvdbId"]) or selected.get("tvdbId")
            if missing_tmdb and tvdb_fallback_id:
                options["episodeOrder"] = "tvdb-aired"
                base["episodeOrder"] = "tvdb-aired"
                base["warnings"].append(
                    {
                        "code": "TMDB_SEASON_COVERAGE_INCOMPLETE",
                        "missingSeasons": missing_tmdb,
                        "fallback": "tvdb-aired",
                    }
                )
                base["suggestedDecisions"]["metadata"]["episode_order"] = "tvdb-aired"
                base["suggestedDecisions"]["metadata"]["remote_source"] = "tvdb"
            elif missing_tmdb:
                base["status"] = "NEEDS_USER"
                base["issues"].append(
                    {
                        "code": "REMOTE_SERIES_SPLIT_REQUIRED",
                        "provider": "tmdb",
                        "missingSeasons": missing_tmdb,
                        "detail": "TMDB 无法覆盖全部季度且没有可用 TVDB 系列，请补充 TVDB 或拆分作品任务",
                    }
                )
                return base
            try:
                for local_season in requested_seasons if not missing_tmdb else []:
                    binding = bindings.get(f"S{local_season}") or bindings.get(str(local_season)) or {}
                    series_id = selected["id"]
                    remote_season = _safe_int(binding.get("tmdb_season"))
                    remote_season = local_season if remote_season is None else remote_season
                    season_value = tmdb.season(series_id, remote_season, language=options["language"])
                    for item in tmdb_episode_summary(season_value):
                        episodes.append({**item, "localSeason": local_season, "remoteSeriesId": series_id})
            except MetadataHttpError as exc:
                payload = {"code": exc.code, "provider": "tmdb", "detail": str(exc), "scope": "season"}
                if options["mode"] == "required":
                    base["status"] = "NEEDS_USER"
                    base["issues"].append(payload)
                    return base
                base["warnings"].append(payload)
            base["episodes"] = episodes
        elif not options["episodeOrder"].startswith("tvdb-"):
            base["status"] = "NEEDS_USER"
            base["issues"].append({"code": "METADATA_EPISODE_ORDER_INVALID", "value": options["episodeOrder"]})
            return base

    tvdb_id = _safe_int(options["tvdbId"]) or selected.get("tvdbId")
    tvdb_required = options["episodeOrder"].startswith("tvdb-")
    if tvdb_id:
        try:
            tvdb = TvdbClient(http)
            series = tvdb.series(tvdb_id) if media_type == "tv" else tvdb.movie(tvdb_id)
            tvdb_name = str(series.get("name") or "") if isinstance(series, dict) else ""
            selected_names = {normalize_title(value) for value in (selected["title"], selected["originalTitle"], *selected["aliases"]) if value}
            name_matches = not tvdb_name or normalize_title(tvdb_name) in selected_names
            base["tvdb"] = {
                "status": "MATCHED" if name_matches else "MISMATCH",
                "id": tvdb_id,
                "name": tvdb_name,
            }
            if not name_matches:
                base["warnings"].append({"code": "TVDB_CROSSCHECK_MISMATCH", "provider": "tvdb", "id": tvdb_id, "name": tvdb_name})
            if tvdb_required:
                order = options["episodeOrder"].removeprefix("tvdb-")
                base["episodes"] = _tvdb_episode_summary(tvdb.episodes(tvdb_id, order))
                requested = set(requested_seasons if media_type == "tv" else [])
                covered = {int(item["season"]) for item in base["episodes"]}
                missing = sorted(requested - covered)
                if missing:
                    base["status"] = "NEEDS_USER"
                    base["issues"].append(
                        {
                            "code": "REMOTE_SERIES_SPLIT_REQUIRED",
                            "provider": "tvdb",
                            "missingSeasons": missing,
                            "detail": "TVDB 也无法用单一系列覆盖全部季度，请拆分为多个作品任务",
                        }
                    )
                    return base
        except MetadataHttpError as exc:
            payload = {"code": exc.code, "provider": "tvdb", "detail": str(exc)}
            if tvdb_required:
                base["status"] = "NEEDS_USER"
                base["issues"].append(payload)
                return base
            base["tvdb"] = {"status": "WARNING", "id": tvdb_id}
            base["warnings"].append(payload)
    elif tvdb_required:
        base["status"] = "NEEDS_USER"
        base["issues"].append({"code": "TVDB_ID_REQUIRED", "provider": "tvdb"})
        return base

    episode_map = suggest_episode_map(work, files, base["episodes"]) if media_type == "tv" else {}
    if episode_map:
        base["suggestedDecisions"]["episode_map"] = episode_map
    base["status"] = "MATCHED"
    return base
