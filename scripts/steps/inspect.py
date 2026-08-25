from __future__ import annotations

import json

from common import backend_cache_path, backend_command, read_text, result, save_state
from media_plan import build_plan


def public_metadata_summary(metadata):
    if not isinstance(metadata, dict):
        return None
    episode_counts = {}
    for item in metadata.get("episodes", []):
        season = item.get("localSeason", item.get("season"))
        if season is not None:
            episode_counts[f"S{int(season)}"] = episode_counts.get(f"S{int(season)}", 0) + 1
    suggested = metadata.get("suggestedDecisions", {})
    return {
        "status": metadata.get("status"),
        "mode": metadata.get("mode"),
        "query": metadata.get("query"),
        "querySource": metadata.get("querySource"),
        "queryCandidates": metadata.get("queryCandidates", []),
        "mediaType": metadata.get("mediaType"),
        "episodeOrder": metadata.get("episodeOrder"),
        "proxy": metadata.get("proxy"),
        "candidates": metadata.get("candidates", []),
        "selected": metadata.get("selected"),
        "tvdb": metadata.get("tvdb"),
        "episodeCounts": dict(sorted(episode_counts.items())),
        "suggestedTitle": suggested.get("title"),
        "suggestedEpisodeMapCount": len(suggested.get("episode_map", {})),
        "issues": metadata.get("issues", []),
        "warnings": metadata.get("warnings", []),
    }


def run(context):
    work = context["work_dir"]
    state = context["state"]
    task = state.get("task", "complete-archive")
    decisions = state.get("decisions", {})
    extra = ["--work-path", str(work), "--task-mode", task]
    if getattr(context.get("args"), "rerun", False):
        extra.append("--rerun")
    if decisions.get("title"):
        extra.extend(["--title", str(decisions["title"])])
    if "metadata" in state.get("resolved_capabilities", []) and isinstance(decisions.get("metadata"), dict):
        extra.extend(["--metadata-json", json.dumps(decisions["metadata"], ensure_ascii=False, separators=(",", ":"))])
    if state.get("branch") == "movie" and decisions.get("retain_embedded_subtitles"):
        extra.append("--retain-embedded-subtitles")
    for requested in state.get("requested_steps") or []:
        extra.extend(["--requested-step", str(requested)])
    for capability in state.get("resolved_capabilities") or []:
        extra.extend(["--selected-capability", str(capability)])
    for sink in state.get("requested_final_sinks") or state.get("final_sinks") or []:
        extra.extend(["--selected-final-sink", str(sink)])
    if "kdocs-tracker" in state.get("resolved_capabilities", []) and state.get("entrypoint") != "hub":
        extra.append("--kdocs-tracker")
    movie_audio_pairs = decisions.get("movie_audio_pairs")
    disc_source = decisions.get("disc_source") or decisions.get("m2ts")
    if state.get("branch") == "movie" and (movie_audio_pairs or disc_source or decisions.get("movie_audio_replacement")):
        extra.append("--movie-audio-replacement")
        if movie_audio_pairs:
            extra.extend(["--movie-audio-pairs-json", json.dumps(movie_audio_pairs, ensure_ascii=False, separators=(",", ":"))])
        elif disc_source:
            extra.extend(["--disc-source", str(disc_source)])
            video_source = decisions.get("video_source") or decisions.get("old_mkv")
            if video_source:
                extra.extend(["--video-source", str(video_source)])
    output = backend_command(work, "inspect", extra, allow_needs_user=True)
    preflight = None
    if backend_cache_path(work).is_file():
        manifest = json.loads(read_text(backend_cache_path(work)))
        generated = build_plan(work, manifest, state)
        state["selected_steps"] = generated["selected_steps"]
        for key in (
            "requested_capabilities",
            "resolved_capabilities",
            "auto_added_capabilities",
            "unavailable_capabilities",
            "final_sinks",
        ):
            state[key] = generated[key]
        save_state(work, state)
        preflight = {
            "issues": generated["issues"], "summary": generated["summary"], "steps": generated["selected_steps"],
            "capabilities": generated["resolved_capabilities"],
            "auto_added_capabilities": generated["auto_added_capabilities"],
            "final_sinks": generated["final_sinks"],
            "metadata": public_metadata_summary(generated.get("metadata")),
        }
    pending = bool(preflight and preflight["issues"])
    status = "NEEDS_USER" if output.get("status") == "NEEDS_USER" or pending else "COMPLETE"
    metadata_warnings = preflight.get("metadata", {}).get("warnings", []) if preflight else []
    return result(status, "quick inspection complete", warnings=metadata_warnings, preflight=preflight)
