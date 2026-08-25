"""Run the shared Hub capability contract against native Linux portable archives."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import portable_tools
import toolchain


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOL_IDS = ("mediainfo", "mkvtoolnix", "ffmpeg", "assfonts")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise portable_tools.PortableToolError(f"JSON root must be an object: {path}")
    return value


def _check_dependencies(root: Path, executables: list[str]) -> None:
    for name in executables:
        binary = root / "libexec" / name
        completed = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False)
        output = completed.stdout + "\n" + completed.stderr
        if "not found" in output:
            raise portable_tools.PortableToolError(f"portable {name} has a missing dynamic library: {output}")
        if completed.returncode != 0 and not any(
            marker in output.casefold() for marker in ("not a dynamic executable", "statically linked")
        ):
            raise portable_tools.PortableToolError(f"cannot inspect portable {name} dependencies: {output}")


def verify(architecture: str, artifacts: Path) -> dict[str, Any]:
    machine = portable_tools._native_architecture(architecture)
    manifest = _read_json(PROJECT_ROOT / "toolchain" / "manifest.json")
    tool_definitions = {item["tool_id"]: item for item in manifest["tools"]}
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"archive-portable-check-{architecture}-") as temporary:
        work = Path(temporary)
        for tool_id in TOOL_IDS:
            archive = artifacts / f"{tool_id}-linux-{architecture}.tar.gz"
            metadata = _read_json(artifacts / f"{archive.name}.json")
            if metadata["sha256"] != portable_tools._sha256(archive) or metadata["size"] != archive.stat().st_size:
                raise portable_tools.PortableToolError(f"artifact metadata mismatch: {archive.name}")
            root = work / f"中文 & {tool_id}"
            portable_tools.extract_portable_tar(archive, root)
            expected_paths = [root / item for item in metadata["executable_paths"]]
            if not all(path.is_file() and path.stat().st_mode & 0o111 for path in expected_paths):
                raise portable_tools.PortableToolError(f"artifact executable path or mode mismatch: {tool_id}")
            tool = tool_definitions[tool_id]
            _check_dependencies(root, list(tool["executables"]))
            candidate = root / "bin" if tool["path_kind"] == "directory" else root / metadata["executable_paths"][0]
            packaged_font = root / "share" / "fonts" / "DejaVuSans.ttf"
            original_font_check = toolchain._font_for_check
            if tool_id == "assfonts":
                toolchain._font_for_check = lambda: (packaged_font, "DejaVu Sans") if packaged_font.is_file() else None
            try:
                checked = toolchain.check_tool(tool, config={}, candidate=candidate)
            finally:
                toolchain._font_for_check = original_font_check
            if checked["status"] != "ready":
                failures = [
                    f"{item['capability_id']}: {item['reason']}"
                    for item in checked.get("capabilities", [])
                    if item.get("status") != "ready"
                ]
                raise portable_tools.PortableToolError(f"{tool_id} capability check failed: {'; '.join(failures)}")
            results.append(
                {
                    "tool_id": tool_id,
                    "version": checked["installed_version"],
                    "artifact": metadata,
                    "capabilities": checked["capabilities"],
                }
            )
    return {
        "schema_version": 1,
        "platform": "linux",
        "architecture": architecture,
        "native_machine": machine,
        "native_verified": True,
        "runner": platform.platform(),
        "path_fixture": "中文 & <tool_id>",
        "tools": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True, choices=tuple(portable_tools.ARCHITECTURES))
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify(args.architecture, args.artifacts.resolve(strict=True))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, KeyError, ValueError, portable_tools.PortableToolError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "report": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
