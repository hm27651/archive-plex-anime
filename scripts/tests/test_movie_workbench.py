from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from internal.movie_workbench import audio_basis, build_movie_plan, inspect_sources, signature, source_path
from internal.errors import WorkflowError


class MovieWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "电影"
        self.work.mkdir()
        self.output = self.root / "暂存"
        self.output.mkdir()
        self.config = {"paths": {}, "tools": {}}
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.manifest = {"configPath": str(self.config_path), "discovery": {}, "taskMode": "replacement"}
        for name in ("video.mkv", "audio.mka", "disc.m2ts", "disc.mkv", "other.mkv"):
            (self.work / name).write_bytes(b"media")

    def inventory(self, audio="audio.mka"):
        def file(name, tracks, kind):
            return {
                "id": name,
                "path": name,
                "signature": signature(self.work / name),
                "kind": kind,
                "tracks": tracks,
                "chapters": False,
            }

        video = {
            "id": "uid:video",
            "mux_id": 0,
            "type": "video",
            "codec": "V_MPEG4/ISO/AVC",
            "language": "jpn",
            "title": "",
            "default": True,
        }
        sound = {
            "id": "uid:audio",
            "mux_id": 3,
            "type": "audio",
            "codec": "A_FLAC",
            "language": "jpn",
            "title": "JPN Main 5.1",
            "default": False,
            "channels": 6,
        }
        return {"files": [file("video.mkv", [video], "video"), file(audio, [sound], "audio")], "issues": []}

    def target(self, audio="audio.mka"):
        ref = {"source": audio, "track": "uid:audio"}
        return {"id": "one", "video": "video.mkv", "part": "", "audio": [ref], "default_audio": ref, "subtitles": []}

    def build(self, target, inventory):
        with (
            patch("internal.movie_workbench.inspect_sources", return_value=inventory),
            patch("internal.movie_workbench.artifact_output_root", return_value=self.output),
        ):
            return build_movie_plan(
                self.work,
                self.manifest,
                {"title": "电影", "release_group": "Group", "movie_plan": {"schema_version": 2, "targets": [target]}},
            )

    def test_all_external_formats_produce_stream_copy_jobs(self):
        for source in ("audio.mka", "disc.m2ts", "disc.mkv"):
            with self.subTest(source=source):
                self.manifest["discovery"] = {}
                inventory = self.inventory(source)
                target = self.target(source)
                target["external_confirmation"] = {"basis": audio_basis(target, inventory), "actor": "owner"}
                generated = self.build(target, inventory)
                self.assertEqual(generated["issues"], [])
                job = generated["plan"]["remuxJobs"][0]
                argument_paths = {str(Path(value).resolve()) for value in job["arguments"] if Path(value).suffix}
                self.assertIn(str((self.work / source).resolve()), argument_paths)
                self.assertIn("--audio-tracks", job["arguments"])
                self.assertNotIn("--sync", job["arguments"])
                self.assertEqual(job["expectedTracks"][1]["codecId"], "A_FLAC")
                self.assertEqual(generated["plan"]["movieAudioPlans"], [])

    def test_external_confirmation_is_bound_to_input_fact_and_tracks(self):
        inventory = self.inventory()
        target = self.target()
        generated = self.build(target, inventory)
        self.assertIn("MOVIE_EXTERNAL_AUDIO_CONFIRMATION_REQUIRED", [i["code"] for i in generated["issues"]])
        target["external_confirmation"] = {"basis": audio_basis(target, inventory)}
        changed = deepcopy(inventory)
        changed["files"][1]["signature"]["size"] += 1
        self.assertIn(
            "MOVIE_EXTERNAL_AUDIO_CONFIRMATION_REQUIRED", [i["code"] for i in self.build(target, changed)["issues"]]
        )

    def test_invalid_tracks_and_missing_defaults_are_actionable(self):
        target = self.target()
        target["audio"][0]["track"] = "uid:missing"
        codes = {i["code"] for i in self.build(target, self.inventory())["issues"]}
        self.assertIn("MOVIE_AUDIO_TRACK_INVALID", codes)
        self.assertIn("MAIN_AUDIO_REQUIRED", codes)

    def test_inventory_never_classifies_mka_as_video_and_does_not_write_state(self):
        raw = {
            "tracks": [
                {
                    "id": 0,
                    "type": "audio",
                    "codec": "FLAC",
                    "properties": {
                        "uid": 12345678901234567890,
                        "codec_id": "A_FLAC",
                        "language": "jpn",
                        "audio_channels": 2,
                    },
                }
            ]
        }
        with (
            patch("internal.movie_workbench.read_mkvmerge_json", return_value=raw),
            patch("internal.movie_workbench._tool", return_value="mkvmerge"),
        ):
            result = inspect_sources(self.work, self.config, ["audio.mka"])
        self.assertEqual(result["files"][0]["kind"], "audio")
        self.assertEqual(result["files"][0]["tracks"][0]["id"], "uid:12345678901234567890")
        self.assertFalse((self.work / ".archive-state.json").exists())
        self.assertEqual(result["subtitle_type_order"], ["JASC", "SC", "JATC", "TC"])

    def test_source_references_cannot_escape(self):
        for source in ("../config.json", str(self.config_path), "C:/outside.mka"):
            with self.assertRaises(WorkflowError):
                source_path(self.work, source)

    def test_selected_ass_missing_font_names_source_and_target(self):
        ass = self.work / "字幕.SC.Group.ass"
        ass.write_text("[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default, NotInstalledFont\n", encoding="utf-8")
        inventory = self.inventory()
        inventory["files"].append({"id": ass.name, "kind": "subtitle", "tracks": [], "signature": signature(ass)})
        target = self.target()
        target["external_confirmation"] = {"basis": audio_basis(target, inventory)}
        target["subtitles"] = [{"source": ass.name, "track": "", "group": "Group", "subtitle_type": "SC"}]
        result = self.build(target, inventory)
        missing = next(i for i in result["issues"] if i["code"] == "FONT_NOT_FOUND")
        self.assertEqual(missing["source"], ass.name)
        self.assertEqual(missing["target_id"], "one")
        self.assertEqual(missing["font"], "NotInstalledFont")
        self.assertFalse((self.work / "字幕.SC.Group.assfonts.ass").exists())


if __name__ == "__main__":
    unittest.main()
