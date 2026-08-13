"""Internal Movie original-disc audio inventory, matching, sync, and mux logic."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import statistics
import subprocess
import sys
from array import array
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from common import decode_output

REPORT_VERSION = 1
KNOWN_LANG_UNDEFINED = {"", "und", "unknown", "unk", "none"}
LOSSLESS_CODECS = {
    "flac",
    "truehd",
    "mlp",
    "pcm_bluray",
    "pcm_s16be",
    "pcm_s16le",
    "pcm_s24be",
    "pcm_s24le",
    "pcm_s32be",
    "pcm_s32le",
}


class HelperError(RuntimeError):
    pass


def run_capture(command: list[str], *, binary: bool = False) -> bytes | str:
    process = subprocess.run(command, capture_output=True, check=False)
    if process.returncode != 0:
        stderr = decode_output(process.stderr).strip()
        stdout = decode_output(process.stdout).strip()
        detail = stderr or stdout or f"exit code {process.returncode}"
        raise HelperError(f"Command failed: {command[0]}: {detail}")
    if binary:
        return process.stdout
    return decode_output(process.stdout)


def run_json(command: list[str]) -> dict[str, Any]:
    text = str(run_capture(command))
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HelperError(f"Command returned invalid JSON: {command[0]}: {exc}") from exc


def parse_duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    except (TypeError, ValueError):
        pass
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", str(value).strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def clean_language(value: Any) -> str:
    language = str(value or "und").strip().lower()
    aliases = {"ja": "jpn", "jp": "jpn", "en": "eng", "zh": "chi", "zho": "chi"}
    return aliases.get(language, language or "und")


def language_known(value: str) -> bool:
    return value not in KNOWN_LANG_UNDEFINED


def clean_layout(value: Any) -> str:
    layout = str(value or "").strip().lower().replace(" ", "")
    layout = layout.replace("(side)", "").replace("(back)", "")
    return layout


def track_type(value: Any) -> str:
    aliases = {"subtitles": "subtitle", "buttons": "button"}
    text = str(value or "unknown").strip().lower()
    return aliases.get(text, text)


def ffprobe_data(path: Path, ffprobe: str) -> dict[str, Any]:
    return run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )


def mkvmerge_data(path: Path, mkvmerge: str) -> dict[str, Any]:
    return run_json([mkvmerge, "-J", str(path)])


def normalize_inventory(path: Path, label: str, ffprobe: str, mkvmerge: str) -> dict[str, Any]:
    if not path.is_file():
        raise HelperError(f"Media file not found: {path}")
    probe = ffprobe_data(path, ffprobe)
    identify = mkvmerge_data(path, mkvmerge)
    format_duration = parse_duration(probe.get("format", {}).get("duration"))

    mux_by_type: dict[str, list[dict[str, Any]]] = {}
    for item in identify.get("tracks", []):
        mux_by_type.setdefault(track_type(item.get("type")), []).append(item)

    type_ordinals: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []
    for stream in probe.get("streams", []):
        kind = track_type(stream.get("codec_type"))
        ordinal = type_ordinals.get(kind, 0)
        type_ordinals[kind] = ordinal + 1
        mux_items = mux_by_type.get(kind, [])
        mux = mux_items[ordinal] if ordinal < len(mux_items) else {}
        properties = mux.get("properties", {}) if isinstance(mux, dict) else {}
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        duration = parse_duration(stream.get("duration"))
        if duration is None:
            duration = parse_duration(tags.get("DURATION"))
        if duration is None:
            duration = format_duration
        codec = str(stream.get("codec_name") or "unknown").lower()
        normalized.append(
            {
                "source": label,
                "ffprobe_index": int(stream.get("index", -1)),
                "mkvmerge_id": mux.get("id"),
                "type": kind,
                "type_order": ordinal,
                "codec": codec,
                "codec_long_name": stream.get("codec_long_name"),
                "codec_profile": stream.get("profile"),
                "codec_id": mux.get("codec") or mux.get("codec_id"),
                "language": clean_language(tags.get("language") or properties.get("language")),
                "title": str(tags.get("title") or properties.get("track_name") or ""),
                "channels": int(stream.get("channels") or properties.get("audio_channels") or 0),
                "channel_layout": str(stream.get("channel_layout") or ""),
                "sample_rate": int(stream.get("sample_rate") or properties.get("audio_sampling_frequency") or 0),
                "bits_per_sample": int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0),
                "width": int(stream.get("width") or 0),
                "height": int(stream.get("height") or 0),
                "pixel_format": str(stream.get("pix_fmt") or ""),
                "frame_rate": str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""),
                "duration_seconds": duration,
                "default": bool(disposition.get("default", properties.get("default_track", False))),
                "forced": bool(disposition.get("forced", properties.get("forced_track", False))),
                "is_flac": codec == "flac",
            }
        )
    return {
        "label": label,
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "duration_seconds": format_duration,
        "tracks": normalized,
        "container": identify.get("container", {}),
    }


def title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def layout_compatible(left: str, right: str) -> bool:
    a = clean_layout(left)
    b = clean_layout(right)
    return not a or not b or a == b


def is_lossless(track: dict[str, Any]) -> bool:
    codec = str(track.get("codec") or "").lower()
    profile = str(track.get("codec_profile") or "").lower()
    codec_id = str(track.get("codec_id") or "").lower()
    return codec in LOSSLESS_CODECS or "dts-hd master" in profile or "dts-hd ma" in codec_id


def candidate_score(old: dict[str, Any], source: dict[str, Any], max_duration_delta: float) -> dict[str, Any] | None:
    if old.get("type") != "audio" or source.get("type") != "audio":
        return None
    if int(old.get("channels") or 0) != int(source.get("channels") or 0):
        return None
    if not layout_compatible(str(old.get("channel_layout") or ""), str(source.get("channel_layout") or "")):
        return None
    old_lang = clean_language(old.get("language"))
    source_lang = clean_language(source.get("language"))
    if language_known(old_lang) and language_known(source_lang) and old_lang != source_lang:
        return None
    old_duration = old.get("duration_seconds")
    source_duration = source.get("duration_seconds")
    duration_delta = None
    if old_duration is not None and source_duration is not None:
        duration_delta = abs(float(old_duration) - float(source_duration))
        if duration_delta > max_duration_delta:
            return None

    score = 100
    reasons = ["channel_count_equal", "channel_layout_compatible"]
    if clean_layout(old.get("channel_layout")) and clean_layout(old.get("channel_layout")) == clean_layout(source.get("channel_layout")):
        score += 20
        reasons.append("channel_layout_equal")
    if language_known(old_lang) and old_lang == source_lang:
        score += 15
        reasons.append("language_equal")
    if duration_delta is not None:
        if duration_delta <= 1:
            score += 20
            reasons.append("duration_within_1s")
        elif duration_delta <= 10:
            score += 12
            reasons.append("duration_within_10s")
        else:
            score += max(0, int(10 - duration_delta / 30))
            reasons.append(f"duration_delta_{duration_delta:.3f}s")
    if int(old.get("type_order") or 0) == int(source.get("type_order") or 0):
        score += 3
        reasons.append("audio_order_equal")
    similarity = title_similarity(str(old.get("title") or ""), str(source.get("title") or ""))
    if similarity >= 0.75:
        score += 5
        reasons.append("title_similar")
    if is_lossless(source):
        score += 4
        reasons.append("source_lossless")
    return {
        "score": score,
        "reasons": reasons,
        "duration_delta_seconds": duration_delta,
        "source": source,
    }


def compact_track(track: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "source",
        "ffprobe_index",
        "mkvmerge_id",
        "type_order",
        "codec",
        "codec_profile",
        "codec_id",
        "language",
        "title",
        "channels",
        "channel_layout",
        "sample_rate",
        "bits_per_sample",
        "duration_seconds",
        "default",
        "forced",
        "is_flac",
    )
    return {field: track.get(field) for field in fields}


def match_inventory(inventory: dict[str, Any], max_duration_delta: float = 180.0) -> dict[str, Any]:
    old_tracks = inventory.get("video_source", {}).get("tracks", [])
    source_tracks = inventory.get("disc_source", {}).get("tracks", [])
    old_flac = [track for track in old_tracks if track.get("type") == "audio" and track.get("is_flac")]
    source_audio = [track for track in source_tracks if track.get("type") == "audio"]
    candidate_lists: list[list[dict[str, Any]]] = []
    for old in old_flac:
        candidates: list[dict[str, Any]] = []
        for source in source_audio:
            candidate = candidate_score(old, source, max_duration_delta)
            if candidate:
                candidates.append(candidate)
        candidates.sort(key=lambda item: (-int(item["score"]), int(item["source"]["type_order"])))
        candidate_lists.append(candidates)

    best_score: int | None = None
    best_assignment: list[dict[str, Any]] | None = None
    best_count = 0

    def assign(index: int, used: set[int], score: int, chosen: list[dict[str, Any]]) -> None:
        nonlocal best_score, best_assignment, best_count
        if index == len(candidate_lists):
            if best_score is None or score > best_score:
                best_score, best_assignment, best_count = score, list(chosen), 1
            elif score == best_score:
                best_count += 1
            return
        for candidate in candidate_lists[index]:
            source_index = int(candidate["source"]["ffprobe_index"])
            if source_index in used:
                continue
            assign(index + 1, used | {source_index}, score + int(candidate["score"]), [*chosen, candidate])

    if old_flac and all(candidate_lists):
        assign(0, set(), 0, [])

    unique_assignment = best_assignment if best_count == 1 else None
    selected_source_indexes: set[int] = set()
    mappings: list[dict[str, Any]] = []
    for index, old in enumerate(old_flac):
        candidates = candidate_lists[index]
        selected = unique_assignment[index]["source"] if unique_assignment else None
        if selected:
            status = "READY_FOR_PCM"
            selected_source_indexes.add(int(selected["ffprobe_index"]))
        else:
            status = "UNMATCHED" if not candidates else "AMBIGUOUS"
        mappings.append(
            {
                "status": status,
                "reference_track": compact_track(old),
                "selected_source": compact_track(selected) if selected else None,
                "candidates": [
                    {
                        "score": candidate["score"],
                        "reasons": candidate["reasons"],
                        "duration_delta_seconds": candidate["duration_delta_seconds"],
                        "source": compact_track(candidate["source"]),
                    }
                    for candidate in candidates
                ],
            }
        )

    additional = [
        compact_track(track)
        for track in source_audio
        if int(track["ffprobe_index"]) not in selected_source_indexes
    ]
    blocked = not old_flac or unique_assignment is None
    return {
        "schemaVersion": REPORT_VERSION,
        "kind": "movie-audio-match",
        "status": "BLOCKED" if blocked else "READY_FOR_PCM",
        "reference_track_count": len(old_flac),
        "mappings": mappings,
        "additional_disc_audio": additional,
        "blocking_reasons": (["NO_OLD_FLAC_SYNC_REFERENCE"] if not old_flac else [])
        + (["UNRESOLVED_FLAC_MAPPING"] if any(item["status"] != "READY_FOR_PCM" for item in mappings) else []),
    }


def stream_duration(path: Path, stream_index: int, ffprobe: str) -> float:
    data = ffprobe_data(path, ffprobe)
    format_duration = parse_duration(data.get("format", {}).get("duration"))
    for stream in data.get("streams", []):
        if int(stream.get("index", -1)) == stream_index:
            duration = parse_duration(stream.get("duration"))
            if duration is None:
                duration = parse_duration((stream.get("tags") or {}).get("DURATION"))
            if duration is None:
                duration = format_duration
            if duration is None:
                break
            return duration
    raise HelperError(f"Duration unavailable for stream {stream_index}: {path}")


def decode_pcm(
    path: Path,
    stream_index: int,
    start: float,
    duration: float,
    ffmpeg: str,
    sample_rate: int,
) -> array:
    if start < 0 or duration <= 0:
        raise HelperError("Invalid PCM decode range")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        f"0:{stream_index}",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{duration:.6f}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    payload = bytes(run_capture(command, binary=True))
    samples = array("h")
    samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def energy_envelope(samples: array, sample_rate: int, hop_seconds: float) -> list[float]:
    block = max(1, int(round(sample_rate * hop_seconds)))
    values: list[float] = []
    for start in range(0, len(samples) - block + 1, block):
        section = samples[start : start + block]
        mean_square = sum(value * value for value in section) / len(section)
        values.append(math.log1p(math.sqrt(mean_square)))
    return values


def informative(envelope: list[float]) -> bool:
    return len(envelope) >= 20 and statistics.fmean(envelope) >= math.log1p(80) and statistics.pstdev(envelope) >= 0.08


def best_correlation(reference: list[float], candidate: list[float], start_lag: int = 0, end_lag: int | None = None) -> tuple[int, float, float]:
    if not reference or len(candidate) < len(reference):
        raise HelperError("PCM envelope is too short for correlation")
    if end_lag is None:
        end_lag = len(candidate) - len(reference)
    start_lag = max(0, start_lag)
    end_lag = min(end_lag, len(candidate) - len(reference))
    if start_lag > end_lag:
        raise HelperError("PCM correlation range is empty")

    count = len(reference)
    ref_mean = statistics.fmean(reference)
    ref_centered = [value - ref_mean for value in reference]
    ref_energy = sum(value * value for value in ref_centered)
    prefix = [0.0]
    prefix_sq = [0.0]
    for value in candidate:
        prefix.append(prefix[-1] + value)
        prefix_sq.append(prefix_sq[-1] + value * value)

    scores: list[tuple[float, int]] = []
    for lag in range(start_lag, end_lag + 1):
        segment_sum = prefix[lag + count] - prefix[lag]
        segment_sq = prefix_sq[lag + count] - prefix_sq[lag]
        segment_mean = segment_sum / count
        segment_energy = max(0.0, segment_sq - count * segment_mean * segment_mean)
        if ref_energy <= 0 or segment_energy <= 0:
            score = -1.0
        else:
            dot = 0.0
            for index, ref_value in enumerate(ref_centered):
                dot += ref_value * (candidate[lag + index] - segment_mean)
            score = dot / math.sqrt(ref_energy * segment_energy)
        scores.append((score, lag))
    scores.sort(reverse=True)
    best_score, best_lag = scores[0]
    distinct = [score for score, lag in scores[1:] if abs(lag - best_lag) > 2]
    second_score = distinct[0] if distinct else -1.0
    return best_lag, best_score, second_score


def analyze_sample(
    reference_path: Path,
    reference_stream: int,
    source_path: Path,
    source_stream: int,
    reference_start: float,
    source_duration: float,
    ffmpeg: str,
    window: float,
    search: float,
    sample_rate: int,
) -> dict[str, Any]:
    search_start = max(0.0, reference_start - search)
    search_end = min(source_duration, reference_start + window + search)
    search_duration = search_end - search_start
    if search_duration < window:
        return {"status": "REJECTED", "reason": "SOURCE_RANGE_TOO_SHORT", "reference_start": reference_start}

    reference_pcm = decode_pcm(reference_path, reference_stream, reference_start, window, ffmpeg, sample_rate)
    source_pcm = decode_pcm(source_path, source_stream, search_start, search_duration, ffmpeg, sample_rate)
    coarse_hop = 0.1
    fine_hop = 0.02
    reference_coarse = energy_envelope(reference_pcm, sample_rate, coarse_hop)
    source_coarse = energy_envelope(source_pcm, sample_rate, coarse_hop)
    if not informative(reference_coarse):
        return {"status": "REJECTED", "reason": "LOW_INFORMATION", "reference_start": reference_start}
    coarse_lag, coarse_score, second_score = best_correlation(reference_coarse, source_coarse)

    reference_fine = energy_envelope(reference_pcm, sample_rate, fine_hop)
    source_fine = energy_envelope(source_pcm, sample_rate, fine_hop)
    ratio = int(round(coarse_hop / fine_hop))
    center = coarse_lag * ratio
    fine_lag, fine_score, _ = best_correlation(reference_fine, source_fine, center - ratio * 2, center + ratio * 2)
    source_match_start = search_start + fine_lag * fine_hop
    offset_seconds = reference_start - source_match_start
    return {
        "status": "CANDIDATE",
        "reference_start": round(reference_start, 6),
        "source_match_start": round(source_match_start, 6),
        "offset_ms": round(offset_seconds * 1000, 3),
        "correlation": round(fine_score, 6),
        "coarse_correlation": round(coarse_score, 6),
        "coarse_margin": round(coarse_score - second_score, 6),
    }


def analyze_pair(
    reference_path: Path,
    reference_stream: int,
    source_path: Path,
    source_stream: int,
    ffmpeg: str,
    ffprobe: str,
    points: int,
    window: float,
    search: float,
    sample_rate: int,
    min_correlation: float,
    min_margin: float,
    tolerance_ms: float,
) -> dict[str, Any]:
    reference_duration = stream_duration(reference_path, reference_stream, ffprobe)
    source_duration = stream_duration(source_path, source_stream, ffprobe)
    if reference_duration <= window:
        raise HelperError("Reference stream is shorter than one sample window")
    fractions = [0.10, 0.30, 0.50, 0.70, 0.90, 0.20, 0.40, 0.60, 0.80, 0.15, 0.85]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for fraction in fractions:
        if len(accepted) >= points:
            break
        start = max(0.0, min(reference_duration - window, (reference_duration - window) * fraction))
        sample = analyze_sample(
            reference_path,
            reference_stream,
            source_path,
            source_stream,
            start,
            source_duration,
            ffmpeg,
            window,
            search,
            sample_rate,
        )
        if sample["status"] == "CANDIDATE" and sample["correlation"] >= min_correlation and sample["coarse_margin"] >= min_margin:
            sample["status"] = "ACCEPTED"
            accepted.append(sample)
        else:
            if sample["status"] == "CANDIDATE":
                sample["status"] = "REJECTED"
                sample["reason"] = "CORRELATION_OR_MARGIN_BELOW_THRESHOLD"
            rejected.append(sample)

    if len(accepted) < 3:
        status = "BLOCKED"
        median_offset = None
        spread = None
        reason = "FEWER_THAN_THREE_VALID_POINTS"
    else:
        offsets = [float(sample["offset_ms"]) for sample in accepted]
        median_offset = statistics.median(offsets)
        spread = max(abs(value - median_offset) for value in offsets)
        if spread > tolerance_ms:
            status = "BLOCKED"
            reason = "NON_FIXED_OFFSET"
        else:
            status = "OK"
            reason = None
    return {
        "status": status,
        "reason": reason,
        "reference_stream": reference_stream,
        "source_stream": source_stream,
        "reference_duration_seconds": reference_duration,
        "source_duration_seconds": source_duration,
        "valid_points": len(accepted),
        "median_offset_ms": round(median_offset, 3) if median_offset is not None else None,
        "max_deviation_ms": round(spread, 3) if spread is not None else None,
        "accepted": accepted,
        "rejected": rejected,
    }


def is_remote_path(path: Path) -> bool:
    text = str(path.resolve())
    if text.startswith("\\\\"):
        return True
    if os.name != "nt":
        return False
    drive = Path(text).drive
    if not drive:
        return False
    root = drive + "\\"
    return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 4


def yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def add_track_properties(arguments: list[str], track: dict[str, Any]) -> None:
    track_id = int(track["mux_id"])
    language = str(track.get("language") or "und")
    arguments.extend(["--language", f"{track_id}:{language}"])
    if "name" in track:
        arguments.extend(["--track-name", f"{track_id}:{track.get('name') or ''}"])
    arguments.extend(["--default-track", f"{track_id}:{yes_no(track.get('default'))}"])
    arguments.extend(["--forced-display-flag", f"{track_id}:{yes_no(track.get('forced'))}"])
    if track.get("origin") == "disc":
        arguments.extend(["--sync", f"{track_id}:{int(round(float(track.get('sync_ms') or 0)))}"])


def validate_mux_plan(plan: dict[str, Any]) -> None:
    for key in ("video_source", "disc_source", "output", "video", "audio", "subtitles"):
        if key not in plan:
            raise HelperError(f"Mux plan is missing: {key}")
    old_mkv = Path(plan["video_source"])
    m2ts = Path(plan["disc_source"])
    output = Path(plan["output"])
    for path in (old_mkv, m2ts):
        if not path.is_file():
            raise HelperError(f"Mux input not found: {path}")
        if is_remote_path(path):
            raise HelperError(f"Mux input must be local: {path}")
    if is_remote_path(output):
        raise HelperError(f"Mux output must be local: {output}")
    if not output.parent.is_dir():
        raise HelperError(f"Mux output directory does not exist: {output.parent}")
    if output.exists():
        raise HelperError(f"Refusing to overwrite local mux output: {output}")
    if not plan.get("audio"):
        raise HelperError("Mux plan must retain at least one audio track")


def build_mkvmerge_command(plan: dict[str, Any], mkvmerge: str) -> list[str]:
    validate_mux_plan(plan)
    old_mkv = str(Path(plan["video_source"]).resolve())
    m2ts = str(Path(plan["disc_source"]).resolve())
    output = str(Path(plan["output"]).resolve())
    video = dict(plan["video"])
    video["mux_id"] = int(video["old_mux_id"])

    old_audio = [dict(track) for track in plan["audio"] if track.get("origin") == "video"]
    source_audio = [dict(track) for track in plan["audio"] if track.get("origin") == "disc"]
    if len(old_audio) + len(source_audio) != len(plan["audio"]):
        raise HelperError("Every audio item origin must be video or disc")

    old_options = ["--video-tracks", str(video["mux_id"])]
    old_options.extend(["--language", f"{video['mux_id']}:{video.get('language') or 'jpn'}"])
    old_options.extend(["--track-name", f"{video['mux_id']}:{video.get('name') or ''}"])
    old_options.extend([
        "--default-track", f"{video['mux_id']}:yes",
        "--forced-display-flag", f"{video['mux_id']}:no",
        "--no-subtitles", "--no-attachments",
    ])
    if old_audio:
        old_options.extend(["--audio-tracks", ",".join(str(track["mux_id"]) for track in old_audio)])
        for track in old_audio:
            add_track_properties(old_options, track)
    else:
        old_options.append("--no-audio")

    source_options = ["--no-video", "--no-subtitles", "--no-attachments"]
    if source_audio:
        source_options.extend(["--audio-tracks", ",".join(str(track["mux_id"]) for track in source_audio)])
        for track in source_audio:
            add_track_properties(source_options, track)
    else:
        source_options.append("--no-audio")

    track_order = [f"0:{video['mux_id']}"]
    for track in plan["audio"]:
        input_id = 0 if track["origin"] == "video" else 1
        track_order.append(f"{input_id}:{int(track['mux_id'])}")

    subtitle_inputs: list[str] = []
    for index, subtitle in enumerate(plan["subtitles"], start=2):
        path = Path(subtitle["path"])
        if not path.is_file() or is_remote_path(path):
            raise HelperError(f"Subtitle must be a local file: {path}")
        subtitle_inputs.extend(
            [
                "--language",
                f"0:{subtitle.get('language') or 'chi'}",
                "--track-name",
                f"0:{subtitle.get('name') or ''}",
                "--default-track",
                f"0:{yes_no(subtitle.get('default'))}",
                "--forced-display-flag",
                f"0:{yes_no(subtitle.get('forced'))}",
                str(path.resolve()),
            ]
        )
        track_order.append(f"{index}:0")

    return [
        mkvmerge,
        "-o",
        output,
        "--track-order",
        ",".join(track_order),
        *old_options,
        old_mkv,
        *source_options,
        m2ts,
        *subtitle_inputs,
    ]


def execute_plan(plan: dict[str, Any], mkvmerge: str) -> dict[str, Any]:
    command = build_mkvmerge_command(plan, mkvmerge)
    result: dict[str, Any] = {
        "schemaVersion": REPORT_VERSION,
        "kind": "movie-audio-mux",
        "status": "FAILED",
        "command": command,
        "output": str(Path(plan["output"]).resolve()),
    }
    process = subprocess.run(command, capture_output=True, check=False)
    result["exitCode"] = process.returncode
    result["stdout"] = decode_output(process.stdout)[-4000:]
    result["stderr"] = decode_output(process.stderr)[-4000:]
    if process.returncode not in {0, 1}:
        result["error"] = result["stderr"] or result["stdout"] or f"mkvmerge exit code {process.returncode}"
    elif not Path(plan["output"]).is_file():
        result["error"] = "mkvmerge exited successfully but output is missing"
    else:
        result["status"] = "WARNING" if process.returncode == 1 else "OK"
        if process.returncode == 1:
            result["warning"] = result["stderr"] or result["stdout"] or "mkvmerge completed with warnings"
        result["size"] = Path(plan["output"]).stat().st_size
    return result


def codec_family(codec: Any, profile: Any = None) -> str:
    name = str(codec or "").lower()
    profile_name = str(profile or "").lower()
    if name.startswith("pcm_") or name == "pcm_bluray":
        return "pcm"
    if name == "dts" and ("dts-hd" in profile_name or "master" in profile_name):
        return "dts-hd-ma"
    return name


def check_item(checks: list[dict[str, Any]], check_id: str, passed: bool, actual: Any, expected: Any, severity: str = "BLOCKED") -> None:
    checks.append(
        {
            "id": check_id,
            "status": "OK" if passed else severity,
            "actual": actual,
            "expected": expected,
        }
    )


def verify_plan(
    plan: dict[str, Any],
    ffprobe: str,
    mkvmerge: str,
    mkvinfo: str,
    *,
    old_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = Path(plan["output"])
    if not output.is_file():
        raise HelperError(f"Output not found: {output}")
    inventory = normalize_inventory(output, "output", ffprobe, mkvmerge)
    tracks = inventory["tracks"]
    videos = [track for track in tracks if track["type"] == "video"]
    audio = [track for track in tracks if track["type"] == "audio"]
    subtitles = [track for track in tracks if track["type"] == "subtitle"]
    attachments = [track for track in tracks if track["type"] == "attachment"]
    checks: list[dict[str, Any]] = []

    check_item(checks, "video_count", len(videos) == 1, len(videos), 1)
    check_item(checks, "audio_count", len(audio) == len(plan["audio"]), len(audio), len(plan["audio"]))
    check_item(checks, "subtitle_count", len(subtitles) == len(plan["subtitles"]), len(subtitles), len(plan["subtitles"]))
    check_item(checks, "attachment_count", len(attachments) == 0, len(attachments), 0)
    pgs = [track for track in subtitles if track["codec"] == "hdmv_pgs_subtitle"]
    check_item(checks, "pgs_removed", not pgs, [track["ffprobe_index"] for track in pgs], [])

    if videos:
        expected_video = plan["video"]
        check_item(checks, "video_name", videos[0]["title"] == str(expected_video.get("name") or ""), videos[0]["title"], expected_video.get("name") or "")
        check_item(checks, "video_language", videos[0]["language"] == clean_language(expected_video.get("language") or "jpn"), videos[0]["language"], clean_language(expected_video.get("language") or "jpn"))
        old_inventory = old_inventory or normalize_inventory(Path(plan["video_source"]), "video_source", ffprobe, mkvmerge)
        old_videos = [track for track in old_inventory["tracks"] if track["type"] == "video"]
        if old_videos:
            for field in ("codec", "width", "height", "pixel_format", "frame_rate"):
                check_item(checks, f"video_{field}", videos[0].get(field) == old_videos[0].get(field), videos[0].get(field), old_videos[0].get(field))
            check_item(checks, "video_selected_from_video_source", expected_video.get("old_mux_id") is not None, expected_video.get("old_mux_id"), "compressed-video MKV track id")

    for index, expected in enumerate(plan["audio"]):
        if index >= len(audio):
            break
        actual = audio[index]
        prefix = f"audio_{index}"
        check_item(checks, f"{prefix}_channels", actual["channels"] == int(expected.get("channels") or actual["channels"]), actual["channels"], expected.get("channels"))
        check_item(checks, f"{prefix}_language", actual["language"] == clean_language(expected.get("language")), actual["language"], clean_language(expected.get("language")))
        check_item(checks, f"{prefix}_name", actual["title"] == str(expected.get("name") or ""), actual["title"], expected.get("name") or "")
        check_item(checks, f"{prefix}_default", actual["default"] == bool(expected.get("default")), actual["default"], bool(expected.get("default")))
        if expected.get("codec"):
            expected_family = codec_family(expected["codec"], expected.get("codec_profile"))
            actual_family = codec_family(actual["codec"], actual.get("codec_profile"))
            check_item(checks, f"{prefix}_codec", actual_family == expected_family, actual_family, expected_family)
        for field, expected_key in (("channel_layout", "channel_layout"), ("sample_rate", "sample_rate"), ("bits_per_sample", "bits_per_sample")):
            if expected.get(expected_key) not in {None, "", 0}:
                layout_is_implicit_for_pcm = (
                    field == "channel_layout"
                    and expected.get("origin") == "disc"
                    and str(actual.get("codec") or "").casefold().startswith("pcm")
                    and not actual.get(field)
                )
                check_item(checks, f"{prefix}_{field}", layout_is_implicit_for_pcm or actual.get(field) == expected.get(expected_key), actual.get(field), expected.get(expected_key))
        if expected.get("origin") == "disc":
            check_item(checks, f"{prefix}_selected_from_disc", expected.get("source_ffprobe_index") is not None, expected.get("source_ffprobe_index"), "disc-audio source stream")

    for index, expected in enumerate(plan["subtitles"]):
        if index >= len(subtitles):
            break
        actual = subtitles[index]
        prefix = f"subtitle_{index}"
        check_item(checks, f"{prefix}_name", actual["title"] == str(expected.get("name") or ""), actual["title"], expected.get("name") or "")
        check_item(checks, f"{prefix}_language", actual["language"] == clean_language(expected.get("language") or "chi"), actual["language"], clean_language(expected.get("language") or "chi"))
        check_item(checks, f"{prefix}_default", actual["default"] == bool(expected.get("default")), actual["default"], bool(expected.get("default")))
        check_item(checks, f"{prefix}_forced", actual["forced"] == bool(expected.get("forced")), actual["forced"], bool(expected.get("forced")))

    try:
        run_capture([mkvinfo, "--summary", str(output)])
        check_item(checks, "mkvinfo_readable", True, True, True)
    except HelperError as exc:
        check_item(checks, "mkvinfo_readable", False, str(exc), "readable container")

    blocked = any(check["status"] == "BLOCKED" for check in checks)
    warned = any(check["status"] == "WARN" for check in checks)
    return {
        "schemaVersion": REPORT_VERSION,
        "kind": "movie-audio-verify",
        "status": "BLOCKED" if blocked else ("WARN" if warned else "OK"),
        "output": str(output.resolve()),
        "size": output.stat().st_size,
        "lightweightVerification": True,
        "checks": checks,
        "inventory": inventory,
    }
