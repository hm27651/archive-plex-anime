from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from archive_rules import backend_cache_path
from internal import preflight
from internal.signatures import file_signature


class PreflightHarness:
    def __init__(self, root: Path, branch: str = "tv") -> None:
        self.root = root
        self.branch = branch
        self.calls: Counter[str] = Counter()
        self.tools = root.parent / "tools"
        self.database = root.parent / "database"
        self.primary = root.parent / "primary"
        self.fallback = root.parent / "fallback"
        for directory in (self.tools, self.database, self.primary, self.fallback):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("mediainfo", "mkvmerge", "assfonts", "ffprobe", "ffmpeg", "mkvinfo", "kdocs-cli"):
            (self.tools / f"{name}.exe").write_bytes(name.encode("ascii"))
        (self.database / "fonts.json").write_text("[]", encoding="utf-8")
        (self.fallback / "fc-subs.db").write_bytes(b"missing-index-is-not-read-by-the-test")
        tv_root = root.parent if branch == "tv" else root.parent.parent / "TV"
        movie_root = root.parent if branch == "movie" else root.parent.parent / "Movie"
        self.config = {
            "_path": str(root.parent / "config.json"),
            "paths": {
                "workRoot": str(tv_root),
                "movieWorkRoot": str(movie_root),
                "primaryFonts": str(self.primary),
                "fallbackFonts": str(self.fallback),
                "fallbackFontDatabase": str(self.fallback / "fc-subs.db"),
                "movieSubtitleArchiveRoot": str(root.parent / "movie-zips"),
            },
            "defaults": {"library": "Anime3", "movieLibrary": "Movie3"},
            "storageTargets": {},
            "plexLibraries": {},
            "tracker": {"enabled": False},
            "tools": {name: str(self.tools / f"{name}.exe") for name in (
                "mediainfo", "mkvmerge", "assfonts", "ffprobe", "ffmpeg", "mkvinfo", "kdocs-cli"
            )},
        }

    def args(
        self,
        *,
        rerun: bool = False,
        title: str | None = "作品",
        pairs: list[dict] | None = None,
        capabilities: list[str] | None = None,
        final_sinks: list[str] | None = None,
    ) -> Namespace:
        values = {
            "work_path": str(self.root),
            "config": None,
            "manifest": None,
            "task_mode": "complete-archive",
            "title": title,
            "requested_step": [],
            "selected_capability": capabilities or [],
            "movie_audio_replacement": bool(pairs),
            "movie_audio_pairs_json": json.dumps(pairs, ensure_ascii=False) if pairs else None,
            "disc_source": None,
            "video_source": None,
            "retain_embedded_subtitles": False,
            "rerun": rerun,
        }
        if final_sinks is not None:
            values["selected_final_sink"] = final_sinks
        return Namespace(**values)

    def media(self, path: Path, _tool: str) -> dict:
        self.calls[f"media:{path.name}"] += 1
        return {"file": file_signature(path), "status": "OK", "tracks": []}

    def library(self, _config: dict, title: str, branch: str, preferred: str) -> dict:
        self.calls["library"] += 1
        return {
            "trackerMatches": [],
            "nasMatches": [],
            "resolution": {"status": "OK", "mode": "create", "library": preferred},
            "trackerState": {"status": "DISABLED", "title": title, "branch": branch},
        }

    def movie_audio(self, _config: dict, title: str, disc: str, video: str, stack: str = "") -> dict:
        self.calls[f"pcm:{stack or 'single'}"] += 1
        return {
            "status": "READY_FOR_PREFLIGHT",
            "title": title,
            "stack": stack,
            "verifiedDiscSource": file_signature(Path(disc)),
            "videoSource": file_signature(Path(video)),
            "subtitleZip": None,
            "matching": {"status": "READY_FOR_PCM", "mappings": [{"id": 1}]},
            "sync": {"status": "OK", "pairs": [{"status": "OK", "valid_points": 3}]},
        }

    def parse_subtitle(self, path: Path) -> list[dict]:
        self.calls[f"subtitle:{path.name}"] += 1
        return [{"normalized": "neededfont", "name": "Needed Font", "sources": [str(path)], "contexts": ["style"]}]

    def resolve_fonts(self, requirements: list[dict], *_args, **_kwargs) -> tuple[list[dict], list[dict]]:
        self.calls["fonts"] += 1
        return ([{**item, "tier": "test", "available": True} for item in requirements], [])

    def metadata(self, work: Path, _config: dict, route: dict, files: list[Path], supplied: dict) -> dict:
        self.calls["metadata"] += 1
        query = str(supplied.get("query") or work.name)
        return {
            "status": "MATCHED", "mode": "auto", "query": query,
            "mediaType": "tv" if route.get("branch") == "anime" else "movie",
            "suggestedDecisions": {"title": query, "metadata": {"mode": "auto", "query": query, "tmdb_id": 1}},
            "issues": [], "warnings": [], "candidates": [], "selected": {"id": 1}, "episodes": [],
        }

    def run(self, args: Namespace) -> dict:
        with mock.patch.object(preflight, "parse_ass_font_requirements", side_effect=self.parse_subtitle), mock.patch.object(
            preflight, "load_assfonts_database", return_value=[]
        ), mock.patch.object(preflight, "resolve_font_availability", side_effect=self.resolve_fonts):
            return preflight.execute_inspection(
                args,
                config_loader=lambda _path: self.config,
                file_lister=preflight.list_task_files,
                media_inspector=self.media,
                tool_resolver=lambda config, name: config["tools"][name],
                embedded_inspector=lambda *_args: {"status": "MISSING", "assTracks": 0, "files": [], "extractedSubtitles": []},
                library_inspector=self.library,
                movie_audio_inspector=self.movie_audio,
                database_resolver=lambda _config: self.database,
                metadata_inspector=self.metadata,
            )


class IncrementalPreflightTests(unittest.TestCase):
    def make_tv(self, directory: str) -> tuple[Path, PreflightHarness, Path, Path]:
        base = Path(directory)
        root = base / "TV" / "作品"
        root.mkdir(parents=True)
        video = root / "source.mkv"
        subtitle = root / "group" / "source.ass"
        subtitle.parent.mkdir()
        video.write_bytes(b"video")
        subtitle.write_text("subtitle", encoding="utf-8")
        return root, PreflightHarness(root), video, subtitle

    def make_movie(self, directory: str) -> tuple[Path, PreflightHarness, list[dict]]:
        base = Path(directory)
        root = base / "Movie" / "作品"
        root.mkdir(parents=True)
        (root / "source.ass").write_text("subtitle", encoding="utf-8")
        pairs = []
        for stack in ("cd1", "cd2"):
            video = root / f"source.{stack}.mkv"
            disc = root / "BDMV" / f"{stack}.m2ts"
            disc.parent.mkdir(exist_ok=True)
            video.write_bytes(f"video-{stack}".encode("ascii"))
            disc.write_bytes(f"disc-{stack}".encode("ascii"))
            pairs.append({"stack": stack, "video_source": str(video), "disc_source": str(disc)})
        return root, PreflightHarness(root, branch="movie"), pairs

    def test_unchanged_rerun_reuses_all_expensive_components(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.run(harness.args())
            before = harness.calls.copy()

            result = harness.run(harness.args(rerun=True))

            self.assertEqual("READY_FOR_PREFLIGHT", result["status"])
            self.assertEqual(before, harness.calls)

    def test_added_work_font_reruns_only_font_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root, harness, _video, _subtitle = self.make_tv(directory)
            harness.run(harness.args())
            before = harness.calls.copy()
            (root / "Needed.ttf").write_bytes(b"font")

            harness.run(harness.args(rerun=True))

            self.assertEqual(before["library"], harness.calls["library"])
            self.assertEqual(before["media:source.mkv"], harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"], harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"] + 1, harness.calls["fonts"])

    def test_changed_ass_reruns_subtitle_and_font_checks_only(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, subtitle = self.make_tv(directory)
            harness.run(harness.args())
            before = harness.calls.copy()
            subtitle.write_text("subtitle changed and longer", encoding="utf-8")

            harness.run(harness.args(rerun=True))

            self.assertEqual(before["library"], harness.calls["library"])
            self.assertEqual(before["media:source.mkv"], harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"] + 1, harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"] + 1, harness.calls["fonts"])

    def test_changed_title_reruns_library_lookup_but_reuses_media_and_fonts(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.run(harness.args(title="旧标题"))
            before = harness.calls.copy()

            harness.run(harness.args(rerun=True, title="新标题"))

            self.assertEqual(before["library"] + 1, harness.calls["library"])
            self.assertEqual(before["media:source.mkv"], harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"], harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"], harness.calls["fonts"])

    def test_changed_metadata_query_reruns_metadata_and_library_only(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.config["metadata"] = {"enabled": True, "mode": "auto"}
            first = harness.args()
            first.title = None
            first.metadata_json = json.dumps({"query": "旧标题"}, ensure_ascii=False)
            harness.run(first)
            before = harness.calls.copy()

            second = harness.args(rerun=True)
            second.title = None
            second.metadata_json = json.dumps({"query": "新标题"}, ensure_ascii=False)
            harness.run(second)

            self.assertEqual(before["metadata"] + 1, harness.calls["metadata"])
            self.assertEqual(before["library"] + 1, harness.calls["library"])
            self.assertEqual(before["media:source.mkv"], harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"], harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"], harness.calls["fonts"])

    def test_unchanged_metadata_is_reused_without_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.config["metadata"] = {"enabled": True, "mode": "auto"}
            harness.run(harness.args())
            before = harness.calls.copy()
            harness.run(harness.args(rerun=True))
            self.assertEqual(before["metadata"], harness.calls["metadata"])

    def test_unselected_metadata_never_reads_credentials_or_calls_inspector(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.config["metadata"] = {"enabled": True, "mode": "auto"}
            args = harness.args(capabilities=["inspect", "remux"])
            with mock.patch.object(preflight, "credential_presence", side_effect=AssertionError("credentials read")):
                harness.run(args)
            manifest = json.loads(backend_cache_path(_root).read_text(encoding="utf-8"))
            self.assertEqual(0, harness.calls["metadata"])
            self.assertEqual("OFF", manifest["discovery"]["metadata"]["status"])
            self.assertFalse(manifest["discovery"]["metadata"]["selected"])

    def test_metadata_selection_changes_invalidate_the_component_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            harness.config["metadata"] = {"enabled": True, "mode": "auto"}
            harness.run(harness.args(capabilities=["inspect", "metadata", "remux"]))
            self.assertEqual(1, harness.calls["metadata"])

            harness.run(harness.args(rerun=True, capabilities=["inspect", "remux"]))
            disabled = json.loads(backend_cache_path(_root).read_text(encoding="utf-8"))
            self.assertEqual(1, harness.calls["metadata"])
            self.assertEqual("OFF", disabled["discovery"]["metadata"]["status"])

            harness.run(harness.args(rerun=True, capabilities=["inspect", "metadata", "remux"]))
            self.assertEqual(2, harness.calls["metadata"])

    def test_custom_final_delivery_without_metadata_requires_a_title(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, _video, _subtitle = self.make_tv(directory)
            args = harness.args(
                title=None,
                capabilities=["inspect", "video-delivery"],
                final_sinks=["video"],
            )
            with self.assertRaises(preflight.WorkflowError) as caught:
                harness.run(args)
            self.assertEqual("TITLE_REQUIRED_WITHOUT_METADATA", caught.exception.code)
            self.assertEqual(0, harness.calls["metadata"])

    def test_added_mka_does_not_invalidate_metadata_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root, harness, _video, _subtitle = self.make_tv(directory)
            harness.config["metadata"] = {"enabled": True, "mode": "auto"}
            harness.run(harness.args())
            before = harness.calls.copy()
            (root / "source.mka").write_bytes(b"audio")

            harness.run(harness.args(rerun=True))

            self.assertEqual(before["metadata"], harness.calls["metadata"])
            self.assertEqual(before["library"], harness.calls["library"])
            self.assertEqual(before["media:source.mkv"], harness.calls["media:source.mkv"])
            self.assertEqual(1, harness.calls["media:source.mka"])

    def test_stacked_movie_reruns_pcm_for_only_the_changed_disc(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, pairs = self.make_movie(directory)
            harness.run(harness.args(pairs=pairs))
            before = harness.calls.copy()
            Path(pairs[1]["disc_source"]).write_bytes(b"changed-disc-cd2")

            harness.run(harness.args(rerun=True, pairs=pairs))

            self.assertEqual(before["pcm:cd1"], harness.calls["pcm:cd1"])
            self.assertEqual(before["pcm:cd2"] + 1, harness.calls["pcm:cd2"])

    def test_stacked_movie_video_change_reruns_only_that_video_and_pcm_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, harness, pairs = self.make_movie(directory)
            harness.run(harness.args(pairs=pairs))
            before = harness.calls.copy()
            Path(pairs[0]["video_source"]).write_bytes(b"changed-video-cd1")

            harness.run(harness.args(rerun=True, pairs=pairs))

            self.assertEqual(before["media:source.cd1.mkv"] + 1, harness.calls["media:source.cd1.mkv"])
            self.assertEqual(before["media:source.cd2.mkv"], harness.calls["media:source.cd2.mkv"])
            self.assertEqual(before["pcm:cd1"] + 1, harness.calls["pcm:cd1"])
            self.assertEqual(before["pcm:cd2"], harness.calls["pcm:cd2"])

    def test_movie_title_change_reuses_pcm_and_refreshes_subtitle_zip_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root, harness, pairs = self.make_movie(directory)
            harness.run(harness.args(title="旧标题", pairs=pairs))
            before = harness.calls.copy()
            archive = Path(harness.config["paths"]["movieSubtitleArchiveRoot"])
            archive.mkdir(parents=True)
            new_zip = archive / "新标题.zip"
            new_zip.write_bytes(b"zip-reference")

            harness.run(harness.args(rerun=True, title="新标题", pairs=pairs))

            self.assertEqual(before["pcm:cd1"], harness.calls["pcm:cd1"])
            self.assertEqual(before["pcm:cd2"], harness.calls["pcm:cd2"])
            manifest = json.loads(backend_cache_path(root).read_text(encoding="utf-8"))
            self.assertEqual(str(new_zip.resolve()), manifest["discovery"]["movieAudioPreflights"][0]["subtitleZip"]["path"])

    def test_corrupt_cache_falls_back_to_full_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root, harness, _video, _subtitle = self.make_tv(directory)
            harness.run(harness.args())
            before = harness.calls.copy()
            backend_cache_path(root).write_text("{", encoding="utf-8")

            harness.run(harness.args(rerun=True))

            self.assertEqual(before["library"] + 1, harness.calls["library"])
            self.assertEqual(before["media:source.mkv"] + 1, harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"] + 1, harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"] + 1, harness.calls["fonts"])

    def test_incompatible_component_cache_falls_back_to_full_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root, harness, _video, _subtitle = self.make_tv(directory)
            harness.run(harness.args())
            before = harness.calls.copy()
            manifest = json.loads(backend_cache_path(root).read_text(encoding="utf-8"))
            manifest["preflightCache"]["version"] -= 1
            backend_cache_path(root).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            harness.run(harness.args(rerun=True))

            self.assertEqual(before["library"] + 1, harness.calls["library"])
            self.assertEqual(before["media:source.mkv"] + 1, harness.calls["media:source.mkv"])
            self.assertEqual(before["subtitle:source.ass"] + 1, harness.calls["subtitle:source.ass"])
            self.assertEqual(before["fonts"] + 1, harness.calls["fonts"])

    def test_public_rerun_flag_is_forwarded_to_backend_inspect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inspect_step = importlib.import_module("steps.inspect")
            state = {
                "task": "complete-archive",
                "branch": "tv",
                "decisions": {},
                "requested_steps": None,
            }
            with mock.patch.object(inspect_step, "backend_command", return_value={"status": "COMPLETE"}) as command:
                inspect_step.run({"work_dir": root, "state": state, "args": Namespace(rerun=True)})
            self.assertIn("--rerun", command.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
