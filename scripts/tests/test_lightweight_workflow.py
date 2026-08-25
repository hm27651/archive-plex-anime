from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("lightweight_workflow", SCRIPTS / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(workflow)

from common import backend_cache_path, backend_command, load_state, read_text, run_process, save_state, state_path, write_json_atomic  # noqa: E402
from archive_rules import BACKEND_CACHE_SCHEMA, RULES_VERSION, STATE_SCHEMA  # noqa: E402
from steps.finalize import FinalizeProgress, run as finalize_run  # noqa: E402
from steps.movie_audio import run as movie_audio_run  # noqa: E402
from steps.package import run as package_run  # noqa: E402
from steps.remux import run as remux_run  # noqa: E402
from steps.review import run as review_run  # noqa: E402
from steps.inspect import public_metadata_summary  # noqa: E402


def init_args(root: Path, *, branch: str = "tv", task: str = "complete-archive") -> Namespace:
    return Namespace(work_dir=str(root), branch=branch, task=task, steps=None, decisions_stdin=False)


def valid_state(**values) -> dict:
    return {"schema": STATE_SCHEMA, "rules_version": RULES_VERSION, **values}


class LightweightWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config_patcher = mock.patch.object(workflow, "load_config", return_value={"paths": {}})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()

    def test_public_metadata_summary_explains_automatic_query_source(self):
        summary = public_metadata_summary(
            {
                "status": "MATCHED",
                "mode": "auto",
                "query": "Gakkou Gurashi!",
                "querySource": "media-common-title",
                "queryCandidates": ["Gakkou Gurashi!"],
                "episodes": [],
                "suggestedDecisions": {},
            }
        )

        self.assertEqual("media-common-title", summary["querySource"])
        self.assertEqual(["Gakkou Gurashi!"], summary["queryCandidates"])

    def test_internal_decision_status_is_normalized_at_the_public_boundary(self):
        self.assertEqual("NEEDS_USER", workflow.WorkflowIssue("DECISION_REQUIRED", "choose").status)

    def test_inspect_backend_forwards_local_capability_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "task": "local-only",
                "branch": "tv",
                "requested_steps": ["subtitle", "package"],
                "decisions": {},
            }
            with mock.patch.object(workflow, "backend_command", return_value={"status": "COMPLETE"}) as backend:
                workflow.inspect_backend(root, state)
            extra = backend.call_args.args[2]
            self.assertEqual(2, extra.count("--requested-step"))
            self.assertIn("subtitle", extra)
            self.assertIn("package", extra)

    def test_inspect_backend_forwards_metadata_as_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "task": "complete-archive", "branch": "tv", "requested_steps": None,
                "resolved_capabilities": ["inspect", "metadata"],
                "decisions": {"metadata": {"query": "中文作品", "tmdb_id": 123}},
            }
            with mock.patch.object(workflow, "backend_command", return_value={"status": "COMPLETE"}) as backend:
                workflow.inspect_backend(root, state)
            extra = backend.call_args.args[2]
            payload = extra[extra.index("--metadata-json") + 1]
            self.assertEqual("中文作品", json.loads(payload)["query"])

    def test_inspect_backend_does_not_forward_unselected_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "task": "local-only",
                "branch": "tv",
                "requested_steps": ["remux"],
                "resolved_capabilities": ["inspect", "remux"],
                "decisions": {"metadata": {"query": "不应联网"}},
            }
            with mock.patch.object(workflow, "backend_command", return_value={"status": "COMPLETE"}) as backend:
                workflow.inspect_backend(root, state)
            self.assertNotIn("--metadata-json", backend.call_args.args[2])

    def test_decisions_stdin_accepts_utf8_chinese_json(self):
        args = Namespace(decisions_stdin=True)
        payload = '\ufeff{"title":"异世界魔王","subtitle_order":["喵萌","樱都"]}'.encode("utf-8")
        decisions = workflow.load_decisions(args, io.BytesIO(payload))
        self.assertEqual("异世界魔王", decisions["title"])
        self.assertEqual(["喵萌", "樱都"], decisions["subtitle_order"])

    def test_redirected_workflow_stdout_and_stderr_are_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_state(
                root,
                valid_state(
                    work_dir=str(root),
                    branch="tv",
                    task="complete-archive",
                    selected_steps=["inspect"],
                    completed_steps=[],
                    approvals={"preflight": False, "final": False},
                    decisions={"title": "中文作品"},
                ),
            )
            environment = os.environ.copy()
            for key in ("PYTHONUTF8", "PYTHONIOENCODING"):
                environment.pop(key, None)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "workflow.py"), "status", "--work-dir", str(root)],
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(0, completed.returncode)
            stdout = completed.stdout.decode("utf-8")
            self.assertEqual("中文作品", json.loads(stdout)["state"]["decisions"]["title"])

            missing = root / "中文目录"
            failed = subprocess.run(
                [sys.executable, str(SCRIPTS / "workflow.py"), "subtitle", "--work-dir", str(missing)],
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertNotEqual(0, failed.returncode)
            stderr = failed.stderr.decode("utf-8")
            self.assertIn("中文目录", json.loads(stderr)["error"])

    def test_decisions_stdin_rejects_non_object_input(self):
        with self.assertRaises(workflow.WorkflowIssue):
            workflow.load_decisions(Namespace(decisions_stdin=True), io.BytesIO(b"[]"))

    def test_metadata_decisions_reject_api_secrets_and_task_proxy(self):
        for field in ("api_key", "token", "proxy"):
            with self.subTest(field=field), self.assertRaises(workflow.WorkflowIssue):
                payload = json.dumps({"metadata": {field: "secret"}}).encode("utf-8")
                workflow.load_decisions(Namespace(decisions_stdin=True), io.BytesIO(payload))

    def test_progress_process_preserves_structured_output(self):
        calls = []
        output = run_process(
            [sys.executable, "-c", "import json; print(json.dumps({'status':'COMPLETE'}))"],
            progress=lambda: calls.append(True),
        )
        self.assertEqual(0, output["code"])
        self.assertEqual("COMPLETE", json.loads(output["stdout"])["status"])
        self.assertTrue(calls)

    def test_init_state_contains_only_step_level_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(workflow, "load_decisions", return_value={"release_group": "测试"}):
                output = workflow.init_state(init_args(root))
            state = load_state(root)
            self.assertEqual("COMPLETE", output["status"])
            self.assertEqual(["inspect"], state["selected_steps"])
            self.assertIsNone(state["requested_steps"])
            self.assertEqual([], state["completed_steps"])
            self.assertEqual(RULES_VERSION, state["rules_version"])
            self.assertEqual("测试", state["decisions"]["release_group"])
            self.assertNotIn("outputs", state)
            self.assertTrue(state_path(root).is_file())

    def test_init_rejects_local_only_review_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = init_args(root, task="local-only")
            args.steps = "remux,review"
            with mock.patch.object(workflow, "load_config", return_value={"paths": {}}), mock.patch.object(
                workflow, "load_decisions", return_value={}
            ):
                with self.assertRaises(workflow.WorkflowIssue) as caught:
                    workflow.init_state(args)
            self.assertEqual("NEEDS_USER", caught.exception.status)
            self.assertIn("LOCAL_STEP_UNSUPPORTED", str(caught.exception))
            self.assertFalse(state_path(root).exists())

    def test_init_rejects_a_missing_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(workflow.WorkflowIssue) as caught:
                workflow.init_state(init_args(missing))
            self.assertEqual("FAILED", caught.exception.status)
            self.assertFalse(missing.exists())

    def test_invalid_state_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json_atomic(state_path(root), {"completed_steps": ["inspect", "subtitle"], "selected_steps": ["inspect", "subtitle"]})
            with self.assertRaisesRegex(workflow.WorkflowIssue, "task state contract mismatch"):
                workflow.load_task_state(root)

    def test_state_round_trip_preserves_chinese_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_state(root, valid_state(title="夏日口袋", path="F:\\pt&bt\\anime\\TV"))
            self.assertIn("夏日口袋", read_text(state_path(root)))
            self.assertEqual("夏日口袋", load_state(root)["title"])

    def test_movie_replacement_defers_step_selection_to_unified_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(workflow, "load_decisions", return_value={"m2ts": "disc.m2ts"}):
                workflow.init_state(init_args(root, branch="movie", task="replacement"))
            selected = load_state(root)["selected_steps"]
            self.assertEqual(["inspect"], selected)

    def test_movie_audio_requires_a_unified_backend_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("steps.movie_audio.backend_command", side_effect=workflow.WorkflowIssue("FAILED", "plan missing")):
                with self.assertRaises(workflow.WorkflowIssue):
                    movie_audio_run({"work_dir": Path(directory), "state": {"decisions": {}}, "args": Namespace()})

    def test_movie_audio_runs_through_the_unified_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "steps.movie_audio.backend_command",
                return_value={"status": "COMPLETE", "output": str(root / "movie-audio.mkv"), "reused": False},
            ) as backend:
                output = movie_audio_run({"work_dir": root, "state": {"decisions": {}}, "args": Namespace()})
            self.assertEqual("COMPLETE", output["status"])
            self.assertEqual("movie-audio", backend.call_args.args[1])
            self.assertTrue(backend.call_args.kwargs["execute"])

    def test_preflight_requires_inspect_and_internal_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            with self.assertRaises(workflow.WorkflowIssue):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root)))

            state = load_state(root)
            state["completed_steps"] = ["inspect"]
            save_state(root, state)
            with self.assertRaises(workflow.WorkflowIssue):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root)))

            manifest = {
                "route": {"status": "OK", "branch": "anime"},
                "discovery": {"subtitles": [], "embeddedSubtitles": {"status": "COMPLETE"}},
                "plan": {"title": "作品"},
            }
            write_json_atomic(backend_cache_path(root), manifest)
            with mock.patch.object(workflow, "configure_backend", return_value={"status": "COMPLETE"}):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root)))
            self.assertTrue(load_state(root)["approvals"]["preflight"])

    def test_preflight_approval_materializes_metadata_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            state = load_state(root)
            state["completed_steps"] = ["inspect"]
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {
                    "route": {"status": "OK", "branch": "anime"},
                    "discovery": {
                        "subtitles": [], "embeddedSubtitles": {"status": "COMPLETE"},
                        "metadata": {
                            "status": "MATCHED", "issues": [],
                            "suggestedDecisions": {
                                "title": "规范标题",
                                "metadata": {"mode": "auto", "tmdb_id": 10, "episode_order": "tmdb"},
                                "episode_map": {"S1/source.mkv": "S01E01"},
                            },
                        },
                    },
                    "plan": {"title": "作品"},
                },
            )
            with mock.patch.object(workflow, "configure_backend", return_value={"status": "COMPLETE"}):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root), decisions_stdin=False))
            decisions = load_state(root)["decisions"]
            self.assertEqual("规范标题", decisions["title"])
            self.assertEqual(10, decisions["metadata"]["tmdb_id"])
            self.assertEqual("S01E01", decisions["episode_map"]["S1/source.mkv"])

    def test_explicit_title_overrides_metadata_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            state = load_state(root)
            state["completed_steps"] = ["inspect"]
            state["decisions"]["title"] = "用户标题"
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {
                    "route": {"status": "OK", "branch": "anime"},
                    "discovery": {
                        "subtitles": [], "embeddedSubtitles": {"status": "COMPLETE"},
                        "metadata": {"status": "MATCHED", "issues": [], "suggestedDecisions": {"title": "API 标题"}},
                    },
                    "plan": {"title": "作品"},
                },
            )
            with mock.patch.object(workflow, "configure_backend", return_value={"status": "COMPLETE"}):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root), decisions_stdin=False))
            self.assertEqual("用户标题", load_state(root)["decisions"]["title"])

    def test_user_episode_map_overrides_one_metadata_entry_without_losing_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            state = load_state(root)
            state["completed_steps"] = ["inspect"]
            state["decisions"]["episode_map"] = {"S0/OVA.mkv": "S00E02"}
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {
                    "route": {"status": "OK", "branch": "anime"},
                    "discovery": {
                        "subtitles": [], "embeddedSubtitles": {"status": "COMPLETE"},
                        "metadata": {
                            "status": "MATCHED", "issues": [],
                            "suggestedDecisions": {"episode_map": {"S0/OVA.mkv": "S00E01", "S1/EP01.mkv": "S01E01"}},
                        },
                    },
                    "plan": {"title": "作品"},
                },
            )
            with mock.patch.object(workflow, "configure_backend", return_value={"status": "COMPLETE"}):
                workflow.approve(Namespace(kind="preflight", work_dir=str(root), decisions_stdin=False))
            self.assertEqual(
                {"S0/OVA.mkv": "S00E02", "S1/EP01.mkv": "S01E01"},
                load_state(root)["decisions"]["episode_map"],
            )

    def test_approval_gate_is_step_level(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            with self.assertRaises(RuntimeError):
                workflow.run_step(Namespace(command="subtitle", work_dir=str(root), rerun=False))

    def test_valid_needs_user_inspect_records_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            write_json_atomic(
                backend_cache_path(root),
                {
                    "schemaVersion": BACKEND_CACHE_SCHEMA,
                    "workflowRevision": workflow.WORKFLOW_REVISION,
                    "workPath": str(root),
                    "stages": {"inspect": {"status": "NEEDS_USER"}},
                    "plan": {},
                    "discovery": {},
                },
            )
            with mock.patch("steps.inspect.run", return_value={"status": "NEEDS_USER", "preflight": {"issues": [{"code": "CHOOSE"}]}}):
                output = workflow.run_step(Namespace(command="inspect", work_dir=str(root), rerun=False))
            self.assertEqual("NEEDS_USER", output["status"])
            self.assertEqual(["inspect"], load_state(root)["completed_steps"])

    def test_invalid_needs_user_cache_does_not_record_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_values = {
                "broken-json": "{",
                "wrong-schema": json.dumps(
                    {
                        "schemaVersion": BACKEND_CACHE_SCHEMA - 1,
                        "workflowRevision": workflow.WORKFLOW_REVISION,
                        "workPath": str(root),
                        "stages": {"inspect": {"status": "NEEDS_USER"}},
                    }
                ),
                "wrong-revision": json.dumps(
                    {
                        "schemaVersion": BACKEND_CACHE_SCHEMA,
                        "workflowRevision": "old",
                        "workPath": str(root),
                        "stages": {"inspect": {"status": "NEEDS_USER"}},
                    }
                ),
                "wrong-work": json.dumps(
                    {
                        "schemaVersion": BACKEND_CACHE_SCHEMA,
                        "workflowRevision": workflow.WORKFLOW_REVISION,
                        "workPath": str(root / "other"),
                        "stages": {"inspect": {"status": "NEEDS_USER"}},
                    }
                ),
                "missing-stage": json.dumps(
                    {
                        "schemaVersion": BACKEND_CACHE_SCHEMA,
                        "workflowRevision": workflow.WORKFLOW_REVISION,
                        "workPath": str(root),
                        "stages": {},
                    }
                ),
            }
            for name, payload in invalid_values.items():
                with self.subTest(name=name):
                    workflow.init_state(init_args(root))
                    path = backend_cache_path(root)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(payload, encoding="utf-8")
                    with mock.patch("steps.inspect.run", return_value={"status": "NEEDS_USER"}):
                        workflow.run_step(Namespace(command="inspect", work_dir=str(root), rerun=False))
                    self.assertEqual([], load_state(root)["completed_steps"])

    def test_failed_inspect_does_not_record_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            with mock.patch("steps.inspect.run", return_value={"status": "FAILED"}):
                output = workflow.run_step(Namespace(command="inspect", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])
            self.assertEqual([], load_state(root)["completed_steps"])

    def test_rerun_invalidates_all_public_downstream_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = ["inspect", "subtitle", "remux", "package", "review", "finalize", "cleanup"]
            save_state(
                root,
                valid_state(
                    work_dir=str(root),
                    branch="tv",
                    task="complete-archive",
                    requested_steps=None,
                    selected_steps=selected,
                    completed_steps=selected[:-1],
                    approvals={"preflight": True, "final": True},
                    decisions={},
                    final_target={"batch_id": "old"},
                    final_results={"batch_id": "old", "tracker": {"status": "COMPLETE"}},
                ),
            )
            write_json_atomic(backend_cache_path(root), {"schemaVersion": BACKEND_CACHE_SCHEMA})
            with mock.patch("steps.subtitle.run", return_value={"status": "COMPLETE"}):
                workflow.run_step(Namespace(command="subtitle", work_dir=str(root), rerun=True))
            refreshed = load_state(root)
            self.assertEqual(["inspect", "subtitle"], refreshed["completed_steps"])
            self.assertFalse(refreshed["approvals"]["final"])
            self.assertNotIn("final_target", refreshed)
            self.assertNotIn("final_results", refreshed)

    def test_backend_invalidation_is_mirrored_into_public_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = ["inspect", "subtitle", "remux", "review", "finalize", "cleanup"]
            state = valid_state(
                work_dir=str(root),
                branch="tv",
                task="complete-archive",
                requested_steps=None,
                selected_steps=selected,
                completed_steps=selected[:-1],
                approvals={"preflight": True, "final": True},
                decisions={},
                final_results={"batch_id": "old"},
            )
            write_json_atomic(backend_cache_path(root), {"plan": {}, "discovery": {}})
            generated = {
                "plan": {"title": "作品", "remuxJobs": [{"output": "x"}], "final": {"video": [{"source": "x"}]}},
                "issues": [],
                "selected_steps": selected,
            }
            with mock.patch.object(workflow, "build_plan", return_value=generated), mock.patch.object(
                workflow, "backend_command", return_value={"status": "COMPLETE", "invalidatedFrom": "remux"}
            ):
                workflow.configure_backend(root, state)
            refreshed = load_state(root)
            self.assertEqual(["inspect", "subtitle"], refreshed["completed_steps"])
            self.assertFalse(refreshed["approvals"]["final"])
            self.assertNotIn("final_results", refreshed)

    def test_decision_error_after_preflight_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_state(root, valid_state(approvals={"preflight": True, "final": False}))
            payload = json.dumps({"status": "NEEDS_USER", "error": "changed"}, ensure_ascii=False)
            with mock.patch("common.load_config", return_value={"tools": {"python": sys.executable}}), mock.patch(
                "common.run_process", return_value={"code": 2, "stdout": "", "stderr": payload}
            ):
                with self.assertRaises(workflow.WorkflowIssue) as caught:
                    backend_command(root, "remux", [])
            self.assertEqual("FAILED", caught.exception.status)

    def test_direct_step_needs_user_after_preflight_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_state(
                root,
                valid_state(
                    selected_steps=["inspect", "subtitle"], completed_steps=["inspect"],
                    approvals={"preflight": True, "final": False}, decisions={},
                ),
            )
            write_json_atomic(backend_cache_path(root), {"schemaVersion": BACKEND_CACHE_SCHEMA})
            with mock.patch("steps.subtitle.run", return_value={"status": "NEEDS_USER", "summary": "changed"}):
                output = workflow.run_step(Namespace(command="subtitle", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])

    def test_complete_flow_defers_remux_and_package_validation_to_review(self):
        context = {"work_dir": Path("X:/task"), "state": {"selected_steps": ["remux", "package", "review"]}, "args": Namespace()}
        with mock.patch("steps.remux.backend_command", return_value={"status": "COMPLETE"}) as remux_command:
            self.assertEqual("COMPLETE", remux_run(context)["status"])
        with mock.patch("steps.package.backend_command", return_value={"status": "COMPLETE"}) as package_command:
            self.assertEqual("COMPLETE", package_run(context)["status"])
        self.assertIn("--defer-output-validation", remux_command.call_args.args[2])
        self.assertIn("--defer-output-validation", package_command.call_args.args[2])

    def test_local_steps_keep_immediate_output_validation_without_review(self):
        context = {"work_dir": Path("X:/task"), "state": {"selected_steps": ["remux", "package"]}, "args": Namespace()}
        with mock.patch("steps.remux.backend_command", return_value={"status": "COMPLETE"}) as remux_command:
            remux_run(context)
        with mock.patch("steps.package.backend_command", return_value={"status": "COMPLETE"}) as package_command:
            package_run(context)
        self.assertNotIn("--defer-output-validation", remux_command.call_args.args[2])
        self.assertNotIn("--defer-output-validation", package_command.call_args.args[2])

    def test_review_stores_complete_final_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = valid_state(branch="tv", approvals={"final": True}, decisions={})
            destination = root / "Anime" / "作品" / "Season 1" / "作品 - S01E01.mkv"
            final = {
                "library": "Anime3",
                "mode": "create",
                "batchId": "batch-1",
                "batchDigest": "digest-1",
                "final": {
                    "video": [{"destination": str(destination)}],
                    "zip": [{"destination": str(root / "Anime（子集化）" / "作品.zip")}],
                    "trackerPlan": {"column": "Anime3"},
                },
            }
            local = {
                "status": "COMPLETE",
                "videos": [{"path": "one.mkv"}, {"path": "two.mkv"}],
                "zip": {"entries": ["one.ass", "two.ass"]},
                "warnings": ["兼容提示"],
            }
            with mock.patch("steps.review.backend_command", side_effect=[local, final]):
                output = review_run({"work_dir": root, "state": state, "args": Namespace()})
            target = load_state(root)["final_target"]
            self.assertEqual("COMPLETE", output["status"])
            self.assertEqual("Anime3", target["library"])
            self.assertEqual(str(root / "Anime" / "作品"), target["video_root"])
            self.assertEqual("Anime3", target["tracker_column"])
            self.assertEqual("batch-1", target["batch_id"])
            self.assertEqual("digest-1", target["batch_digest"])
            self.assertFalse(load_state(root)["approvals"]["final"])
            self.assertEqual({"videos": 2, "zip": True, "zip_entries": 2}, output["artifacts"])
            self.assertEqual(["兼容提示"], output["warnings"])
            self.assertNotIn("plan", output)

    def test_review_uses_media_hierarchy_for_titles_starting_with_s_or_containing_dots(self):
        for title in ("Summer", "Title.2024"):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = valid_state(branch="tv", approvals={"final": True}, decisions={})
                destination = root / "Anime" / title / "S1" / f"{title}.S01E01.mkv"
                final = {
                    "library": "Anime3",
                    "mode": "create",
                    "batchId": "batch-1",
                    "final": {"video": [{"destination": str(destination)}], "zip": []},
                }
                local = {"status": "COMPLETE", "videos": [{"path": "one.mkv"}], "zip": None, "warnings": []}
                with mock.patch("steps.review.backend_command", side_effect=[local, final]):
                    review_run({"work_dir": root, "state": state, "args": Namespace()})
                self.assertEqual(str(root / "Anime" / title), load_state(root)["final_target"]["video_root"])

    def test_finalize_progress_reports_only_checkpoint_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "finalPreparation": {
                    "final": {
                        "video": [{"destination": "one"}, {"destination": "two"}],
                        "zip": [{"destination": "subtitles"}],
                        "trackerPlan": {"column": "Anime3"},
                    }
                }
            }
            write_json_atomic(backend_cache_path(root), manifest)
            save_state(root, valid_state())
            progress = FinalizeProgress(root, "batch-1")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                progress()
                progress()
                save_state(
                    root,
                    valid_state(**{
                        "final_results": {
                            "batch_id": "batch-1",
                            "video": {"one": {"status": "COMPLETE", "size": 1}},
                            "zip": {"subtitles": {"status": "COMPLETE", "size": 1}},
                            "tracker": {"status": "COMPLETE"},
                        }
                    }),
                )
                progress()
            lines = stderr.getvalue().splitlines()
            self.assertEqual(2, len(lines))
            self.assertIn("视频 0/2 | ZIP 等待 | 维护表 等待", lines[0])
            self.assertIn("视频 1/2 | ZIP 完成 | 维护表 完成", lines[1])

    def test_finalize_forwards_the_approved_batch_digest(self):
        state = valid_state(
            decisions={"batch_id": "batch-1"},
            approved_final_digest="digest-1",
        )
        with mock.patch(
            "steps.finalize.backend_command",
            return_value={"status": "COMPLETE", "warnings": []},
        ) as backend:
            output = finalize_run(
                {"work_dir": Path("X:/task"), "state": state, "args": Namespace()}
            )
        self.assertEqual("COMPLETE", output["status"])
        self.assertEqual(
            ["--approved-batch", "batch-1", "--approved-digest", "digest-1"],
            backend.call_args.args[2],
        )

    def test_final_approval_rejects_incomplete_or_changed_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            with self.assertRaises(workflow.WorkflowIssue):
                workflow.approve(Namespace(kind="final", work_dir=str(root)))
            state = load_state(root)
            state["final_target"] = {
                "library": "Anime3",
                "video_root": "T:\\Anime\\作品",
                "zip": "T:\\Anime（子集化）\\作品.zip",
                "tracker_column": "Anime3",
                "operation": "create",
                "batch_id": "batch-1",
                "batch_digest": "digest-1",
            }
            state["completed_steps"] = ["review"]
            save_state(root, state)
            with self.assertRaises(workflow.WorkflowIssue):
                workflow.approve(Namespace(kind="final", work_dir=str(root), library="Anime2"))

    def test_final_approval_records_the_reviewed_batch_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.init_state(init_args(root))
            state = load_state(root)
            state["final_target"] = {
                "library": "Anime3",
                "video_root": "T:\\Anime\\作品",
                "zip": "",
                "tracker_column": "Anime3",
                "operation": "create",
                "batch_id": "batch-1",
                "batch_digest": "digest-1",
            }
            state["completed_steps"] = ["review"]
            save_state(root, state)
            output = workflow.approve(Namespace(kind="final", work_dir=str(root)))
            approved = output["state"]
            self.assertTrue(approved["approvals"]["final"])
            self.assertEqual("digest-1", approved["approved_final_digest"])

    def test_cleanup_rejects_a_final_batch_without_video_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            state = valid_state(**{
                "work_dir": str(root),
                "branch": "tv",
                "selected_steps": ["cleanup"],
                "completed_steps": ["finalize"],
                "final_sinks": ["subtitle_zip"],
                "approvals": {"preflight": True, "final": True},
                "decisions": {},
                "final_results": {"batch_id": "batch-1", "tracker": {"status": "COMPLETE"}},
            })
            save_state(root, state)
            write_json_atomic(backend_cache_path(root), {"schemaVersion": BACKEND_CACHE_SCHEMA, "finalPreparation": {"final": {"batchId": "batch-1", "video": [], "zip": []}}})
            with mock.patch("steps.cleanup.load_config", return_value={"paths": {"workRoot": directory}}):
                output = workflow.run_step(Namespace(command="cleanup", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])
            self.assertTrue(root.exists())

    def test_cleanup_removes_readonly_files_without_losing_retry_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            readonly = root / "font.ttc"
            readonly.write_bytes(b"font")
            readonly.chmod(stat.S_IREAD)
            source = root / "source.mkv"
            destination = Path(directory) / "nas" / "episode.mkv"
            destination.parent.mkdir()
            source.write_bytes(b"video")
            destination.write_bytes(b"video")
            state = valid_state(**{
                "work_dir": str(root),
                "branch": "tv",
                "selected_steps": ["cleanup"],
                "completed_steps": ["finalize"],
                "final_sinks": ["video"],
                "approvals": {"preflight": True, "final": True},
                "decisions": {},
                "final_results": {
                    "batch_id": "batch-1",
                    "video": {
                        str(destination): {
                            "status": "COMPLETE",
                            "size": destination.stat().st_size,
                            "source_size": source.stat().st_size,
                        }
                    },
                },
            })
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {
                    "schemaVersion": BACKEND_CACHE_SCHEMA,
                    "finalPreparation": {
                        "final": {
                            "batchId": "batch-1",
                            "video": [{"source": str(source), "destination": str(destination)}],
                            "zip": [],
                        }
                    },
                },
            )
            with mock.patch("steps.cleanup.load_config", return_value={"paths": {"workRoot": directory}}):
                output = workflow.run_step(Namespace(command="cleanup", work_dir=str(root), rerun=False))
            self.assertEqual("COMPLETE", output["status"])
            self.assertFalse(root.exists())

    def test_cleanup_rejects_a_task_outside_the_configured_branch_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            state = valid_state(**{
                "work_dir": str(root),
                "branch": "tv",
                "selected_steps": ["cleanup"],
                "completed_steps": ["finalize"],
                "final_sinks": ["video"],
                "approvals": {"preflight": True, "final": True},
                "decisions": {},
                "final_results": {"batch_id": "batch-1", "tracker": {"status": "COMPLETE"}},
            })
            save_state(root, state)
            write_json_atomic(backend_cache_path(root), {"schemaVersion": BACKEND_CACHE_SCHEMA, "finalPreparation": {"final": {"batchId": "batch-1", "video": [], "zip": []}}})
            with mock.patch("steps.cleanup.load_config", return_value={"paths": {"workRoot": str(Path(directory) / "other")}}):
                output = workflow.run_step(Namespace(command="cleanup", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])
            self.assertTrue(root.exists())

    def test_cleanup_revalidates_every_completed_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            missing_destination = Path(directory) / "nas" / "episode.mkv"
            state = valid_state(
                work_dir=str(root),
                branch="tv",
                selected_steps=["cleanup"],
                completed_steps=["finalize"],
                final_sinks=["video"],
                approvals={"preflight": True, "final": True},
                decisions={},
                final_results={
                    "batch_id": "batch-1",
                    "video": {str(missing_destination): {"status": "COMPLETE", "size": 5}},
                    "tracker": {"status": "COMPLETE"},
                },
            )
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {"finalPreparation": {"final": {"batchId": "batch-1", "video": [{"destination": str(missing_destination)}], "zip": []}}},
            )
            with mock.patch("steps.cleanup.load_config", return_value={"paths": {"workRoot": directory}}):
                output = workflow.run_step(Namespace(command="cleanup", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])
            self.assertTrue(root.exists())

    def test_cleanup_rejects_a_completed_destination_inside_the_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "task"
            root.mkdir()
            source = root / "source.mkv"
            destination = root / "nas" / "episode.mkv"
            destination.parent.mkdir()
            source.write_bytes(b"video")
            destination.write_bytes(b"video")
            state = valid_state(
                work_dir=str(root),
                branch="tv",
                selected_steps=["cleanup"],
                completed_steps=["finalize"],
                final_sinks=["video"],
                approvals={"preflight": True, "final": True},
                decisions={},
                final_results={
                    "batch_id": "batch-1",
                    "video": {
                        str(destination): {
                            "status": "COMPLETE",
                            "size": destination.stat().st_size,
                            "source_size": source.stat().st_size,
                        }
                    },
                    "tracker": {"status": "COMPLETE"},
                },
            )
            save_state(root, state)
            write_json_atomic(
                backend_cache_path(root),
                {
                    "finalPreparation": {
                        "final": {
                            "batchId": "batch-1",
                            "video": [{"source": str(source), "destination": str(destination)}],
                            "zip": [],
                        }
                    }
                },
            )
            with mock.patch("steps.cleanup.load_config", return_value={"paths": {"workRoot": directory}}):
                output = workflow.run_step(Namespace(command="cleanup", work_dir=str(root), rerun=False))
            self.assertEqual("FAILED", output["status"])
            self.assertTrue(root.exists())

    def test_restore_backend_invalidates_local_steps_without_guessing_numbered_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "S1"
            group = season / "SC 组"
            group.mkdir(parents=True)
            source_video = season / "Source [01].mkv"
            source_video.write_bytes(b"source")
            subtitle = group / "作品.S01E01.ass"
            video = season / "作品.S01E01.mkv"
            package = root / "作品.zip"
            subtitle.write_text("subtitle", encoding="utf-8")
            video.write_bytes(b"video")
            package.write_bytes(b"zip")
            manifest = {
                "plan": {
                    "renameJobs": [{"source": str(group / "Source [01].assfonts.ass"), "target": str(subtitle)}],
                    "remuxJobs": [{"source": str(source_video), "output": str(video), "arguments": [str(subtitle)]}],
                    "package": {"output": str(package), "entries": []},
                    "final": {"video": [{"source": str(video)}], "zip": [{"source": str(package)}]},
                },
                "stages": {},
            }
            write_json_atomic(backend_cache_path(root), manifest)
            state = valid_state(
                work_dir=str(root),
                branch="tv",
                task="complete-archive",
                requested_steps=None,
                selected_steps=["inspect", "subtitle", "remux", "package", "review", "finalize", "cleanup"],
                completed_steps=["inspect", "subtitle", "remux", "package"],
                approvals={"preflight": True, "final": True},
                decisions={},
            )
            restart = workflow.restore_backend_results(root, state)
            restored = json.loads(read_text(backend_cache_path(root)))
            persisted = load_state(root)
            self.assertEqual("subtitle", restart)
            self.assertEqual(str(subtitle), restored["plan"]["renameJobs"][0]["target"])
            self.assertEqual(str(video), restored["plan"]["remuxJobs"][0]["output"])
            self.assertEqual(str(package), restored["plan"]["package"]["output"])
            self.assertEqual({}, restored["stages"])
            self.assertEqual(["inspect"], persisted["completed_steps"])
            self.assertFalse(persisted["approvals"]["final"])

    def test_cli_failure_is_structured_json(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = workflow.main(["status", "--work-dir", "Z:\\definitely-missing"])
        self.assertEqual(0, code)
        # A command requiring state produces structured JSON instead of a traceback.
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = workflow.main(["inspect", "--work-dir", "Z:\\definitely-missing"])
        self.assertEqual(3, code)
        payload = json.loads(stderr.getvalue())
        self.assertEqual("FAILED", payload["status"])


if __name__ == "__main__":
    unittest.main()
