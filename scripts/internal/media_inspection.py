"""Fast MediaInfo inventory and exact MKVToolNix track-ID mapping."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from common import decode_output

class MediaInspectionError(RuntimeError):
    def __init__(self, code: str, message: str, category: str = "FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.category = category


def _bool(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"yes", "true", "1"}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _language(value: Any) -> str:
    value = str(value or "und").strip().casefold()
    return {"ja": "jpn", "zh": "chi", "en": "eng"}.get(value, value or "und")


def _type(value: Any) -> str | None:
    return {"video": "video", "audio": "audio", "text": "subtitles"}.get(str(value or "").casefold())


def _track_key(track: dict[str, Any]) -> str:
    if track.get("uid") not in {None, ""}:
        return f"uid:{track['uid']}"
    fields = (
        track.get("type"), track.get("codecId"), track.get("language"), track.get("channels"),
        track.get("channelLayout"), track.get("sampleRate"), track.get("bitDepth"),
        track.get("width"), track.get("height"), track.get("title"), track.get("streamOrder"),
    )
    return "feature:" + "|".join("" if value is None else str(value) for value in fields)


def run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        raise MediaInspectionError("MEDIA_TOOL_FAILED", decode_output(completed.stderr))
    try:
        return json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaInspectionError("MEDIA_JSON_INVALID", f"Invalid JSON from {command[0]}: {exc}") from exc


def read_mediainfo_json(path: Path, executable: str) -> dict[str, Any]:
    return run_json([executable, "--Output=JSON", "--", str(path)])


def read_mkvmerge_json(path: Path, executable: str) -> dict[str, Any]:
    return run_json([executable, "-J", str(path)])


def normalize_mediainfo(payload: dict[str, Any]) -> dict[str, Any]:
    raw_tracks = payload.get("media", {}).get("track", [])
    tracks: list[dict[str, Any]] = []
    chapter_count = 0
    for raw in raw_tracks:
        kind = _type(raw.get("@type"))
        if raw.get("@type") == "Menu":
            chapter_count = len(raw.get("extra", {}) or {})
            continue
        if not kind:
            continue
        track = {
            "type": kind,
            "codecId": raw.get("CodecID"),
            "format": raw.get("Format"),
            "uid": str(raw.get("UniqueID")) if raw.get("UniqueID") not in {None, ""} else None,
            "streamOrder": _int(raw.get("StreamOrder")),
            "language": _language(raw.get("Language")),
            "title": str(raw.get("Title") or ""),
            "default": _bool(raw.get("Default")),
            "forced": _bool(raw.get("Forced")),
            "duration": _float(raw.get("Duration")),
            "channels": _int(raw.get("Channels")),
            "channelLayout": raw.get("ChannelLayout") or raw.get("ChannelPositions"),
            "sampleRate": _int(raw.get("SamplingRate")),
            "bitDepth": _int(raw.get("BitDepth")),
            "width": _int(raw.get("Width")),
            "height": _int(raw.get("Height")),
            "frameRate": _float(raw.get("FrameRate")),
        }
        track["trackKey"] = _track_key(track)
        tracks.append(track)
    return {"tracks": tracks, "chapters": {"present": chapter_count > 0, "count": chapter_count}}


def normalize_mkvmerge(payload: dict[str, Any]) -> dict[str, Any]:
    tracks = []
    for raw in payload.get("tracks", []):
        props = raw.get("properties", {})
        kind = "subtitles" if raw.get("type") == "subtitles" else raw.get("type")
        tracks.append(
            {
                "id": int(raw["id"]),
                "type": kind,
                "codecId": props.get("codec_id"),
                "format": raw.get("codec"),
                "uid": str(props.get("uid")) if props.get("uid") not in {None, ""} else None,
                "language": _language(props.get("language")),
                "title": str(props.get("track_name") or ""),
                "default": bool(props.get("default_track")),
                "forced": bool(props.get("forced_track")),
                "duration": _float(props.get("tag_duration")),
                "channels": _int(props.get("audio_channels")),
                "channelLayout": props.get("audio_channels_layout"),
                "sampleRate": _int(props.get("audio_sampling_frequency")),
                "bitDepth": _int(props.get("audio_bits_per_sample")),
                "width": _int(props.get("pixel_dimensions", "x").split("x", 1)[0]) if props.get("pixel_dimensions") else None,
                "height": _int(props.get("pixel_dimensions", "x").split("x", 1)[1]) if props.get("pixel_dimensions") and "x" in props.get("pixel_dimensions") else None,
                "order": len(tracks),
            }
        )
    return {"tracks": tracks}


def _matches(media: dict[str, Any], mux: dict[str, Any]) -> bool:
    if media.get("type") != mux.get("type"):
        return False
    left_codec = str(media.get("codecId") or media.get("format") or "").casefold()
    right_codec = str(mux.get("codecId") or mux.get("format") or "").casefold()
    return not left_codec or not right_codec or left_codec == right_codec or left_codec in right_codec or right_codec in left_codec


def _score(media: dict[str, Any], mux: dict[str, Any]) -> int:
    score = 0
    for key, weight in (("language", 8), ("channels", 8), ("channelLayout", 6), ("sampleRate", 5), ("bitDepth", 4), ("width", 5), ("height", 5), ("title", 3), ("default", 2), ("forced", 2)):
        left, right = media.get(key), mux.get(key)
        if left not in {None, ""} and right not in {None, ""} and str(left).casefold() == str(right).casefold():
            score += weight
    if media.get("streamOrder") == mux.get("order"):
        score += 1
    return score


def map_selected_tracks(media_tracks: list[dict[str, Any]], mux_tracks: list[dict[str, Any]], selected_keys: list[str] | None = None) -> dict[str, int]:
    selected = [track for track in media_tracks if selected_keys is None or track.get("trackKey") in selected_keys]
    result: dict[str, int] = {}
    used: set[int] = set()
    for media in selected:
        candidates = [track for track in mux_tracks if track["id"] not in used and _matches(media, track)]
        uid = media.get("uid")
        if uid:
            exact = [track for track in candidates if track.get("uid") == uid]
            if len(exact) == 1:
                candidates = exact
        if len(candidates) != 1:
            scored = sorted((( _score(media, item), item) for item in candidates), key=lambda pair: pair[0], reverse=True)
            if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
                raise MediaInspectionError("TRACK_MAPPING_AMBIGUOUS", f"Cannot uniquely map {media.get('trackKey')}", "DECISION_REQUIRED")
            candidates = [scored[0][1]]
        chosen = candidates[0]
        result[str(media["trackKey"])] = int(chosen["id"])
        used.add(int(chosen["id"]))
    return result
