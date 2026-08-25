"""Validate portable-tool reports, prepare a Release, and update the tool manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOL_IDS = ("mediainfo", "mkvtoolnix", "ffmpeg", "assfonts")
ARCHITECTURES = ("amd64", "arm64")


class ReleaseError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata(dist: Path, *, require_artifacts: bool = True) -> dict[tuple[str, str], dict[str, Any]]:
    values: dict[tuple[str, str], dict[str, Any]] = {}
    for architecture in ARCHITECTURES:
        report_path = dist / f"capability-report-linux-{architecture}.json"
        report = _read_json(report_path)
        if report.get("architecture") != architecture or report.get("native_verified") is not True:
            raise ReleaseError(f"report is not a native {architecture} verification: {report_path.name}")
        report_items = {
            item.get("tool_id"): item for item in report.get("tools", []) if isinstance(item, dict)
        }
        report_tools = set(report_items)
        if report_tools != set(TOOL_IDS):
            raise ReleaseError(f"capability report has an invalid tool scope: {report_path.name}")
        for tool_id in TOOL_IDS:
            path = dist / f"{tool_id}-linux-{architecture}.tar.gz.json"
            item = _read_json(path)
            key = (tool_id, architecture)
            if (
                item.get("tool_id") != tool_id
                or item.get("architecture") != architecture
                or item.get("platform") != "linux"
                or item.get("native_built") is not True
            ):
                raise ReleaseError(f"invalid artifact metadata: {path.name}")
            artifact = dist / str(item["filename"])
            report_item = report_items[tool_id]
            capabilities = report_item.get("capabilities") or []
            if (
                report_item.get("artifact") != item
                or not capabilities
                or any(capability.get("status") != "ready" for capability in capabilities)
            ):
                raise ReleaseError(f"capability evidence does not match artifact metadata: {path.name}")
            if require_artifacts:
                if (
                    not artifact.is_file()
                    or artifact.stat().st_size != item.get("size")
                    or _sha256(artifact) != item.get("sha256")
                ):
                    raise ReleaseError(f"artifact file does not match metadata: {artifact.name}")
            values[key] = item
    return values


def prepare_release(dist: Path, tag: str, notes_path: Path) -> dict[str, Any]:
    values = _metadata(dist)
    index = {
        "schema_version": 1,
        "release_tag": tag,
        "verification": {
            "amd64": "clean python:3.11-slim-bookworm on native GitHub runner ubuntu-24.04",
            "arm64": "clean python:3.11-slim-bookworm on native GitHub runner ubuntu-24.04-arm",
        },
        "artifacts": [values[(tool_id, architecture)] for tool_id in TOOL_IDS for architecture in ARCHITECTURES],
    }
    index_path = dist / "portable-tools-index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "Linux portable tools for the Bangumi Media Hub base container.",
        "",
        "Both architectures ran the real shared capability contract in a clean python:3.11-slim-bookworm container",
        "on native GitHub-hosted runners; arm64 was not accepted from extraction-only or QEMU-only evidence.",
        "",
        "| Tool | Architecture | Version | SHA-256 |",
        "|---|---|---|---|",
    ]
    for tool_id in TOOL_IDS:
        for architecture in ARCHITECTURES:
            item = values[(tool_id, architecture)]
            lines.append(f"| {tool_id} | {architecture} | {item['version']} | `{item['sha256']}` |")
    lines.extend(
        (
            "",
            "The JSON capability reports and artifact metadata are attached for audit. Packages install no system",
            "files at runtime and contain no KDocs, otf2ttf, credentials, media rules, or task state.",
            "",
        )
    )
    notes_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return index


def update_manifest(dist: Path, tag: str, manifest_version: str, manifest_path: Path) -> None:
    values = _metadata(dist, require_artifacts=False)
    manifest = _read_json(manifest_path)
    manifest["manifest_version"] = manifest_version
    tools = {item["tool_id"]: item for item in manifest["tools"]}
    for tool_id in TOOL_IDS:
        tool = tools[tool_id]
        windows = [item for item in tool["artifacts"] if item["platform"] == "windows"]
        linux = []
        for architecture in ARCHITECTURES:
            item = values[(tool_id, architecture)]
            linux.append(
                {
                    "version": item["version"],
                    "platform": "linux",
                    "architecture": architecture,
                    "artifact_url": (
                        "https://github.com/hm27651/archive-plex-anime/releases/download/"
                        f"{tag}/{item['filename']}"
                    ),
                    "filename": item["filename"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "archive_format": item["archive_format"],
                    "executable_paths": item["executable_paths"],
                }
            )
        tool["artifacts"] = windows + linux
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--dist", required=True, type=Path)
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--notes", required=True, type=Path)
    apply = subparsers.add_parser("apply-manifest")
    apply.add_argument("--dist", required=True, type=Path)
    apply.add_argument("--tag", required=True)
    apply.add_argument("--manifest-version", required=True)
    apply.add_argument("--manifest", type=Path, default=PROJECT_ROOT / "toolchain" / "manifest.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_release(args.dist.resolve(strict=True), args.tag, args.notes)
        else:
            update_manifest(
                args.dist.resolve(strict=True), args.tag, args.manifest_version, args.manifest.resolve(strict=True)
            )
    except (OSError, KeyError, ValueError, ReleaseError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
