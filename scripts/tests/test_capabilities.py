from __future__ import annotations

import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import capabilities  # noqa: E402
import workflow  # noqa: E402
from common import load_state, save_state  # noqa: E402
from internal.preflight import inspection_required_tools  # noqa: E402
from internal.final_delivery import execute_final_delivery  # noqa: E402


ALL_TV = {
    "inspect",
    "metadata",
    "subtitle",
    "remux",
    "subtitle-package",
    "video-delivery",
    "subtitle-delivery",
    "kdocs-tracker",
    "cleanup",
}


class CapabilityRuleTests(unittest.TestCase):
    def resolve(self, **values):
        return capabilities.resolve_capabilities(
            selection_mode=values.get("selection_mode", "preset"),
            preset=values.get("preset", "complete-archive"),
            requested=values.get("requested", []),
            entrypoint=values.get("entrypoint", "cli"),
            branch=values.get("branch", "tv"),
            available=values.get("available", ALL_TV),
        )

    def test_presets_keep_existing_step_shapes(self):
        for preset in ("complete-archive", "replacement"):
            resolved = self.resolve(preset=preset)
            self.assertEqual(
                ["inspect", "subtitle", "remux", "package", "review", "finalize", "cleanup"],
                resolved["selected_steps"],
            )
        archive_only = self.resolve(
            preset="archive-only",
            available={"inspect", "metadata", "video-delivery", "kdocs-tracker", "cleanup"},
        )
        self.assertEqual(["inspect", "review", "finalize", "cleanup"], archive_only["selected_steps"])
        local_only = self.resolve(
            preset="local-only",
            branch="movie",
            requested=["remux"],
            available={"inspect", "metadata", "movie-audio", "subtitle", "remux"},
        )
        self.assertEqual(["inspect", "movie-audio", "subtitle", "remux"], local_only["selected_steps"])

    def test_custom_selection_auto_adds_media_dependencies(self):
        resolved = self.resolve(
            selection_mode="custom",
            branch="movie",
            requested=["remux"],
            available={"inspect", "metadata", "movie-audio", "subtitle", "remux"},
        )
        self.assertEqual(["inspect", "movie-audio", "subtitle", "remux"], resolved["resolved_capabilities"])
        self.assertEqual(["inspect", "movie-audio", "subtitle"], resolved["auto_added_capabilities"])
        self.assertEqual([], resolved["issues"])

    def test_cleanup_auto_adds_video_delivery_and_rejects_it_when_unavailable(self):
        resolved = self.resolve(
            selection_mode="custom",
            requested=["subtitle-delivery", "cleanup"],
            entrypoint="hub",
            available={
                "inspect",
                "subtitle",
                "subtitle-package",
                "video-delivery",
                "subtitle-delivery",
                "cleanup",
            },
        )
        self.assertIn("video-delivery", resolved["auto_added_capabilities"])
        self.assertEqual(["video", "subtitle_zip"], resolved["final_sinks"])
        self.assertIn("cleanup", resolved["selected_steps"])

        unavailable = self.resolve(
            selection_mode="custom",
            requested=["subtitle-delivery", "cleanup"],
            entrypoint="hub",
            available={"inspect", "subtitle", "subtitle-package", "subtitle-delivery"},
        )
        self.assertIn("CAPABILITY_UNAVAILABLE", {item["code"] for item in unavailable["issues"]})
        self.assertNotIn("cleanup", unavailable["selected_steps"])

    def test_invalid_branch_preset_and_inspect_only_requests_are_explicit(self):
        tv_audio = self.resolve(selection_mode="custom", requested=["movie-audio"], available=None)
        self.assertIn("CAPABILITY_NOT_VISIBLE", {item["code"] for item in tv_audio["issues"]})
        archive_remux = self.resolve(
            selection_mode="custom",
            preset="archive-only",
            requested=["remux"],
            available=None,
        )
        self.assertIn("ARCHIVE_ONLY_CAPABILITY_UNSUPPORTED", {item["code"] for item in archive_remux["issues"]})
        inspect_only = self.resolve(selection_mode="custom", requested=["inspect"], available=None)
        self.assertIn("CUSTOM_EXECUTABLE_CAPABILITY_REQUIRED", {item["code"] for item in inspect_only["issues"]})

    def test_hub_catalog_and_presets_never_expose_kdocs(self):
        catalog = capabilities.capability_catalog("hub", "tv")
        ids = {item["id"] for item in catalog["capabilities"]}
        self.assertNotIn("kdocs-tracker", ids)
        self.assertTrue(all("kdocs-tracker" not in item["capabilities"] for item in catalog["presets"]))
        resolved = self.resolve(entrypoint="hub")
        self.assertNotIn("kdocs-tracker", resolved["resolved_capabilities"])
        self.assertNotIn("tracker", resolved["final_sinks"])

    def test_final_sink_filter_removes_unselected_actions(self):
        plan = {"final": {"video": [{"source": "v"}], "zip": [{"source": "z"}]}}
        capabilities.apply_final_sinks(plan, ["video"])
        self.assertEqual([{"source": "v"}], plan["final"]["video"])
        self.assertEqual([], plan["final"]["zip"])
        self.assertFalse(plan["final"]["tracker"])

    def test_hub_tool_check_does_not_require_kdocs(self):
        args = Namespace(
            task_mode="complete-archive",
            requested_step=[],
            selected_capability=["inspect", "metadata", "video-delivery"],
            movie_audio_replacement=False,
            kdocs_tracker=False,
        )
        selected = inspection_required_tools(args, {"tracker": {"enabled": True}}, has_external_subtitles=True)
        self.assertNotIn("kdocs-cli", selected)

    def test_unselected_tracker_has_no_final_worker_or_kdocs_call(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker_apply = mock.Mock()
            stages = []
            output = execute_final_delivery(
                Path(directory),
                {"video": [], "zip": [], "tracker": False},
                "batch",
                tracker_apply=tracker_apply,
                on_stage=lambda stage, status, _payload: stages.append((stage, status)),
            )
        self.assertEqual("COMPLETE", output["status"])
        self.assertNotIn("final-tracker", output["completed"])
        self.assertEqual([], stages)
        tracker_apply.assert_not_called()

    def test_reinitializing_selection_invalidates_old_progress_and_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def args(capability: str) -> Namespace:
                return Namespace(
                    work_dir=str(root),
                    branch="tv",
                    task="complete-archive",
                    steps=None,
                    capabilities=capability,
                    entrypoint="cli",
                    decisions_stdin=False,
                )

            with mock.patch.object(workflow, "load_config", return_value={}):
                workflow.init_state(args("remux"))
                state = load_state(root)
                state["completed_steps"] = ["inspect", "remux"]
                state["approvals"] = {"preflight": True, "final": True}
                save_state(root, state)
                workflow.init_state(args("subtitle"))
            reset = load_state(root)
            self.assertEqual("local-only", reset["task"])
            self.assertEqual(["subtitle"], reset["requested_capabilities"])
            self.assertEqual([], reset["completed_steps"])
            self.assertEqual({"preflight": False, "final": False}, reset["approvals"])


if __name__ == "__main__":
    unittest.main()
