from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import hub_executor  # noqa: E402
from archive_rules import RULES_VERSION, backend_cache_path, state_path  # noqa: E402
from common import WorkflowIssue, load_state, save_state, write_json_atomic  # noqa: E402
from execution_protocol import PROTOCOL_VERSION, ProtocolError, protocol_descriptor, validate_request  # noqa: E402
from internal import library_target  # noqa: E402
from internal.archive_backend import _tv_replacement_target_plan  # noqa: E402
from internal.media_inspection import normalize_mediainfo  # noqa: E402
from internal.metadata_client import MetadataHttpError  # noqa: E402
from internal.remux_pipeline import execute_remux, validate_mkv_output  # noqa: E402


def snapshot(root: Path, relative: str = "测试作品") -> dict:
    return {
        "snapshot_id": "snapshot-1",
        "mode": "native",
        "branch": "tv",
        "work_root": str(root),
        "task_relative_path": relative,
        "storage_roots": {},
        "subtitle_roots": {},
    }


def request(root: Path, command: str, payload: dict | None = None, *, command_id: str = "command-1") -> dict:
    value = {
        "protocol_version": PROTOCOL_VERSION,
        "expected_rules_version": RULES_VERSION,
        "task_id": "task-1",
        "run_id": "run-1",
        "command_id": command_id,
        "command": command,
        "payload": payload or {},
    }
    if command != "capabilities":
        value["path_snapshot"] = snapshot(root)
    return value


class ExecutionProtocolTests(unittest.TestCase):
    def test_metadata_preview_is_path_bound_and_does_not_scan_or_change_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "测试作品").mkdir()
            local_app_data = root / "config-root"
            config = local_app_data / "archive-plex-anime" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"metadata":{"enabled":true,"mode":"auto"}}', encoding="utf-8")
            payload = {
                "decisions": {"metadata": {"enabled": True, "mode": "auto", "query": "测试作品"}},
                "local_seasons": [1, 2],
            }
            checked = validate_request(request(root, "metadata_preview", payload))
            preview = {
                "status": "MATCHED",
                "candidates": [{"id": 1, "title": "测试作品"}],
                "selected": {"id": 1, "title": "测试作品"},
                "episodes": [],
            }
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}), mock.patch(
                "hub_executor.inspect_metadata", return_value=preview
            ) as inspect:
                result = hub_executor._dispatch(checked)

            self.assertEqual("OK", result["status"])
            self.assertTrue(result["read_only"])
            self.assertFalse(result["media_scanned"])
            self.assertFalse(result["task_state_changed"])
            self.assertEqual([], inspect.call_args.args[3])
            self.assertEqual([1, 2], inspect.call_args.kwargs["local_season_numbers"])

    def test_mediainfo_empty_menu_track_still_reports_chapters(self):
        with_chapters = normalize_mediainfo(
            {"media": {"track": [{"@type": "General"}, {"@type": "Menu", "": None}]}}
        )
        without_chapters = normalize_mediainfo(
            {"media": {"track": [{"@type": "General"}]}}
        )

        self.assertEqual({"present": True, "count": 1}, with_chapters["chapters"])
        self.assertEqual({"present": False, "count": 0}, without_chapters["chapters"])

    def test_review_repairs_legacy_preserved_chapter_expectation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "show.S01E01.mkv"
            output.write_bytes(b"mkv")
            job = {
                "source": str(output),
                "output": str(output),
                "arguments": ["--no-subtitles", str(output), "--no-chapters", "subtitle.ass"],
                "chapters": "drop",
                "expectedChapters": False,
                "expectedTracks": [
                    {
                        "type": "video",
                        "language": "jpn",
                        "name": "Group",
                        "default": True,
                        "forced": False,
                    }
                ],
            }

            inventory, warnings = validate_mkv_output(
                output,
                "mkvmerge",
                job,
                inspector=lambda *_args: {
                    "status": "OK",
                    "tracks": [
                        {
                            "type": "video",
                            "language": "jpn",
                            "name": "Group",
                            "default": True,
                            "forced": False,
                        }
                    ],
                    "chapters": {"present": True, "count": 5},
                    "attachments": [],
                },
                allow_preserved_chapter_repair=True,
            )

        self.assertTrue(inventory["chapters"]["present"])
        self.assertTrue(job["expectedChapters"])
        self.assertEqual("preserve", job["chapters"])
        self.assertEqual(1, len(warnings))

    def test_review_does_not_repair_explicit_chapter_drop(self):
        output = Path("show.S01E01.mkv")
        job = {
            "source": str(output),
            "arguments": ["--no-chapters", str(output)],
            "expectedChapters": False,
            "expectedTracks": [{"type": "video"}],
        }
        with self.assertRaisesRegex(Exception, "chapters expected=False actual=True"):
            validate_mkv_output(
                output,
                "mkvmerge",
                job,
                inspector=lambda *_args: {
                    "status": "OK",
                    "tracks": [{"type": "video"}],
                    "chapters": {"present": True, "count": 1},
                    "attachments": [],
                },
                allow_preserved_chapter_repair=True,
            )

    def test_hub_can_confirm_one_nas_target_without_kdocs_tracker(self):
        nas = [
            {
                "library": "Anime3",
                "path": "/archive/targets/3/Anime/测试作品",
                "name": "测试作品",
                "webrip": False,
                "seasons": [],
            }
        ]

        strict = library_target.resolve_target("tv", [], nas, "Anime1")
        hub = library_target.resolve_target(
            "tv", [], nas, "Anime1", allow_nas_only=True
        )

        self.assertEqual("LIBRARY_TARGET_ORPHAN", strict["code"])
        self.assertEqual("OK", hub["status"])
        self.assertEqual("replace", hub["mode"])
        self.assertEqual("Anime3", hub["library"])

    def test_descriptor_is_versioned_and_deterministic(self):
        first = protocol_descriptor()
        second = protocol_descriptor()
        self.assertEqual(PROTOCOL_VERSION, first["protocol_version"])
        self.assertEqual(RULES_VERSION, first["rules_version"])
        self.assertEqual(first["source_version"], second["source_version"])
        self.assertEqual(64, len(first["source_version"]))
        self.assertEqual(["video", "subtitle_zip"], first["hub_final_sinks"])
        self.assertEqual(["inputs", "staged", "final"], first["artifact_groups"])
        self.assertIn("progress", first["event_required"])
        self.assertIn("next_action", first["event_required"])
        self.assertIn("mapping_preview", first["commands"])
        self.assertIn("tmdb_token", first["hub_forbidden_keys"])
        self.assertEqual(
            ["checkpoint_id", "stage", "status", "resumable", "details"],
            first["checkpoint_required"],
        )

    def test_metadata_check_distinguishes_configuration_auth_network_and_proxy(self):
        with mock.patch.object(
            hub_executor, "credential_presence", return_value={"tmdb": False, "tvdb": False}
        ):
            result = hub_executor._metadata_check({"providers": ["tmdb", "tvdb"]})
        self.assertEqual("not_configured", result["providers"]["tmdb"]["status"])
        self.assertEqual("not_configured", result["providers"]["tvdb"]["status"])

        with mock.patch.dict(os.environ, {"ARCHIVE_TMDB_TOKEN": "test-token"}, clear=False), mock.patch.object(
            hub_executor, "credential_presence", return_value={"tmdb": True}
        ), mock.patch.object(
            hub_executor.TmdbClient,
            "search",
            side_effect=MetadataHttpError("TMDB_AUTH_FAILED", "bad credential"),
        ):
            result = hub_executor._metadata_check({"providers": ["tmdb"]})
        self.assertEqual("auth_failed", result["providers"]["tmdb"]["status"])

        with mock.patch.dict(os.environ, {"ARCHIVE_TMDB_TOKEN": "test-token"}, clear=False), mock.patch.object(
            hub_executor, "credential_presence", return_value={"tmdb": True}
        ), mock.patch.object(
            hub_executor.TmdbClient,
            "search",
            side_effect=MetadataHttpError("METADATA_NETWORK_UNAVAILABLE", "offline", transient=True),
        ):
            direct = hub_executor._metadata_check({"providers": ["tmdb"]})
            proxied = hub_executor._metadata_check(
                {"providers": ["tmdb"], "proxy": "http://127.0.0.1:7890"}
            )
        self.assertEqual("network_failed", direct["providers"]["tmdb"]["status"])
        self.assertEqual("proxy_failed", proxied["providers"]["tmdb"]["status"])

    def test_inspect_projects_media_tracks_subtitles_fonts_and_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "discovery": {
                    "videos": [{"file": {"path": "S1/show.E01.mkv"}, "tracks": [{"type": "audio"}]}],
                    "subtitles": [{"file": {"path": "S1/SC/show.E01.ass"}, "group": "SC"}],
                    "fontRequirements": [{"name": "Required Font"}],
                    "fontAvailability": [{"name": "Required Font", "available": False}],
                    "missingFonts": [{"name": "Required Font", "available": False}],
                    "embeddedSubtitles": {"status": "EXTERNAL"},
                    "movieAudioPreflights": [{"status": "READY_FOR_PREFLIGHT"}],
                    "libraryTarget": {"resolution": {"library": "Anime1"}},
                    "metadata": {"status": "READY", "title": "测试作品"},
                }
            }
            write_json_atomic(backend_cache_path(root), manifest)

            projected = hub_executor._inspect_analysis(
                root,
                {"summary": {"videos": 1}, "route": {"branch": "anime"}},
            )

            self.assertEqual("S1/show.E01.mkv", projected["videos"][0]["file"]["path"])
            self.assertEqual("SC", projected["subtitles"][0]["group"])
            self.assertEqual("Required Font", projected["font_requirements"][0]["name"])
            self.assertEqual("Required Font", projected["missing_fonts"][0]["name"])
            self.assertEqual("EXTERNAL", projected["embedded_subtitles"]["status"])
            self.assertEqual("Anime1", projected["library_target"]["resolution"]["library"])
            self.assertEqual("测试作品", projected["metadata"]["title"])
            self.assertEqual("S01E01", projected["media_rows"][0]["episode_label"])
            self.assertEqual(1, len(projected["media_rows"][0]["subtitles"]))
            self.assertIn("metadata_evidence", projected)
            self.assertIn("next_action", projected)

    def test_preflight_issues_are_projected_as_decision_requests(self):
        requests = hub_executor._decision_requests(
            {},
            {
                "preflight": {
                    "issues": [{"code": "SUBTITLE_ARCHIVE_ROOT_REQUIRED"}]
                }
            },
        )

        self.assertEqual(1, len(requests))
        self.assertEqual("SUBTITLE_ARCHIVE_ROOT_REQUIRED", requests[0]["code"])
        self.assertEqual("subtitle_archive_mode", requests[0]["field"])

    def test_optional_metadata_outage_is_not_projected_as_a_user_decision(self):
        requests = hub_executor._decision_requests(
            {"metadata": {"mode": "auto"}},
            {
                "preflight": {
                    "issues": [
                        {"code": "CAPABILITY_UNAVAILABLE", "capability": "metadata"}
                    ]
                }
            },
        )

        self.assertEqual([], requests)

    def test_required_metadata_outage_explains_the_available_choices(self):
        requests = hub_executor._decision_requests(
            {"metadata": {"mode": "required"}},
            {
                "preflight": {
                    "issues": [
                        {"code": "CAPABILITY_UNAVAILABLE", "capability": "metadata"}
                    ]
                }
            },
        )

        self.assertEqual(1, len(requests))
        self.assertEqual("恢复在线剧集信息", requests[0]["label"])
        self.assertIn("自动/离线模式", requests[0]["details"]["message"])

    def test_mapping_preview_orders_video_and_subtitle_sequences_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "discovery": {
                    "videos": [
                        {"file": {"path": str(root / "video-a.mkv")}},
                        {"file": {"path": str(root / "video-b.mkv")}},
                    ],
                    "subtitles": [
                        {"file": {"path": str(root / "subtitle-a.ass")}},
                        {"file": {"path": str(root / "subtitle-b.ass")}},
                    ],
                }
            }
            write_json_atomic(backend_cache_path(root), manifest)

            preview = hub_executor._mapping_preview(
                root,
                {
                    "strategy": "order",
                    "scope": "all",
                    "parameters": {"season": 1, "start_episode": 1},
                    "decisions": {},
                },
            )

            self.assertEqual("S01E01", preview["episode_map_patch"]["video-a.mkv"])
            self.assertEqual("S01E02", preview["episode_map_patch"]["video-b.mkv"])
            self.assertEqual("S01E01", preview["episode_map_patch"]["subtitle-a.ass"])
            self.assertEqual("S01E02", preview["episode_map_patch"]["subtitle-b.ass"])

    def test_hub_request_rejects_kdocs_and_unsafe_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forbidden = request(
                root,
                "initialize",
                {"preset": "complete-archive", "capabilities": ["kdocs-tracker"]},
            )
            with self.assertRaisesRegex(ProtocolError, "KDocs"):
                validate_request(forbidden)

            nested = request(
                root,
                "approve_preflight",
                {"decisions": {"metadata": {"tracker_column": "Archive"}}},
            )
            with self.assertRaisesRegex(ProtocolError, "KDocs"):
                validate_request(nested)

            credential = request(
                root,
                "approve_preflight",
                {"decisions": {"metadata": {"tmdb_token": "must-not-cross-protocol"}}},
            )
            with self.assertRaisesRegex(ProtocolError, "credentials"):
                validate_request(credential)

            escaped = request(root, "status")
            escaped["path_snapshot"]["task_relative_path"] = "../outside"
            with self.assertRaisesRegex(ProtocolError, "task_relative_path"):
                validate_request(escaped)

    def test_path_snapshot_rejects_formal_targets_overlapping_work_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = request(root, "status")
            value["path_snapshot"]["storage_roots"] = {"storage_1": str(root / "Anime")}
            with self.assertRaisesRegex(ProtocolError, "separate from work_root"):
                validate_request(value)

    def test_approve_final_accepts_only_storage_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = request(
                root,
                "approve_final",
                {"final_target": {"storage_id": "storage_1", "operation": "replace"}},
            )
            value["path_snapshot"]["storage_roots"] = {
                "storage_1": str(root.parent / "Anime")
            }
            with self.assertRaisesRegex(ProtocolError, "unsupported fields"):
                validate_request(value)

    def test_approve_final_accepts_valid_target_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = request(
                root,
                "approve_final",
                {
                    "final_target": {
                        "storage_id": "storage_1",
                        "target_actions": {"S00E01": "option-1"},
                    }
                },
            )
            value["path_snapshot"]["storage_roots"] = {
                "storage_1": str(root.parent / "Anime")
            }
            self.assertEqual(value, validate_request(value))

    def test_tv_replacement_plan_supports_mixed_create_and_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "潘朵拉之心"
            target.mkdir()
            old = target / "潘多拉之心.S01E01.mkv"
            old.write_bytes(b"old")
            jobs = [
                {"source": "stage/S1/E01.mkv", "relativePath": "潘多拉之心/S1/潘多拉之心.S01E01.mkv"},
                {"source": "stage/S0/E01.mkv", "relativePath": "潘多拉之心/S0/潘多拉之心.S00E01.mkv"},
            ]

            planned, conflicts, summary = _tv_replacement_target_plan(jobs, target)

            self.assertEqual([], conflicts)
            self.assertEqual({"create": 1, "replace": 1, "conflict": 0}, summary)
            self.assertEqual("replace", planned[0]["operation"])
            self.assertEqual(str(old.resolve()), planned[0]["destination"])
            self.assertEqual("create", planned[1]["operation"])
            self.assertEqual(
                str((target / "S0" / "潘多拉之心.S00E01.mkv").resolve()),
                planned[1]["destination"],
            )

    def test_tv_replacement_plan_requires_choice_for_duplicate_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "作品"
            (target / "S1").mkdir(parents=True)
            (target / "作品.S01E01.mkv").write_bytes(b"old-1")
            (target / "S1" / "作品.S01E01.mkv").write_bytes(b"old-2")
            jobs = [
                {"source": "stage/E01.mkv", "relativePath": "作品/S1/作品.S01E01.mkv"}
            ]

            planned, conflicts, summary = _tv_replacement_target_plan(jobs, target)

            self.assertEqual("conflict", planned[0]["operation"])
            self.assertEqual(1, summary["conflict"])
            self.assertEqual(3, len(conflicts[0]["options"]))
            create_option = next(
                item for item in conflicts[0]["options"] if item["operation"] == "create"
            )
            resolved, remaining, resolved_summary = _tv_replacement_target_plan(
                jobs, target, {"S01E01": create_option["id"]}
            )
            self.assertEqual([], remaining)
            self.assertEqual("create", resolved[0]["operation"])
            self.assertEqual(1, resolved_summary["create"])

    def test_approve_final_derives_tv_movie_and_subtitle_targets(self):
        for branch, library in (("tv", "Anime1"), ("movie", "Movie1")):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "work"
                work = root / "测试作品"
                work.mkdir(parents=True)
                video_root = base / ("Anime" if branch == "tv" else "Movie")
                reviewed_video_root = video_root / "测试作品"
                subtitle_root = base / "subtitles"
                video_root.mkdir()
                reviewed_video_root.mkdir()
                subtitle_root.mkdir()
                save_state(
                    work,
                    {
                        "schema": 8,
                        "rules_version": RULES_VERSION,
                        "work_dir": str(work),
                        "branch": branch,
                        "task": "replacement",
                        "selected_steps": ["review"],
                        "completed_steps": ["review"],
                        "approvals": {"preflight": True, "final": False},
                        "decisions": {"title": "测试作品"},
                        "final_sinks": ["video", "subtitle_zip"],
                        "final_target": {
                            "library": library,
                            "video_root": str(reviewed_video_root.resolve()),
                            "zip": str((subtitle_root / "测试作品.zip").resolve()),
                            "operation": "replace",
                            "batch_id": "batch-1",
                            "batch_digest": "digest-1",
                        },
                    },
                )
                value = request(
                    root,
                    "approve_final",
                    {"final_target": {"storage_id": "storage_1"}},
                )
                value["path_snapshot"].update(
                    branch=branch,
                    storage_roots={"storage_1": str(video_root)},
                    subtitle_roots={branch: str(subtitle_root)},
                )

                events = hub_executor.execute(value)

                self.assertEqual("succeeded", events[-1]["status"])
                target = events[-1]["result"]["state"]["final_target"]
                self.assertEqual(library, target["library"])
                self.assertEqual(str(reviewed_video_root.resolve()), target["video_root"])
                self.assertEqual(
                    str((subtitle_root / "测试作品.zip").resolve()),
                    target["zip"],
                )
                self.assertNotIn("tracker_column", target)

    def test_approve_final_rejects_reviewed_video_target_outside_selected_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "work"
            work = root / "测试作品"
            work.mkdir(parents=True)
            storage_root = base / "Anime"
            storage_root.mkdir()
            outside_target = base / "OtherAnime" / "测试作品"
            outside_target.mkdir(parents=True)
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "replacement",
                    "selected_steps": ["review"],
                    "completed_steps": ["review"],
                    "approvals": {"preflight": True, "final": False},
                    "decisions": {"title": "测试作品"},
                    "final_sinks": ["video"],
                    "final_target": {
                        "library": "Anime1",
                        "video_root": str(outside_target.resolve()),
                        "operation": "replace",
                        "batch_id": "batch-1",
                        "batch_digest": "digest-1",
                    },
                },
            )
            value = request(
                root,
                "approve_final",
                {"final_target": {"storage_id": "storage_1"}},
            )
            value["path_snapshot"].update(
                branch="tv",
                storage_roots={"storage_1": str(storage_root)},
            )

            events = hub_executor.execute(value)

            self.assertEqual("failed", events[-1]["status"])
            self.assertEqual("PROTOCOL_FINAL_TARGET_INVALID", events[-1]["issues"][0]["code"])
            state = load_state(work)
            self.assertFalse(state["approvals"]["final"])

    def test_hub_status_omits_forbidden_internal_state_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "local-only",
                    "selected_steps": ["inspect"],
                    "completed_steps": [],
                    "approvals": {"preflight": False, "final": False},
                    "decisions": {},
                    "final_target": {"tracker_column": "", "operation": "create"},
                },
            )
            events = hub_executor.execute(request(root, "status", command_id="sanitized-status"))
            target = events[-1]["result"]["state"]["final_target"]
            self.assertNotIn("tracker_column", target)
            self.assertEqual("create", target["operation"])

    def test_status_projects_staged_final_artifacts_and_recovery_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "work"
            work = root / "测试作品"
            work.mkdir(parents=True)
            staged = work / "测试作品.mkv"
            staged.write_bytes(b"mkv")
            destination = base / "Anime" / "测试作品.mkv"
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(b"mkv")
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "replacement",
                    "selected_steps": ["inspect", "review", "finalize"],
                    "completed_steps": ["inspect", "review"],
                    "approvals": {"preflight": True, "final": True},
                    "decisions": {},
                    "final_results": {
                        "batch_id": "batch-1",
                        "video": {
                            str(destination): {
                                "status": "COMPLETE",
                                "size": 3,
                                "source_size": 3,
                            }
                        },
                    },
                },
            )
            write_json_atomic(
                backend_cache_path(work),
                {
                    "plan": {
                        "final": {
                            "video": [
                                {
                                    "source": str(staged),
                                    "destination": str(destination),
                                }
                            ]
                        }
                    }
                },
            )
            events = hub_executor.execute(request(root, "status", command_id="artifact-projection"))
            artifacts = events[-1]["artifacts"]
            self.assertEqual("ready", artifacts["staged"][0]["state"])
            self.assertEqual("verified", artifacts["final"][0]["state"])
            statuses = {item["status"] for item in events[-1]["checkpoints"]}
            self.assertEqual({"completed"}, statuses)

    def test_initialize_emits_ordered_ndjson_contract_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            value = request(
                root,
                "initialize",
                {
                    "preset": "complete-archive",
                    "capabilities": ["video-delivery", "subtitle-delivery"],
                    "final_sinks": ["video", "subtitle_zip"],
                    "decisions": {"title": "测试作品"},
                },
            )
            with mock.patch.object(hub_executor.workflow, "load_config", return_value={"paths": {}}):
                events = hub_executor.execute(value)
            self.assertEqual([1, 2, 3], [item["sequence"] for item in events])
            self.assertEqual(["accepted", "running", "succeeded"], [item["status"] for item in events])
            self.assertEqual({"inputs", "staged", "final"}, set(events[-1]["artifacts"]))
            self.assertEqual("task_directory", events[-1]["artifacts"]["inputs"][0]["kind"])
            self.assertIsInstance(events[-1]["checkpoints"], list)
            state = events[-1]["result"]["state"]
            self.assertEqual("hub", state["entrypoint"])
            self.assertEqual(["video", "subtitle_zip"], state["requested_final_sinks"])
            self.assertNotIn("tracker", state["requested_final_sinks"])

    def test_executor_reports_running_before_dispatch_completes(self):
        value = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_rules_version": RULES_VERSION,
            "task_id": "task-stream",
            "run_id": "run-stream",
            "command_id": "command-stream",
            "command": "capabilities",
            "payload": {"branch": "tv"},
        }
        observed: list[str] = []

        def dispatch(_request):
            self.assertEqual(["accepted", "running"], observed)
            return {"status": "OK", "entrypoint": "hub"}

        with mock.patch.object(hub_executor, "_dispatch", side_effect=dispatch):
            events = hub_executor.execute(
                value,
                on_event=lambda item: observed.append(item["status"]),
            )
        self.assertEqual(["accepted", "running", "succeeded"], observed)
        self.assertEqual(observed, [item["status"] for item in events])

    def test_executor_streams_ordered_file_progress_events(self):
        value = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_rules_version": RULES_VERSION,
            "task_id": "task-progress",
            "run_id": "run-progress",
            "command_id": "command-progress",
            "command": "capabilities",
            "payload": {"branch": "tv"},
        }

        def dispatch(_request):
            callback = hub_executor._ACTIVE_PROGRESS.get()
            self.assertTrue(callable(callback))
            callback({"stage": "remux", "completed_items": 0, "total_items": 2, "current_item": "S01E01.mkv"})
            callback(
                {
                    "stage": "remux",
                    "completed_items": 1,
                    "total_items": 2,
                    "current_item": "S01E01.mkv",
                    "reused_items": 1,
                    "remaining_items": 1,
                    "remaining_bytes": 1024,
                    "available_bytes": 4096,
                    "action": "reused",
                }
            )
            return {"status": "OK", "entrypoint": "hub"}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"ARCHIVE_PROTOCOL_CACHE_DIR": directory},
        ), mock.patch.object(hub_executor, "_dispatch", side_effect=dispatch):
            events = hub_executor.execute(value)

        self.assertEqual([1, 2, 3, 4, 5], [item["sequence"] for item in events])
        self.assertEqual(["accepted", "running", "running", "running", "succeeded"], [item["status"] for item in events])
        self.assertEqual(1, events[-2]["progress"]["completed_items"])
        self.assertEqual("S01E01.mkv", events[-2]["progress"]["current_item"])
        self.assertEqual(1, events[-2]["progress"]["reused_items"])
        self.assertEqual("reused", events[-2]["progress"]["action"])

    def test_executor_preserves_retryable_archive_error_code_and_details(self):
        value = request(Path("."), "capabilities", {"branch": "tv"}, command_id="space-error")
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"ARCHIVE_PROTOCOL_CACHE_DIR": directory},
        ), mock.patch.object(
            hub_executor,
            "_dispatch",
            side_effect=WorkflowIssue(
                "FAILED",
                "not enough space",
                code="ARCHIVE_INSUFFICIENT_SPACE",
                details={"required_bytes": 200, "available_bytes": 100},
                retryable=True,
            ),
        ):
            events = hub_executor.execute(value)

        issue = events[-1]["issues"][0]
        self.assertEqual("ARCHIVE_INSUFFICIENT_SPACE", issue["code"])
        self.assertTrue(issue["retryable"])
        self.assertEqual(200, issue["details"]["required_bytes"])

    def test_remux_pipeline_reports_each_file_start_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress: list[dict] = []
            manifest = {
                "workPath": str(root),
                "plan": {
                    "remuxJobs": [
                        {"output": str(root / "S1" / "show.S01E01.mkv"), "arguments": ["--no-date"]},
                        {"output": str(root / "S1" / "show.S01E02.mkv"), "arguments": ["--no-date"]},
                    ]
                },
                "discovery": {"videos": []},
            }

            def runner(command):
                Path(command[2]).write_bytes(b"mkv")
                return {"exitCode": 0, "stdout": "", "stderr": ""}

            result = execute_remux(
                manifest,
                "mkvmerge",
                runner=runner,
                track_mapper=lambda *_args: {},
                validator=lambda *_args: ({}, []),
                direct_output=True,
                on_progress=lambda item: progress.append(dict(item)),
            )

        self.assertEqual("COMPLETE", result["status"])
        self.assertEqual([0, 1, 1, 2], [item["completed_items"] for item in progress])
        self.assertEqual([2, 2, 2, 2], [item["total_items"] for item in progress])
        self.assertTrue(progress[0]["current_item"].endswith("show.S01E01.mkv"))
        self.assertTrue(progress[-1]["current_item"].endswith("show.S01E02.mkv"))

    def test_remux_failure_keeps_validated_files_and_retry_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "staging" / "show-task"
            output_root.mkdir(parents=True)
            sources = [root / "source-1.mkv", root / "source-2.mkv"]
            for source in sources:
                source.write_bytes(b"source")
            manifest = {
                "workPath": str(root),
                "plan": {
                    "remuxJobs": [
                        {
                            "source": str(sources[0]),
                            "output": str(output_root / "S1" / "show.S01E01.mkv"),
                            "arguments": [str(sources[0])],
                        },
                        {
                            "source": str(sources[1]),
                            "output": str(output_root / "S1" / "show.S01E02.mkv"),
                            "arguments": [str(sources[1])],
                        },
                    ]
                },
                "discovery": {"videos": [{"file": {"path": str(source)}} for source in sources]},
            }
            first_calls: list[str] = []

            def failing_runner(command):
                first_calls.append(command[2])
                if len(first_calls) == 2:
                    return {"exitCode": 2, "stdout": "", "stderr": "mux failed"}
                Path(command[2]).write_bytes(b"mkv-1")
                return {"exitCode": 0, "stdout": "", "stderr": ""}

            with mock.patch.dict(
                os.environ, {"ARCHIVE_TASK_OUTPUT_ROOT": str(output_root)}
            ), self.assertRaisesRegex(Exception, "mux failed"):
                execute_remux(
                    manifest,
                    "mkvmerge",
                    runner=failing_runner,
                    track_mapper=lambda *_args: {},
                    validator=lambda *_args: ({"status": "OK"}, []),
                    direct_output=True,
                )

            first_output = output_root / "S1" / "show.S01E01.mkv"
            second_output = output_root / "S1" / "show.S01E02.mkv"
            self.assertTrue(first_output.is_file())
            self.assertFalse(second_output.exists())
            self.assertTrue((output_root / "remux" / "resume.json").is_file())

            retry_calls: list[str] = []
            progress: list[dict] = []

            def retry_runner(command):
                retry_calls.append(command[2])
                Path(command[2]).write_bytes(b"mkv-2")
                return {"exitCode": 0, "stdout": "", "stderr": ""}

            with mock.patch.dict(
                os.environ, {"ARCHIVE_TASK_OUTPUT_ROOT": str(output_root)}
            ):
                result = execute_remux(
                    manifest,
                    "mkvmerge",
                    runner=retry_runner,
                    track_mapper=lambda *_args: {},
                    validator=lambda *_args: ({"status": "OK"}, []),
                    direct_output=True,
                    on_progress=lambda item: progress.append(dict(item)),
                )
            second_exists = second_output.is_file()

        self.assertEqual("COMPLETE", result["status"])
        self.assertEqual(1, result["stage"]["reused"])
        self.assertEqual(1, len(retry_calls))
        self.assertTrue(retry_calls[0].endswith("show.S01E02.mkv.tmp"))
        self.assertTrue(second_exists)
        self.assertTrue(any(item.get("action") == "reused" for item in progress))

    def test_remux_space_check_fails_before_starting_the_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            manifest = {
                "workPath": str(root),
                "plan": {
                    "remuxJobs": [
                        {
                            "source": str(source),
                            "output": str(root / "show.S01E01.mkv"),
                            "arguments": [str(source)],
                        }
                    ]
                },
                "discovery": {"videos": [{"file": {"path": str(source)}}]},
            }
            runner = mock.Mock()
            with mock.patch(
                "internal.remux_pipeline.shutil.disk_usage",
                return_value=mock.Mock(free=0),
            ):
                with self.assertRaises(Exception) as raised:
                    execute_remux(
                        manifest,
                        "mkvmerge",
                        runner=runner,
                        track_mapper=lambda *_args: {},
                        validator=lambda *_args: ({"status": "OK"}, []),
                        direct_output=True,
                    )

        self.assertEqual("ARCHIVE_INSUFFICIENT_SPACE", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        runner.assert_not_called()

    def test_same_command_replays_and_changed_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "local-only",
                    "selected_steps": ["inspect"],
                    "completed_steps": [],
                    "approvals": {"preflight": False, "final": False},
                    "decisions": {},
                },
            )
            value = request(root, "status")
            first = hub_executor.execute(value)
            second = hub_executor.execute(value)
            self.assertEqual(first, second)

            changed = {**value, "run_id": "run-2"}
            with self.assertRaisesRegex(ProtocolError, "command_id"):
                hub_executor.execute(changed)

    def test_capability_command_replays_original_events(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"ARCHIVE_PROTOCOL_CACHE_DIR": directory},
        ):
            value = request(Path(directory), "capabilities", {"branch": "tv"}, command_id="capability-replay")
            first = hub_executor.execute(value)
            time.sleep(0.01)
            second = hub_executor.execute(value)
            self.assertEqual(first, second)

    def test_concurrent_duplicate_command_dispatches_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "local-only",
                    "selected_steps": ["inspect"],
                    "completed_steps": [],
                    "approvals": {"preflight": False, "final": False},
                    "decisions": {},
                },
            )
            value = request(root, "status", command_id="concurrent-command")
            original = hub_executor._dispatch
            calls = 0
            calls_lock = threading.Lock()
            outputs: list[list[dict]] = []
            failures: list[BaseException] = []

            def slow_dispatch(item):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.1)
                return original(item)

            def run():
                try:
                    outputs.append(hub_executor.execute(value))
                except BaseException as exc:  # pragma: no cover - retained for useful thread diagnostics
                    failures.append(exc)

            with mock.patch.object(hub_executor, "_dispatch", side_effect=slow_dispatch):
                threads = [threading.Thread(target=run) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual([], failures)
            self.assertEqual(1, calls)
            self.assertEqual(2, len(outputs))
            self.assertEqual(outputs[0], outputs[1])

    def test_interrupted_command_is_not_executed_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "local-only",
                    "selected_steps": ["inspect"],
                    "completed_steps": [],
                    "approvals": {"preflight": False, "final": False},
                    "decisions": {},
                },
            )
            value = request(root, "status", command_id="interrupted-command")
            with mock.patch.object(hub_executor, "_dispatch", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    hub_executor.execute(value)
            with mock.patch.object(hub_executor, "_dispatch") as dispatch:
                events = hub_executor.execute(value)
            dispatch.assert_not_called()
            self.assertEqual("failed", events[-1]["status"])
            self.assertEqual("PROTOCOL_COMMAND_INTERRUPTED", events[-1]["issues"][0]["code"])

    def test_windows_incompatible_command_id_uses_safe_cache_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            save_state(
                work,
                {
                    "schema": 8,
                    "rules_version": RULES_VERSION,
                    "work_dir": str(work),
                    "branch": "tv",
                    "task": "local-only",
                    "selected_steps": ["inspect"],
                    "completed_steps": [],
                    "approvals": {"preflight": False, "final": False},
                    "decisions": {},
                },
            )
            value = request(root, "status", command_id="command:CON")
            hub_executor.execute(value)
            cache = hub_executor._command_cache(work, value["command_id"])
            self.assertTrue(cache.is_file())
            self.assertRegex(cache.name, r"^[0-9a-f]{64}\.json$")
            self.assertNotIn(":", cache.name)

    def test_final_sink_mismatch_stops_before_state_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "测试作品"
            work.mkdir()
            value = request(
                root,
                "initialize",
                {
                    "preset": "complete-archive",
                    "capabilities": ["video-delivery"],
                    "final_sinks": ["subtitle_zip"],
                    "decisions": {"title": "测试作品"},
                },
            )
            events = hub_executor.execute(value)
            self.assertEqual(["accepted", "running", "failed"], [item["status"] for item in events])
            self.assertEqual("PROTOCOL_FINAL_SINK_MISMATCH", events[-1]["issues"][0]["code"])
            self.assertFalse(state_path(work).is_file())

    def test_public_executor_uses_stdout_only_for_ndjson(self):
        value = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_rules_version": RULES_VERSION,
            "task_id": "task-1",
            "run_id": "run-1",
            "command_id": "command-1",
            "command": "capabilities",
            "payload": {"branch": "tv"},
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONUTF8="1",
                ARCHIVE_PROTOCOL_CACHE_DIR=directory,
            )
            completed = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS / "hub_executor.py"), "execute"],
                input=json.dumps(value, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                check=False,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8"))
        events = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
        self.assertEqual([1, 2, 3], [item["sequence"] for item in events])
        self.assertEqual("succeeded", events[-1]["status"])
        self.assertEqual(b"", completed.stderr)


if __name__ == "__main__":
    unittest.main()
