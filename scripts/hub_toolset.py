"""Assemble deterministic Hub toolsets from the fixed single-tool manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import portable_tools
import toolchain


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "toolchain" / "manifest.json"
SOURCE_LOCK_PATH = PROJECT_ROOT / "toolchain" / "linux-sources.json"
TOOLSET_ID = "hub"
TARGETS = {("windows", "x64"), ("linux", "amd64"), ("linux", "arm64")}
LICENSE_PATTERN = re.compile(r"(?:^|/)(?:license|copying|copyright|notice)(?:[._-].*)?$", re.IGNORECASE)


class HubToolsetError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HubToolsetError(f"JSON root must be an object: {path}")
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_target(platform_name: str, architecture: str) -> None:
    system = "windows" if os.name == "nt" else platform.system().casefold()
    machine = platform.machine().casefold()
    normalized = "x64" if machine in {"amd64", "x86_64"} and system == "windows" else machine
    if system == "linux" and machine in {"amd64", "x86_64"}:
        normalized = "amd64"
    elif system == "linux" and machine in {"arm64", "aarch64"}:
        normalized = "arm64"
    if (system, normalized) != (platform_name, architecture):
        raise HubToolsetError(
            f"native toolset assembly mismatch: requested {platform_name}/{architecture}, "
            f"running {system}/{normalized or 'unknown'}"
        )


def _artifact(tool: dict[str, Any], platform_name: str, architecture: str) -> dict[str, Any]:
    selected = next(
        (
            item
            for item in tool["artifacts"]
            if item["platform"] == platform_name and item["architecture"] == architecture
        ),
        None,
    )
    if selected is None:
        raise HubToolsetError(f"single-tool artifact is missing: {tool['tool_id']} {platform_name}/{architecture}")
    return selected


def _obtain_artifact(artifact: dict[str, Any], input_dir: Path, *, fetch: bool) -> Path:
    path = input_dir / str(artifact["filename"])
    if not path.is_file():
        if not fetch:
            raise HubToolsetError(f"single-tool artifact is missing from input: {path.name}")
        input_dir.mkdir(parents=True, exist_ok=True)
        portable_tools._download(str(artifact["artifact_url"]), str(artifact["sha256"]), path)
    if path.stat().st_size != artifact["size"] or portable_tools._sha256(path) != artifact["sha256"]:
        raise HubToolsetError(f"single-tool artifact does not match manifest: {path.name}")
    return path


def _copy_licenses(tool: dict[str, Any], tool_root: Path, package_root: Path) -> list[str]:
    destination = package_root / "licenses" / tool["tool_id"]
    destination.mkdir(parents=True, exist_ok=True)
    source_record = destination / "SOURCE.json"
    source_record.write_text(
        json.dumps(
            {
                "tool_id": tool["tool_id"],
                "name": tool["name"],
                "declared_license": tool["license"],
                "project_url": tool["project_url"],
                "download_page_url": tool["download_page_url"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    copied = [source_record.relative_to(package_root).as_posix()]
    for source in sorted(tool_root.rglob("*"), key=lambda item: item.relative_to(tool_root).as_posix()):
        if not source.is_file():
            continue
        relative = source.relative_to(tool_root).as_posix()
        if LICENSE_PATTERN.search(relative) is None:
            continue
        target = destination / "bundled" / Path(*Path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target.relative_to(package_root).as_posix())
    return copied


def _fixture(package_root: Path, work: Path) -> tuple[str, str]:
    lock = _read_json(SOURCE_LOCK_PATH)
    definition = lock["fixtures"]["assfonts_test_font"]
    archive = work / "dejavu-sans.zip"
    portable_tools._download(str(definition["url"]), str(definition["sha256"]), archive)
    extracted = work / "dejavu"
    portable_tools.extract_portable_archive(archive, extracted, "zip")
    font_source = extracted / Path(str(definition["font_path"]))
    license_source = extracted / Path(str(definition["license_path"]))
    if not font_source.is_file() or not license_source.is_file():
        raise HubToolsetError("DejaVu Sans fixture archive has an unexpected layout")
    font = package_root / "fixtures" / "fonts" / "DejaVuSans.ttf"
    license_path = package_root / "licenses" / "dejavu-fonts" / "LICENSE"
    font.parent.mkdir(parents=True, exist_ok=True)
    license_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(font_source, font)
    shutil.copy2(license_source, license_path)
    return font.relative_to(package_root).as_posix(), license_path.relative_to(package_root).as_posix()


def assemble(
    platform_name: str,
    architecture: str,
    version: str,
    input_dir: Path,
    output_dir: Path,
    *,
    fetch: bool = False,
) -> dict[str, Any]:
    if (platform_name, architecture) not in TARGETS:
        raise HubToolsetError(f"unsupported Hub toolset target: {platform_name}/{architecture}")
    _native_target(platform_name, architecture)
    manifest = toolchain.load_manifest()
    tools_by_id = {item["tool_id"]: item for item in manifest["tools"]}
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"archive-hub-toolset-{platform_name}-{architecture}-") as temporary:
        work = Path(temporary)
        package_root = work / "toolset"
        tool_records: list[dict[str, Any]] = []
        license_paths: list[str] = []
        for tool_id in toolchain.HUB_TOOL_IDS:
            tool = tools_by_id[tool_id]
            artifact = _artifact(tool, platform_name, architecture)
            archive = _obtain_artifact(artifact, input_dir, fetch=fetch)
            tool_root = package_root / "tools" / tool_id / str(artifact["version"])
            portable_tools.extract_portable_archive(archive, tool_root, str(artifact["archive_format"]))
            commands = {
                executable: (
                    tool_root / Path(str(relative))
                ).relative_to(package_root).as_posix()
                for executable, relative in zip(tool["executables"], artifact["executable_paths"], strict=True)
            }
            missing = [name for name, path in commands.items() if not (package_root / path).is_file()]
            if missing:
                raise HubToolsetError(f"toolset command paths are missing for {tool_id}: {missing}")
            license_paths.extend(_copy_licenses(tool, tool_root, package_root))
            tool_records.append(
                {
                    "id": tool_id,
                    "name": tool["name"],
                    "version": artifact["version"],
                    "path_kind": tool["path_kind"],
                    "path_setting": tool["path_setting"],
                    "commands": commands,
                    "capabilities": [item["capability_id"] for item in tool["capability_checks"]],
                    "source_artifact": {
                        "filename": artifact["filename"],
                        "sha256": artifact["sha256"],
                        "size": artifact["size"],
                        "url": artifact["artifact_url"],
                    },
                }
            )
        fixture_path, fixture_license = _fixture(package_root, work)
        license_paths.append(fixture_license)
        toolset = {
            "schema_version": 1,
            "toolset_id": TOOLSET_ID,
            "version": version,
            "platform": platform_name,
            "architecture": architecture,
            "source_manifest_version": manifest["manifest_version"],
            "source_manifest_sha256": _canonical_sha256(manifest),
            "tool_ids": list(toolchain.HUB_TOOL_IDS),
            "tools": tool_records,
            "fixtures": {
                "assfonts_test_font": {
                    "path": fixture_path,
                    "family": "DejaVu Sans",
                    "license_path": fixture_license,
                }
            },
            "license_paths": sorted(set(license_paths)),
        }
        manifest_path = package_root / "toolset.json"
        manifest_path.write_text(json.dumps(toolset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        extension = "zip" if platform_name == "windows" else "tar.gz"
        filename = f"hub-toolset-{platform_name}-{architecture}.{extension}"
        output = output_dir / filename
        if platform_name == "windows":
            portable_tools.create_deterministic_zip(package_root, output)
        else:
            portable_tools.create_deterministic_tar(package_root, output)
        metadata = {
            "schema_version": 1,
            "toolset_id": TOOLSET_ID,
            "version": version,
            "platform": platform_name,
            "architecture": architecture,
            "native_built": True,
            "filename": filename,
            "sha256": portable_tools._sha256(output),
            "size": output.stat().st_size,
            "archive_format": extension,
            "manifest_path": "toolset.json",
            "tool_ids": list(toolchain.HUB_TOOL_IDS),
            "fixture_paths": [fixture_path],
            "license_paths": sorted(set(license_paths)),
            "source_manifest_version": manifest["manifest_version"],
            "source_manifest_sha256": toolset["source_manifest_sha256"],
        }
        metadata_path = output_dir / f"{filename}.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows", "linux"))
    parser.add_argument("--architecture", required=True, choices=("x64", "amd64", "arm64"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = assemble(
            args.platform,
            args.architecture,
            args.version,
            args.input.resolve(strict=False),
            args.output.resolve(strict=False),
            fetch=args.fetch,
        )
    except (OSError, KeyError, ValueError, HubToolsetError, portable_tools.PortableToolError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "toolset": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
