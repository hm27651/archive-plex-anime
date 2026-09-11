from __future__ import annotations

import json
import argparse
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
from internal.source_grouping import group_source_scope, season_name  # noqa: E402
from internal.archive_backend import _tv_replacement_target_plan  # noqa: E402
from internal.media_inspection import normalize_mediainfo  # noqa: E402
from internal.metadata_client import MetadataHttpError  # noqa: E402
from internal.remux_pipeline import execute_remux, validate_mkv_output  # noqa: E402
from internal import archive_backend  # noqa: E402
from internal.delivery_targets import bind_video_targets  # noqa: E402
from internal.errors import WorkflowError  # noqa: E402


class RemainingCleanupAndTransferTests(unittest.TestCase):
    def test_removed_and_changed_files_are_not_implicitly_deleted(self):
        from internal.hub_task_contract import cleanup_preview, execute_cleanup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, library = root/'source', root/'library'
            source.mkdir(); library.mkdir()
            for name in ['video.mkv', 'subtitle.ass', 'font.ttf']:
                (source/name).write_bytes(b'original')
            baseline = {'source_directory':[{'path':p.name,'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in source.iterdir()]}
            (source/'subtitle.ass').rename(root/'subtitle.ass')
            (source/'font.ttf').write_bytes(b'modified-font')
            (source/'unrelated.txt').write_text('keep')
            preview = cleanup_preview(staging_directory=None,source_directory=source,formal_directories=[library],exclusive_source_directory=True,shared_parent_directory=False,delivery_confirmed=True,baseline=baseline)
            rows = {r['path']:r for r in preview['actions'][0]['files']}
            self.assertNotIn('subtitle.ass', rows)
            self.assertTrue(rows['video.mkv']['recommended'])
            self.assertFalse(rows['font.ttf']['recommended'])
            self.assertFalse(rows['unrelated.txt']['recommended'])
            # Even a new file appearing after preview cannot be swept into deletion.
            (source/'added-after.txt').write_text('keep')
            execute_cleanup(preview,['source_directory'],{'source_directory':['video.mkv']})
            self.assertFalse((source/'video.mkv').exists())
            self.assertTrue((source/'unrelated.txt').exists())
            self.assertTrue((source/'added-after.txt').exists())
            self.assertEqual(b'original',(root/'subtitle.ass').read_bytes())
            self.assertEqual(b'modified-font',(source/'font.ttf').read_bytes())

    def test_changed_selected_file_and_shared_source_block_cleanup(self):
        from internal.hub_task_contract import cleanup_preview, execute_cleanup
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.mkdir();video=source/'video.mkv';video.write_bytes(b'old')
            baseline={'source_directory':[{'path':video.name,'size':3,'mtime_ns':video.stat().st_mtime_ns}]}
            args=dict(staging_directory=None,source_directory=source,formal_directories=[Path(directory)/'library'],exclusive_source_directory=True,shared_parent_directory=False,delivery_confirmed=True,baseline=baseline)
            preview=cleanup_preview(**args);video.write_bytes(b'changed')
            with self.assertRaises(WorkflowError):execute_cleanup(preview,['source_directory'],{'source_directory':['video.mkv']})
            self.assertEqual(b'changed',video.read_bytes())
            args['shared_parent_directory']=True
            with self.assertRaises(WorkflowError):execute_cleanup(cleanup_preview(**args),['source_directory'],{'source_directory':['video.mkv']})

    def test_transfer_emits_bytes_then_checkpoint_completion_and_reuse(self):
        from internal import final_delivery as delivery
        from internal.signatures import file_signature
        from contextlib import redirect_stderr
        import io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.mkv';source.write_bytes(b'x'*1024)
            target=root/'library'/'target.mkv'
            final={'video':[{'source':str(source),'destination':str(target),'operation':'create','sourceSignature':file_signature(source)}],'zip':[]}
            stream=io.StringIO()
            with redirect_stderr(stream), mock.patch.object(delivery,'_final_checkpoint_matches',return_value=False), mock.patch.object(delivery,'_save_final_checkpoint') as checkpoint, mock.patch.object(delivery,'_final_attempt_matches',return_value=False):
                result=delivery.execute_final_delivery(root,final,'batch')
                self.assertEqual('COMPLETE',result['status']);checkpoint.assert_called_once()
            events=[json.loads(line.split(' ',1)[1]) for line in stream.getvalue().splitlines() if line.startswith('ARCHIVE_TRANSFER ')]
            self.assertEqual('copying',events[0]['action'])
            self.assertIn('verifying',[event['action'] for event in events])
            self.assertEqual('completed',events[-1]['action']);self.assertEqual(1024,events[-1]['copied_bytes'])
            self.assertEqual(source.read_bytes(),target.read_bytes())
            stream=io.StringIO()
            with redirect_stderr(stream),mock.patch.object(delivery,'_final_checkpoint_matches',return_value=True),mock.patch.object(delivery,'copy_and_verify',side_effect=AssertionError('must skip')):
                self.assertEqual('COMPLETE',delivery.execute_final_delivery(root,final,'batch')['status'])
            self.assertIn('reused',stream.getvalue())

    def test_transfer_stderr_is_forwarded_without_corrupting_result_json(self):
        from common import run_process
        events=[]
        class Progress:
            def __call__(self):pass
            def transfer(self,value):events.append(value)
        result=run_process([sys.executable,'-B','-c',"import sys;print('ARCHIVE_TRANSFER {\"copied_bytes\": 123}',file=sys.stderr);print('{\"status\":\"COMPLETE\"}')"],progress=Progress())
        self.assertEqual([{'copied_bytes':123}],events)
        self.assertEqual('COMPLETE',json.loads(result['stdout'])['status'])
        self.assertEqual('',result['stderr'])


class FinalizeOptionalZipTests(unittest.TestCase):
    def test_finalize_optional_zip_keeps_signatures_and_confirmation_checks(self):
        from copy import deepcopy
        from internal.signatures import seal_final_batch

        for branch in ('anime', 'movie'):
            for zip_case in ('missing', 'null', 'present'):
                with self.subTest(branch=branch, zip=zip_case), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    video_signature = {'path': str(root / 'video.mkv'), 'size': 3, 'mtimeUtcNs': 1}
                    zip_signature = {'path': str(root / 'subtitles.zip'), 'size': 2, 'mtimeUtcNs': 1}
                    review = {'videos': [{'file': video_signature}]}
                    if zip_case != 'missing':
                        review['zip'] = {'file': zip_signature} if zip_case == 'present' else None
                    final = seal_final_batch({'mode': 'create', 'video': [{'source': video_signature['path']}], 'zip': []})
                    manifest = {'workPath': str(root), 'route': {'branch': branch}, 'localVerification': review,
                                'stages': {'verify-local': {'status': 'COMPLETE'}}, 'finalPreparation': {'final': final}}
                    args = argparse.Namespace(manifest=str(root / 'manifest.json'), approved_batch=final['batchId'], approved_digest=final['batchDigest'])
                    with mock.patch.object(archive_backend, 'load_manifest', side_effect=lambda _: deepcopy(manifest)), mock.patch.object(archive_backend, 'require_execution') as approval, mock.patch.object(archive_backend, 'require_result_signatures') as signatures, mock.patch.object(archive_backend, 'execute_final_delivery', return_value={'status': 'COMPLETE', 'completed': [], 'warnings': []}) as delivery, mock.patch.object(archive_backend, 'save_manifest'):
                        self.assertEqual('COMPLETE', archive_backend.command_finalize(args)['status'])
                        approval.assert_called_once()
                        self.assertEqual([video_signature] + ([zip_signature] if zip_case == 'present' else []), signatures.call_args.args[3])
                        delivery.assert_called_once()
                        delivery.reset_mock()
                        args.approved_digest = 'incorrect'
                        with self.assertRaises(WorkflowError) as error:
                            archive_backend.command_finalize(args)
                        self.assertEqual('FINAL_BATCH_MISMATCH', error.exception.code)
                        delivery.assert_not_called()
                        args.approved_digest = final['batchDigest']
                        signatures.side_effect = WorkflowError('FINAL_SOURCE_CHANGED', 'source changed')
                        with self.assertRaises(WorkflowError):
                            archive_backend.command_finalize(args)
                        delivery.assert_not_called()


class DeliveryModeTests(unittest.TestCase):
    def test_unified_preflight_binds_create_and_reports_collisions_for_both_branches(self):
        import media_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for branch, suffix in [('tv', 'S1/Komi.S01E01.mkv'), ('movie', 'Komi.mkv')]:
                work = root / branch
                target = root / 'library' / branch / '自选文件夹'
                manifest = {'route':{'status':'OK','branch':'anime' if branch=='tv' else 'movie'},
                            'discovery':{'libraryTarget':{'resolution':{'status':'OK','mode':'create','library':'Anime3' if branch=='tv' else 'Movie3','nas':{'path':str(target)}}}}}
                tracks = [{'type':'video','name':'vcb-studio','language':'jpn','default':True,'forced':False}]
                def generated(*_args, **_kwargs):
                    return {'issues':[], 'plan':{'title':'Komi','remuxJobs':[{'output':str(work/suffix),'arguments':['input.mkv'],'expectedTracks':tracks}],
                                                'final':{'video':[{'source':str(work/suffix),'relativePath':f'Komi/{suffix}','expectedTracks':tracks}]}}}
                state = {'branch':branch,'task':'complete-archive','entrypoint':'hub','selection_mode':'custom','requested_capabilities':['inspect','remux','video-delivery'],
                         'decisions':{'delivery_mode':'create','batch_workbench':True,'movie_plan':{'schema_version':2},'staging':{'relative_path':'Komi'}}}
                with mock.patch('internal.movie_workbench.build_movie_plan', side_effect=generated):
                    result = media_plan.build_plan(work, manifest, state)
                    self.assertEqual([], result['issues'])
                    self.assertEqual('create', result['plan']['final']['deliveryMode'])
                    self.assertEqual(str(target/suffix), result['plan']['final']['video'][0]['destination'])
                    (target/suffix).parent.mkdir(parents=True, exist_ok=True)
                    (target/suffix).write_bytes(b'keep')
                    result = media_plan.build_plan(work, manifest, state)
                    self.assertIn('LIBRARY_CREATE_TARGET_EXISTS', [issue['code'] for issue in result['issues']])
                    self.assertEqual(b'keep', (target/suffix).read_bytes())

    def config(self, root, branch, mode):
        library = 'Anime3' if branch == 'tv' else 'Movie3'
        return {'tracker':{'enabled':False}, 'storageTargets':{'disk':{'localPath':str(root)}},
                'plexLibraries':{library:{'storageTarget':'disk', 'relativePath':''}},
                'hubTask':{'deliveryMode':mode, 'moviePlan':{'schema_version':2},
                           'libraryTarget':{'library':library,'relative_path':'自选作品文件夹'}}}

    def test_tv_and_movie_missing_directory_is_only_valid_for_create(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for branch in ['tv', 'movie']:
                config = self.config(root, branch, 'create')
                library = config['hubTask']['libraryTarget']['library']
                result = archive_backend.inspect_library_existing(config, 'Komi', branch, library)
                self.assertEqual('create', result['resolution']['mode'])
                self.assertFalse((root / '自选作品文件夹').exists())
                config['hubTask']['deliveryMode'] = 'replace'
                result = archive_backend.inspect_library_existing(config, 'Komi', branch, library)
                self.assertEqual('MANUAL_REPLACEMENT_TARGET_MISSING', result['resolution']['code'])
                config['hubTask']['libraryTarget']['relative_path'] = '../outside'
                self.assertEqual('MANUAL_REPLACEMENT_TARGET_INVALID', archive_backend.inspect_library_existing(config, 'Komi', branch, library)['resolution']['code'])

    def test_new_entry_honors_custom_folder_and_never_replaces(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '自选作品文件夹'
            for suffix in ['S1/Komi.S01E01.mkv', 'Komi.cd1.mkv']:
                job = {'source':'stage.mkv','relativePath':f'Komi/{suffix}', 'operation':'replace','destination':'untrusted'}
                planned = bind_video_targets([job], target, 'create')
                self.assertEqual('create', planned[0]['operation'])
                destination = target / suffix
                self.assertEqual(str(destination.resolve()), planned[0]['destination'])
                self.assertFalse(target.exists() and destination.exists())
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b'original')
                with self.assertRaisesRegex(WorkflowError, '新入库文件已存在'):
                    bind_video_targets([job], target, 'create')
                self.assertEqual(b'original', destination.read_bytes())
            with self.assertRaisesRegex(WorkflowError, '无效'):
                bind_video_targets([{'relativePath':'Komi/../escape.mkv'}], target, 'create')

    def test_movie_replacement_keeps_confirmed_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            old = target / 'Komi.cd1.mkv'
            old.write_bytes(b'old')
            jobs = [{'relativePath':f'Komi/Komi.cd{n}.mkv'} for n in [1,2]]
            planned = bind_video_targets(jobs, target, 'replace')
            self.assertEqual(['replace','create'], [job['operation'] for job in planned])
            old.unlink()
            with self.assertRaisesRegex(WorkflowError, '待替换视频不存在'):
                bind_video_targets(planned, target, 'replace', frozen=True)

    def test_prepare_final_create_does_not_use_tv_replacement_or_target_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'source'
            work.mkdir()
            library_root = root / 'library'
            library_root.mkdir()
            for branch, suffix in [('tv','S1/Komi.S01E01.mkv'), ('movie','Komi.mkv')]:
                config = self.config(library_root, branch, 'create')
                library = config['hubTask']['libraryTarget']['library']
                source = work / 'new.mkv'
                target = library_root / '自选作品文件夹'
                manifest = {'workPath':str(work), 'configPath':'unused', 'route':{'branch':'anime' if branch=='tv' else 'movie'},
                            'stages':{'verify-local':{'status':'COMPLETE'}}, 'localVerification':{'videos':[{'file':{'path':str(source), 'size':3,'mtimeUtcNs':1}}]},
                            'plan':{'title':'Komi', 'libraryTarget':{'status':'OK','mode':'create','library':library,'nas':{'path':str(target)}},
                                    'final':{'mode':'create','deliveryMode':'create','video':[{'source':str(source), 'relativePath':f'Komi/{suffix}', 'operation':'replace'}]}}}
                with mock.patch.object(archive_backend, 'load_manifest', return_value=manifest), mock.patch.object(archive_backend, 'require_backend_config', return_value=config), mock.patch.object(archive_backend, 'save_manifest'), mock.patch.object(archive_backend, 'load_task_state', return_value={'final_target_actions':{'S01E01':'replace-choice'}}), mock.patch.object(archive_backend, '_tv_replacement_target_plan', side_effect=AssertionError('must not replace')):
                    result = archive_backend.command_prepare_final(argparse.Namespace(manifest=str(root/'manifest.json')))
                    self.assertEqual('create', result['final']['video'][0]['operation'])
                    self.assertEqual(str(target / suffix), result['final']['video'][0]['destination'])
                    self.assertFalse(target.exists() and (target / suffix).exists())
                    (target / suffix).parent.mkdir(parents=True, exist_ok=True)
                    (target / suffix).write_bytes(b'old')
                    with self.assertRaisesRegex(WorkflowError, '新入库文件已存在'):
                        archive_backend.command_prepare_final(argparse.Namespace(manifest=str(root/'manifest.json')))


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
    def test_recommend_accepts_local_facts_and_groups_without_metadata_lookup(self):
        value = request(Path("."), "recommend", {"branch": "tv", "source_scope": {
            "source_relative_path": "Komi-san", "files": [{"path": "S1/01.mkv"}, {"path": "S2/13.mkv"}],
        }})
        value.pop("path_snapshot")
        with mock.patch("hub_executor.inspect_metadata", side_effect=AssertionError("no remote lookup")):
            result = hub_executor._dispatch(validate_request(value))
        self.assertEqual("single", result["source_grouping"]["mode"])
        self.assertEqual([1, 2], result["source_grouping"]["seasons"])

    def test_inspect_sources_finishes_with_succeeded_protocol_event(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"ARCHIVE_PROTOCOL_CACHE_DIR": directory},
        ), mock.patch.object(hub_executor, "load_config", return_value={"paths": {}, "tools": {}}):
            root = Path(directory)
            (root / "测试作品").mkdir()
            events = hub_executor.execute(request(root, "inspect_sources", command_id="inspect-sources"))

        self.assertEqual(["accepted", "running", "succeeded"], [item["status"] for item in events])
        self.assertEqual("OK", events[-1]["result"]["status"])
        self.assertEqual([], events[-1]["result"]["files"])

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


def grouping(*paths, branch="tv", parent="[VCB-Studio] Komi-san"):
    return group_source_scope({"source_relative_path": parent, "files": [{"path": path} for path in paths]}, branch)


class SourceGroupingTests(unittest.TestCase):
    def test_komi_seasons_are_one_work_without_episode_renumbering(self):
        result = grouping("S1/Komi [01].mkv", "S2/Komi [13].mkv", "S1/SPs/NCOP.mkv")
        self.assertEqual("single", result["source_grouping"]["mode"])
        self.assertEqual([], result["split_suggestions"])
        self.assertEqual([1, 2, 0], [row["season"] for row in result["source_scope"]["files"]])
        self.assertEqual("S2/Komi [13].mkv", result["source_scope"]["files"][1]["path"])

    def test_supported_season_names(self):
        for name, number in [("S01", 1), ("Season01", 1), ("Season 2", 2), ("第1季", 1), ("第一季", 1), ("第十二季", 12)]:
            with self.subTest(name=name):
                self.assertEqual(("", number), season_name(name))
        self.assertEqual(("作品 2", None), season_name("作品 2"))
        self.assertEqual(("S123", None), season_name("S123"))

    def test_release_tags_do_not_split_the_same_title(self):
        result = grouping("[Nekomoe kissaten&VCB-Studio] Komi-san S1 [1080p]/01.mkv", "[VCB-Studio] Komi-san S2 [1080p]/13.mkv")
        self.assertEqual("single", result["source_grouping"]["mode"])
        self.assertEqual("Komi-san", result["source_grouping"]["groups"][0]["title"])

    def test_different_titles_are_suggestions_not_identity_claims(self):
        result = grouping("作品甲/01.mkv", "作品乙/01.mkv", parent="合集")
        self.assertEqual("split", result["source_grouping"]["mode"])
        self.assertEqual(["作品甲", "作品乙"], [g["title"] for g in result["split_suggestions"]])
        self.assertEqual(["作品甲/01.mkv", "作品乙/01.mkv"], [g["files"][0]["path"] for g in result["split_suggestions"]])

    def test_extras_do_not_become_a_work(self):
        result = grouping("S1/01.mkv", "S2/13.mkv", "SPs/SP.mkv", "Scans/bonus.mp4")
        self.assertEqual("single", result["source_grouping"]["mode"])
        self.assertEqual(4, len(result["source_grouping"]["groups"][0]["files"]))
        multi = grouping("作品甲/01.mkv", "作品乙/01.mkv", "SPs/bonus.mkv")
        self.assertEqual(2, len(multi["split_suggestions"]))
        self.assertEqual(1, multi["source_grouping"]["unassigned_count"])

    def test_different_works_with_the_same_season_are_not_duplicate_versions(self):
        result = grouping("作品甲 S1/01.mkv", "作品乙 S1/01.mkv")
        self.assertEqual("split", result["source_grouping"]["mode"])
        self.assertEqual([], result["source_grouping"]["duplicate_seasons"])

    def test_unbracketed_resolution_versions_require_confirmation(self):
        result = grouping("S1 1080p/01.mkv", "S1 2160p/01.mkv")
        self.assertEqual("confirm", result["source_grouping"]["mode"])
        self.assertEqual([1], result["source_grouping"]["duplicate_seasons"])

    def test_tv_and_movie_must_not_be_merged(self):
        result = grouping("S1/01.mkv", "S2/13.mkv", "Movie/movie.mkv")
        self.assertEqual("confirm", result["source_grouping"]["mode"])
        self.assertFalse(result["source_grouping"]["merge_allowed"])
        self.assertEqual(["tv", "movie"], [g["branch"] for g in result["source_grouping"]["groups"]])
        self.assertEqual(2, len(result["source_grouping"]["groups"][0]["files"]))

    def test_unclear_names_and_duplicate_versions_need_confirmation(self):
        for paths in [("第一部分/01.mkv", "第二部分/02.mkv"), ("S1 [1080p]/01.mkv", "S1 [2160p]/01.mkv"), ("01.mkv", "作品乙/01.mkv")]:
            with self.subTest(paths=paths):
                self.assertEqual("confirm", grouping(*paths)["source_grouping"]["mode"])

    def test_movie_cd_parts_stay_together(self):
        self.assertEqual("single", grouping("cd1/movie.mkv", "cd2/movie.mkv", branch="movie")["source_grouping"]["mode"])

    def test_input_is_not_mutated_and_empty_scope_is_supported(self):
        scope = {"source_relative_path": "Komi", "files": [{"path": "S2/13.mkv"}]}
        group_source_scope(scope, "tv")
        self.assertNotIn("season", scope["files"][0])
        self.assertEqual([], grouping()["source_scope"]["files"])


class BatchWorkbenchTests(unittest.TestCase):
    def test_explicit_tvdb_dvd_does_not_require_tmdb(self):
        from internal import metadata_match
        client = mock.Mock()
        client.series.return_value = {'name':'Komi-san'}
        client.episodes.return_value = [{'seasonNumber':1,'number':1,'name':'Episode 1'}]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(metadata_match, 'TmdbClient', side_effect=AssertionError('TMDB must not be contacted')), mock.patch.object(metadata_match, 'TvdbClient', return_value=client):
            result = metadata_match.inspect_metadata(Path(directory), {'metadata':{'enabled':True}}, {'branch':'anime'}, [], {'provider':'tvdb','tvdb_id':20,'mode':'required','episode_order':'dvd','query':'Komi-san'})
        client.episodes.assert_called_once_with(20, 'dvd')
        self.assertEqual('MATCHED', result['status'])
        self.assertEqual('tvdb-dvd', result['episodeOrder'])
        self.assertEqual('tvdb', result['selected']['provider'])

    def test_tv_explicit_stream_plan_uses_overrides_and_tv_output_contract(self):
        from internal.movie_workbench import build_movie_plan
        from internal.batch_workbench import describe_batch
        from archive_rules import validate_plan
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / 'Komi [01].mkv'
            source.write_bytes(b'source')
            config = work / 'config.json'
            config.write_text('{"paths":{},"tools":{}}', encoding='utf-8')
            inventory = {'files':[{'id':source.name,'path':source.name,'kind':'video','signature':{'size':6,'mtime_ns':source.stat().st_mtime_ns},'tracks':[
                {'id':'v','mux_id':0,'type':'video','codec':'V_MPEG4/ISO/AVC','title':'original','language':'jpn'},
                {'id':'a','mux_id':7,'type':'audio','codec':'A_FLAC','title':'Main','language':'jpn','channels':2},
                {'id':'c','mux_id':8,'type':'audio','codec':'A_AAC','title':'Commentary','language':'jpn','channels':2},
            ]}], 'issues':[]}
            description = describe_batch(inventory, work, 'tv', {})
            target = description['suggested_targets'][0]
            target.update(episode='S01E01', video_properties={'language':'und','title':'VCB'})
            target['audio'] = [{'source':source.name,'track':'a','language':'eng','title':'2.0ch','forced':True}]
            target['default_audio'] = {'source':source.name,'track':'a'}
            with mock.patch('internal.movie_workbench.inspect_sources', return_value=inventory):
                result = build_movie_plan(work, {'configPath':str(config),'discovery':{},'route':{'branch':'anime'}}, {'title':'Komi','library_target':None,'batch_workbench':True,'movie_plan':{'schema_version':2,'targets':[target]}})
                for branch in ['anime', 'movie']:
                    candidate = json.loads(json.dumps(target))
                    if branch == 'movie': candidate['part'] = ''
                    decisions = {'title':'Komi','batch_workbench':True,'batch_features':{'subtitles':True},'batch_subtitle_groups':[{'id':'g','files':[{'id':'01.ass'}]}],'movie_plan':{'schema_version':2,'targets':[candidate]}}
                    failed = build_movie_plan(work, {'configPath':str(config),'discovery':{},'route':{'branch':branch}}, decisions)
                    self.assertIn('BATCH_SUBTITLE_MAPPING_REQUIRED', [i['code'] for i in failed['issues']])
                    candidate['subtitle_skips'] = ['g']
                    skipped = build_movie_plan(work, {'configPath':str(config),'discovery':{},'route':{'branch':branch}}, decisions)
                    self.assertNotIn('BATCH_SUBTITLE_MAPPING_REQUIRED', [i['code'] for i in skipped['issues']])
            self.assertEqual([], result['issues'])
            plan = result['plan']
            self.assertEqual([], validate_plan(work, 'tv', 'local-only', plan))
            job = plan['remuxJobs'][0]
            self.assertTrue(job['output'].endswith('Komi.S01E01.mkv'))
            self.assertEqual(['VCB','2.0ch'], [t['name'] for t in job['expectedTracks']])
            self.assertEqual('eng', job['expectedTracks'][1]['language'])
            self.assertEqual('A_FLAC', job['expectedTracks'][1]['codecId'])
            self.assertIn('1:7', job['arguments'][-1])
            self.assertNotIn('--sync', job['arguments'])
            self.assertEqual(b'source', source.read_bytes())


if __name__ == "__main__":
    unittest.main()
