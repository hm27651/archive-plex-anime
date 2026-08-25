from __future__ import annotations

import copy
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import toolchain  # noqa: E402
import portable_tools  # noqa: E402
import workflow  # noqa: E402


class ToolManifestTests(unittest.TestCase):
    def test_manifest_matches_schema_and_has_unique_public_ids(self):
        manifest = toolchain.load_manifest()
        tool_ids = [item["tool_id"] for item in manifest["tools"]]
        capability_ids = [
            (item["tool_id"], capability["capability_id"])
            for item in manifest["tools"]
            for capability in item["capability_checks"]
        ]

        self.assertEqual(len(tool_ids), len(set(tool_ids)))
        self.assertEqual(len(capability_ids), len(set(capability_ids)))
        self.assertEqual(manifest["schema_version"], 1)

    def test_manifest_rejects_duplicate_tool_and_artifact_targets(self):
        manifest = toolchain.load_manifest()
        duplicate_tool = copy.deepcopy(manifest)
        duplicate_tool["tools"].append(copy.deepcopy(duplicate_tool["tools"][0]))
        with self.assertRaisesRegex(toolchain.ToolchainError, "duplicate tool_id"):
            toolchain.validate_manifest(duplicate_tool)

        duplicate_artifact = copy.deepcopy(manifest)
        duplicate_artifact["tools"][0]["artifacts"].append(
            copy.deepcopy(duplicate_artifact["tools"][0]["artifacts"][0])
        )
        with self.assertRaisesRegex(toolchain.ToolchainError, "duplicate artifact target"):
            toolchain.validate_manifest(duplicate_artifact)

        duplicate_capability = copy.deepcopy(manifest)
        duplicate_capability["tools"][0]["capability_checks"].append(
            copy.deepcopy(duplicate_capability["tools"][0]["capability_checks"][0])
        )
        with self.assertRaisesRegex(toolchain.ToolchainError, "duplicate capability_id"):
            toolchain.validate_manifest(duplicate_capability)

    def test_manifest_rejects_non_https_urls_and_invalid_hashes(self):
        manifest = toolchain.load_manifest()
        insecure = copy.deepcopy(manifest)
        insecure["tools"][0]["project_url"] = "http://example.invalid/tool"
        with self.assertRaises(toolchain.ToolchainError) as raised:
            toolchain.validate_manifest(insecure)
        self.assertEqual(raised.exception.code, "MANIFEST_SCHEMA_INVALID")

        invalid_hash = copy.deepcopy(manifest)
        invalid_hash["tools"][0]["artifacts"][0]["sha256"] = "latest"
        with self.assertRaises(toolchain.ToolchainError) as raised:
            toolchain.validate_manifest(invalid_hash)
        self.assertEqual(raised.exception.code, "MANIFEST_SCHEMA_INVALID")

    def test_hub_projection_excludes_kdocs_and_internal_font_converter(self):
        projection = toolchain.export_projection("hub")
        ids = [item["id"] for item in projection["tools"]]

        self.assertEqual(ids, ["mediainfo", "mkvtoolnix", "ffmpeg", "assfonts"])
        self.assertNotIn("kdocs-cli", json.dumps(projection, ensure_ascii=False))
        self.assertNotIn("otf2ttf", json.dumps(projection, ensure_ascii=False))
        self.assertTrue(all("download_url" in item for item in projection["tools"]))
        self.assertTrue(all("capabilities" in item for item in projection["tools"]))

    def test_hub_projection_preserves_current_adapter_ids_and_path_settings(self):
        projection = toolchain.export_projection("hub")
        tools = {item["id"]: item for item in projection["tools"]}

        self.assertEqual(
            {tool_id: item["path_setting"] for tool_id, item in tools.items()},
            {
                "mediainfo": "mediainfo_path",
                "mkvtoolnix": "mkvtoolnix_dir",
                "ffmpeg": "ffmpeg_dir",
                "assfonts": "assfonts_path",
            },
        )
        self.assertEqual(
            [item["id"] for item in tools["mkvtoolnix"]["capabilities"]],
            ["version", "identify", "inspect", "extract"],
        )
        self.assertEqual(
            [item["id"] for item in tools["ffmpeg"]["capabilities"]],
            ["version", "pcm_decode", "probe"],
        )

    def test_export_is_deterministic_and_contains_no_runtime_state(self):
        with mock.patch.object(toolchain, "_source_commit", return_value="a" * 40):
            first = toolchain.export_projection("hub")
            second = toolchain.export_projection("hub")

        self.assertEqual(first, second)
        encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
        for forbidden in ("installed_version", '"paths"', "not_configured", "media_rules", "task_state"):
            self.assertNotIn(forbidden, encoded)

    def test_skill_projection_retains_kdocs(self):
        projection = toolchain.export_projection("skill")
        ids = {item["id"] for item in projection["tools"]}

        self.assertIn("kdocs-cli", ids)
        self.assertIn("otf2ttf", ids)

    def test_artifact_selection_is_platform_and_architecture_specific(self):
        manifest = toolchain.load_manifest()
        ffmpeg = next(item for item in manifest["tools"] if item["tool_id"] == "ffmpeg")

        self.assertEqual(toolchain.select_artifact(ffmpeg, "windows", "x64")["version"], "8.1.2-44-g7c533d0f86")
        self.assertIsNone(toolchain.select_artifact(ffmpeg, "linux", "amd64"))


class PortableArchiveTests(unittest.TestCase):
    def test_deterministic_tar_has_stable_bytes_and_normalized_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package"
            (source / "bin").mkdir(parents=True)
            executable = source / "bin" / "工具"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
            executable.chmod(0o755)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"

            portable_tools.create_deterministic_tar(source, first)
            executable.touch()
            portable_tools.create_deterministic_tar(source, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first, "r:gz") as archive:
                member = archive.getmember("bin/工具")
            self.assertEqual((member.uid, member.gid, member.mtime, member.mode), (0, 0, 0, 0o755))

    def test_portable_extraction_rejects_traversal_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, member in (
                ("traversal.tar.gz", tarfile.TarInfo("../escape")),
                ("link.tar.gz", tarfile.TarInfo("bin/link")),
            ):
                archive = root / name
                if name.startswith("link"):
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                with tarfile.open(archive, "w:gz") as output:
                    output.addfile(member)
                with self.subTest(name=name):
                    with self.assertRaises(portable_tools.PortableToolError):
                        portable_tools.extract_portable_tar(archive, root / f"extract-{name}")


class ToolVersionTests(unittest.TestCase):
    def tool(self, tool_id: str) -> dict:
        return next(item for item in toolchain.load_manifest()["tools"] if item["tool_id"] == tool_id)

    def test_supported_tool_versions_are_recognized(self):
        examples = {
            "mediainfo": ("MediaInfo Command line\nMediaInfoLib - v26.05", "26.05"),
            "mkvtoolnix": ("mkvmerge v100.0 ('You Oughta Know')", "100.0"),
            "ffmpeg": ("ffmpeg version n8.1.2-44-g7c533d0f86 Copyright", "n8.1.2-44-g7c533d0f86"),
            "assfonts": ("assfonts v0.7.3", "0.7.3"),
            "kdocs-cli": ("2.5.13", "2.5.13"),
        }
        for tool_id, (output, expected) in examples.items():
            with self.subTest(tool_id=tool_id):
                self.assertEqual(toolchain._probe_version(tool_id, output), (True, expected))
                self.assertEqual(toolchain._version_from_output(tool_id, output), expected)

    def test_successful_command_with_unrecognized_output_is_not_a_version(self):
        for tool_id in ("mediainfo", "mkvtoolnix", "ffmpeg", "assfonts", "kdocs-cli", "otf2ttf"):
            with self.subTest(tool_id=tool_id):
                self.assertEqual(toolchain._probe_version(tool_id, "ordinary successful program"), (False, ""))

    def test_kdocs_uses_version_command_and_requires_numeric_output(self):
        tool = self.tool("kdocs-cli")
        executable = Path("C:/Tools/kdocs-cli.exe")

        self.assertEqual(toolchain._version_command(tool, {"kdocs-cli": executable}), [str(executable), "--version"])
        self.assertEqual(toolchain._probe_version("kdocs-cli", "KDocs CLI help\n2.5.13\nusage"), (False, ""))

    def test_otf2ttf_usage_proves_identity_without_fabricating_version(self):
        tool = self.tool("otf2ttf")
        executable = Path("C:/Tools/otf2ttf.exe")

        self.assertEqual(toolchain._version_command(tool, {"otf2ttf": executable}), [str(executable), "--help"])
        self.assertEqual(toolchain._probe_version("otf2ttf", "usage: otf2ttf [-h] input_file"), (True, ""))

    def test_unrecognized_version_output_prevents_ready_status(self):
        tool = self.tool("kdocs-cli")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / toolchain._executable_name("kdocs-cli")
            candidate.write_bytes(b"fake")
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            result = toolchain.check_tool(
                tool,
                config={},
                candidate=candidate,
                runner=lambda _arguments: {"ok": True, "stdout": "ordinary successful program", "stderr": ""},
            )

        self.assertEqual(result["status"], "capability_failed")
        self.assertEqual(result["installed_version"], "")
        self.assertEqual(result["capabilities"][0]["reason"], "version output is not recognizable")

    def test_otf2ttf_can_be_ready_with_empty_installed_version(self):
        tool = self.tool("otf2ttf")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / toolchain._executable_name("otf2ttf")
            candidate.write_bytes(b"fake")
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            result = toolchain.check_tool(
                tool,
                config={},
                candidate=candidate,
                runner=lambda _arguments: {"ok": True, "stdout": "usage: otf2ttf [-h] input_file", "stderr": ""},
            )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["installed_version"], "")
        self.assertEqual(result["capabilities"][0]["status"], "ready")


class ToolPathTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        selected = root / "config.json"
        selected.write_text(
            json.dumps(
                {
                    "paths": {"workRoot": "preserve"},
                    "tools": {"python": sys.executable, "assfonts": "preserve-assfonts"},
                    "metadata": {"enabled": True},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return selected

    def test_list_succeeds_when_all_tools_are_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text("{}", encoding="utf-8")
            with mock.patch.object(toolchain.shutil, "which", return_value=None):
                result = toolchain.list_tools("hub", config)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["tools"]), 4)
        self.assertTrue(all(item["status"] == "not_configured" for item in result["tools"]))

    def test_check_only_dispatches_requested_tool(self):
        seen: list[str] = []

        def fake_check(item, **_kwargs):
            seen.append(item["tool_id"])
            return {"tool_id": item["tool_id"], "status": "ready"}

        with mock.patch.object(toolchain, "_optional_config", return_value=({}, Path("config.json"))):
            with mock.patch.object(toolchain, "check_tool", side_effect=fake_check):
                result = toolchain.check_tools("hub", ["ffmpeg"])

        self.assertEqual(seen, ["ffmpeg"])
        self.assertEqual(result["tools"], [{"tool_id": "ffmpeg", "status": "ready"}])

    def test_hub_cannot_select_kdocs(self):
        with self.assertRaises(toolchain.ToolchainError) as raised:
            toolchain.check_tools("hub", ["kdocs-cli"])
        self.assertEqual(raised.exception.code, "TOOL_UNKNOWN")

    def test_unknown_platform_has_stable_status(self):
        manifest = toolchain.load_manifest()
        selected = manifest["tools"][0]
        with mock.patch.object(toolchain, "current_platform", return_value=("plan9", "mips")):
            result = toolchain.check_tool(selected, config={})
        self.assertEqual(result["status"], "unsupported_platform")

    def test_directory_tool_requires_every_executable_before_checking(self):
        manifest = toolchain.load_manifest()
        selected = next(item for item in manifest["tools"] if item["tool_id"] == "mkvtoolnix")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("mkvmerge", "mkvinfo"):
                path = root / toolchain._executable_name(name)
                path.write_bytes(b"fake")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            result = toolchain.check_tool(selected, config={}, candidate=root, run_capabilities=False)

        self.assertEqual(result["status"], "missing")
        self.assertIn("mkvextract", result["reason"])

    def test_use_path_rejects_relative_path_without_touching_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.write_config(Path(temporary))
            before = config.read_bytes()
            with self.assertRaises(toolchain.ToolchainError) as raised:
                toolchain.update_tool_path("mediainfo", Path("tools/MediaInfo.exe"), config)
            self.assertEqual(raised.exception.code, "ABSOLUTE_PATH_REQUIRED")
            self.assertEqual(config.read_bytes(), before)

    def test_failed_capability_check_preserves_current_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            candidate = root / "中文 & MediaInfo.exe"
            candidate.write_bytes(b"fake")
            before = config.read_bytes()
            failure = {"status": "capability_failed", "reason": "controlled failure"}
            with mock.patch.object(toolchain, "check_tool", return_value=failure):
                with self.assertRaises(toolchain.ToolchainError) as raised:
                    toolchain.update_tool_path("mediainfo", candidate, config)
            self.assertEqual(raised.exception.code, "TOOL_CHECK_FAILED")
            self.assertEqual(config.read_bytes(), before)

    def test_use_path_preserves_config_when_version_identity_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            candidate = root / toolchain._executable_name("kdocs-cli")
            candidate.write_bytes(b"fake")
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            before = config.read_bytes()
            completed = {"ok": True, "stdout": "ordinary successful program", "stderr": ""}
            with mock.patch.object(toolchain, "_run_command", return_value=completed):
                with self.assertRaises(toolchain.ToolchainError) as raised:
                    toolchain.update_tool_path("kdocs-cli", candidate, config)

            self.assertEqual(raised.exception.code, "TOOL_CHECK_FAILED")
            self.assertEqual(config.read_bytes(), before)

    def test_successful_use_path_changes_only_the_selected_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            candidate = root / "中文 & MediaInfo.exe"
            candidate.write_bytes(b"fake")
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            ready = {
                "tool_id": "mediainfo",
                "status": "ready",
                "reason": "",
                "paths": {"mediainfo": str(candidate)},
            }
            with mock.patch.object(toolchain, "check_tool", return_value=ready) as checked:
                result = toolchain.update_tool_path("mediainfo", candidate, config)
            saved = json.loads(config.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "OK")
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(saved["tools"]["mediainfo_path"], str(candidate.resolve(strict=False)))
        self.assertEqual(saved["tools"]["mediainfo"], str(candidate.resolve(strict=False)))
        self.assertEqual(saved["tools"]["assfonts"], "preserve-assfonts")
        self.assertEqual(saved["paths"], {"workRoot": "preserve"})
        self.assertEqual(saved["metadata"], {"enabled": True})

    def test_failed_recheck_rolls_back_atomic_config_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root)
            candidate = root / "MediaInfo.exe"
            candidate.write_bytes(b"fake")
            before = config.read_bytes()
            ready = {"status": "ready", "reason": "", "paths": {"mediainfo": str(candidate)}}
            failed = {"status": "capability_failed", "reason": "recheck failed"}
            with mock.patch.object(toolchain, "check_tool", side_effect=[ready, failed]):
                with self.assertRaises(toolchain.ToolchainError) as raised:
                    toolchain.update_tool_path("mediainfo", candidate, config)
            self.assertEqual(raised.exception.code, "TOOL_RECHECK_FAILED")
            self.assertEqual(config.read_bytes(), before)

    def test_subprocess_arguments_preserve_ampersand_without_shell(self):
        marker = "中文 & path"
        result = toolchain._run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", marker])

        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), marker)


class ToolCliTests(unittest.TestCase):
    def test_tools_list_routes_through_public_cli(self):
        payload = {"status": "OK", "entrypoint": "hub", "tools": []}
        stdout = io.StringIO()
        with mock.patch.object(workflow, "list_tools", return_value=payload) as listed:
            with redirect_stdout(stdout):
                code = workflow.main(["tools", "list", "--entrypoint", "hub", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), payload)
        listed.assert_called_once_with("hub")

    def test_tools_check_reports_stable_unknown_tool_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = workflow.main(["tools", "check", "--entrypoint", "hub", "--tool", "kdocs-cli", "--json"])

        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["code"], "TOOL_UNKNOWN")

    def test_tools_export_writes_utf8_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "Hub 工具清单.json"
            stdout = io.StringIO()
            with mock.patch.object(toolchain, "_source_commit", return_value="b" * 40):
                with redirect_stdout(stdout):
                    code = workflow.main(
                        ["tools", "export", "--entrypoint", "hub", "--output", str(output), "--json"]
                    )
            exported = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(exported["entrypoint"], "hub")
        self.assertEqual([item["id"] for item in exported["tools"]], ["mediainfo", "mkvtoolnix", "ffmpeg", "assfonts"])
        self.assertEqual(json.loads(stdout.getvalue())["projection"], exported)


if __name__ == "__main__":
    unittest.main()
