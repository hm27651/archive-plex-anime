"""Build and inspect deterministic Linux portable tool archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_LOCK_PATH = PROJECT_ROOT / "toolchain" / "linux-sources.json"
MANIFEST_PATH = PROJECT_ROOT / "toolchain" / "manifest.json"
ARCHITECTURES = {"amd64": {"x86_64", "amd64"}, "arm64": {"aarch64", "arm64"}}
SYSTEM_LIBRARIES = {
    "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2",
    "libc.so.6",
    "libdl.so.2",
    "libm.so.6",
    "libpthread.so.0",
    "librt.so.1",
}


class PortableToolError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PortableToolError(f"JSON root must be an object: {path}")
    return value


def _run(arguments: list[str], *, timeout: int = 600, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=timeout, env=env)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PortableToolError(f"command failed ({completed.returncode}): {arguments[0]}: {detail[-2000:]}")
    return completed


def _native_architecture(architecture: str) -> str:
    machine = platform.machine().casefold()
    if architecture not in ARCHITECTURES or machine not in ARCHITECTURES[architecture]:
        raise PortableToolError(f"native runner mismatch: requested {architecture}, running {machine or 'unknown'}")
    return machine


def _configure_snapshot(lock: dict[str, Any]) -> None:
    snapshot = str(lock["debian_snapshot"])
    release = str(lock["debian_release"])
    marker = Path(f"/tmp/archive-toolchain-snapshot-{snapshot}")
    if marker.is_file():
        return
    sources = "\n".join(
        (
            f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/{snapshot}/ {release} main",
            f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/{snapshot}/ {release}-updates main",
            f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/{snapshot}/ {release}-security main",
            "",
        )
    )
    Path("/etc/apt/sources.list").write_text(sources, encoding="utf-8")
    deb822 = Path("/etc/apt/sources.list.d/debian.sources")
    if deb822.exists():
        deb822.unlink()
    _run(["apt-get", "-o", "Acquire::Check-Valid-Until=false", "update"])
    marker.write_text(snapshot, encoding="ascii")


def _install(packages: list[str]) -> None:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    _run(["apt-get", "install", "--no-install-recommends", "-y", *packages], env=env)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, expected_sha256: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "archive-plex-anime-portable-builder/1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = _sha256(destination)
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise PortableToolError(f"upstream checksum mismatch: expected {expected_sha256}, got {actual}")


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise PortableToolError(f"unsafe upstream archive path: {name}")
    return path


def _extract_assfonts(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as source:
        members = {str(_safe_member_path(item.name)): item for item in source.getmembers() if item.isfile()}
        binary_member = members.get("bin/assfonts")
        if binary_member is None:
            raise PortableToolError("assfonts archive does not contain bin/assfonts")
        extracted = destination / "assfonts"
        stream = source.extractfile(binary_member)
        if stream is None:
            raise PortableToolError("cannot read assfonts binary")
        with extracted.open("wb") as output:
            shutil.copyfileobj(stream, output)
        extracted.chmod(0o755)
        notices = destination / "upstream-notices"
        notices.mkdir()
        for name, member in sorted(members.items()):
            if not re.search(r"(?:^|/)(?:LICENSE|NOTICE|copyright)(?:\.|$)", name, re.IGNORECASE):
                continue
            source_stream = source.extractfile(member)
            if source_stream is None:
                continue
            target = notices / PurePosixPath(name).name
            with target.open("wb") as output:
                shutil.copyfileobj(source_stream, output)
        return extracted


def _dependencies(binary: Path) -> list[Path]:
    completed = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False)
    combined = completed.stdout + "\n" + completed.stderr
    if "not a dynamic executable" in combined.casefold() or "statically linked" in combined.casefold():
        return []
    if completed.returncode != 0:
        raise PortableToolError(f"cannot inspect dynamic dependencies for {binary}: {combined.strip()}")
    if "not found" in combined:
        raise PortableToolError(f"unresolved dynamic dependency for {binary}: {combined}")
    paths: set[Path] = set()
    for line in combined.splitlines():
        match = re.search(r"(?:=>\s*)?(/[^(\s]+)", line)
        if match is None:
            continue
        selected = Path(match.group(1))
        if selected.name not in SYSTEM_LIBRARIES:
            paths.add(selected)
    return sorted(paths, key=lambda item: item.name)


def _copy_unique(source: Path, destination: Path) -> None:
    if destination.exists():
        if _sha256(source) != _sha256(destination):
            raise PortableToolError(f"conflicting runtime libraries: {source.name}")
        return
    shutil.copy2(source, destination, follow_symlinks=True)
    destination.chmod(0o755 if os.access(source, os.X_OK) else 0o644)


def _launcher(name: str, *, assfonts: bool = False) -> str:
    environment = ""
    if assfonts:
        environment = (
            'export FONTCONFIG_PATH="$ROOT/etc/fonts"\n'
            'export FONTCONFIG_FILE="$ROOT/etc/fonts/fonts.conf"\n'
            'export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${TMPDIR:-/tmp}/archive-assfonts-cache}"\n'
        )
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        'export LD_LIBRARY_PATH="$ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        f"{environment}"
        f'exec "$ROOT/libexec/{name}" "$@"\n'
    )


def _owning_package(path: Path) -> str | None:
    completed = subprocess.run(["dpkg-query", "-S", str(path)], capture_output=True, text=True, check=False)
    if completed.returncode != 0 or ":" not in completed.stdout:
        return None
    return completed.stdout.split(":", 1)[0].strip()


def _copy_licenses(packages: set[str], root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in sorted(packages):
        version = _run(["dpkg-query", "-W", "-f=${Version}", package]).stdout.strip()
        versions[package] = version
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if copyright_file.is_file():
            target = root / "share" / "licenses" / package
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copyright_file, target / "copyright")
    return versions


def _stage_binary(source: Path, name: str, root: Path, packages: set[str], *, assfonts: bool = False) -> None:
    libexec = root / "libexec"
    libraries = root / "lib"
    binaries = root / "bin"
    libexec.mkdir(parents=True, exist_ok=True)
    libraries.mkdir(parents=True, exist_ok=True)
    binaries.mkdir(parents=True, exist_ok=True)
    target = libexec / name
    shutil.copy2(source, target, follow_symlinks=True)
    target.chmod(0o755)
    package = _owning_package(source)
    if package:
        packages.add(package)
    for dependency in _dependencies(source):
        _copy_unique(dependency, libraries / dependency.name)
        package = _owning_package(dependency)
        if package:
            packages.add(package)
    entrypoint = binaries / name
    entrypoint.write_text(_launcher(name, assfonts=assfonts), encoding="utf-8", newline="\n")
    entrypoint.chmod(0o755)


def _fontconfig(root: Path) -> None:
    fonts = root / "share" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    source_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not source_font.is_file():
        raise PortableToolError("controlled DejaVu Sans test font is missing")
    shutil.copy2(source_font, fonts / source_font.name)
    config = root / "etc" / "fonts"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fonts.conf").write_text(
        """<?xml version=\"1.0\"?>
<!DOCTYPE fontconfig SYSTEM \"urn:fontconfig:fonts.dtd\">
<fontconfig>
  <dir prefix=\"relative\">../../share/fonts</dir>
  <cachedir prefix=\"xdg\">fontconfig</cachedir>
</fontconfig>
""",
        encoding="utf-8",
        newline="\n",
    )


def create_deterministic_tar(source: Path, output: Path) -> None:
    source = source.resolve(strict=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
                    relative = path.relative_to(source).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    if info.isdir():
                        info.mode = 0o755
                        archive.addfile(info)
                    elif info.isfile():
                        executable_tree = relative.startswith("bin/") or relative.startswith("libexec/")
                        info.mode = 0o755 if executable_tree or path.stat().st_mode & stat.S_IXUSR else 0o644
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        raise PortableToolError(f"portable package cannot contain links or special files: {relative}")


def create_deterministic_zip(source: Path, output: Path) -> None:
    source = source.resolve(strict=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
            relative = path.relative_to(source).as_posix()
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise PortableToolError(f"portable package cannot contain links or special files: {relative}")
            name = f"{relative}/" if path.is_dir() else relative
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            executable_tree = relative.startswith("tools/") and path.suffix.casefold() in {".exe", ".cmd", ".bat"}
            mode = 0o755 if path.is_dir() or executable_tree else 0o644
            info.external_attr = ((stat.S_IFDIR if path.is_dir() else stat.S_IFREG) | mode) << 16
            if path.is_dir():
                archive.writestr(info, b"")
            else:
                archive.writestr(info, path.read_bytes())


def extract_portable_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=True)
    with tarfile.open(archive, "r:*") as source:
        for member in source.getmembers():
            relative = _safe_member_path(member.name)
            target = destination.joinpath(*relative.parts)
            resolved = target.resolve(strict=False)
            if root != resolved and root not in resolved.parents:
                raise PortableToolError(f"archive path escapes extraction root: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
                continue
            if not member.isfile():
                raise PortableToolError(f"portable archive contains a link or special file: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise PortableToolError(f"cannot read portable archive member: {member.name}")
            with target.open("wb") as output:
                shutil.copyfileobj(stream, output)
            target.chmod(member.mode & 0o777)


def _extract_portable_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=True)
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            relative = _safe_member_path(member.filename.rstrip("/"))
            target = destination.joinpath(*relative.parts)
            resolved = target.resolve(strict=False)
            if root != resolved and root not in resolved.parents:
                raise PortableToolError(f"archive path escapes extraction root: {member.filename}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PortableToolError(f"portable archive contains a link: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output)
            if mode & 0o111:
                target.chmod(0o755)


def _extract_portable_7z(archive: Path, destination: Path) -> None:
    executable = shutil.which("7z") or shutil.which("7z.exe")
    members: list[str] = []
    if executable is not None:
        listed = _run([executable, "l", "-slt", str(archive)])
        for line in listed.stdout.splitlines():
            if not line.startswith("Path = "):
                continue
            value = line[7:].strip()
            if value and Path(value).name != archive.name:
                _safe_member_path(value.replace("\\", "/"))
                members.append(value)
    else:
        tar = shutil.which("tar")
        if tar is None:
            raise PortableToolError("7z or a libarchive-compatible tar is required for 7z inputs")
        listed = _run([tar, "-tf", str(archive)])
        for value in listed.stdout.splitlines():
            if value.strip():
                _safe_member_path(value.strip().replace("\\", "/").rstrip("/"))
                members.append(value.strip())
    if not members:
        raise PortableToolError(f"7z archive is empty: {archive}")
    destination.mkdir(parents=True, exist_ok=True)
    if executable is not None:
        _run([executable, "x", "-y", f"-o{destination}", str(archive)])
    else:
        _run([tar, "-xf", str(archive), "-C", str(destination)])
    for path in destination.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise PortableToolError(f"7z archive contains a link or special file: {path}")


def extract_portable_archive(archive: Path, destination: Path, archive_format: str) -> None:
    if archive_format in {"tar.gz", "tar.xz"}:
        extract_portable_tar(archive, destination)
    elif archive_format == "zip":
        _extract_portable_zip(archive, destination)
    elif archive_format == "7z":
        _extract_portable_7z(archive, destination)
    else:
        raise PortableToolError(f"unsupported portable archive format: {archive_format}")


def _version(tool_id: str, root: Path) -> str:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import toolchain  # noqa: PLC0415

    manifest = _read_json(MANIFEST_PATH)
    tool = next(item for item in manifest["tools"] if item["tool_id"] == tool_id)
    paths = {name: root / "bin" / name for name in tool["executables"]}
    completed = _run(toolchain._version_command(tool, paths), timeout=60)
    recognized, version = toolchain._probe_version(tool_id, completed.stdout + "\n" + completed.stderr)
    if not recognized:
        raise PortableToolError(f"portable {tool_id} version output is not recognizable")
    return version


def build(tool_id: str, architecture: str, output_dir: Path) -> dict[str, Any]:
    machine = _native_architecture(architecture)
    lock = _read_json(SOURCE_LOCK_PATH)
    _configure_snapshot(lock)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"archive-{tool_id}-{architecture}-") as temporary:
        work = Path(temporary)
        root = work / "package"
        packages: set[str] = set()
        if tool_id == "assfonts":
            definition = lock["assfonts"]
            _install(list(definition["runtime_packages"]))
            upstream = work / "assfonts-upstream.tar.gz"
            source = definition["sources"][architecture]
            _download(str(source["url"]), str(source["sha256"]), upstream)
            binary = _extract_assfonts(upstream, work)
            _stage_binary(binary, "assfonts", root, packages, assfonts=True)
            _fontconfig(root)
            notices = work / "upstream-notices"
            if notices.is_dir():
                target = root / "share" / "licenses" / "assfonts"
                target.mkdir(parents=True, exist_ok=True)
                for notice in notices.iterdir():
                    shutil.copy2(notice, target / notice.name)
        else:
            definition = lock["apt_tools"].get(tool_id)
            if not isinstance(definition, dict):
                raise PortableToolError(f"unsupported portable tool: {tool_id}")
            _install(list(definition["packages"]))
            for name in definition["executables"]:
                found = shutil.which(str(name))
                if found is None:
                    raise PortableToolError(f"installed executable is missing: {name}")
                _stage_binary(Path(found), str(name), root, packages)
        package_versions = _copy_licenses(packages, root)
        version = _version(tool_id, root)
        filename = f"{tool_id}-linux-{architecture}.tar.gz"
        artifact = output_dir / filename
        create_deterministic_tar(root, artifact)
        metadata = {
            "schema_version": 1,
            "tool_id": tool_id,
            "version": version,
            "platform": "linux",
            "architecture": architecture,
            "native_machine": machine,
            "native_built": True,
            "filename": filename,
            "sha256": _sha256(artifact),
            "size": artifact.stat().st_size,
            "archive_format": "tar.gz",
            "executable_paths": [f"bin/{name}" for name in definition["executables"]],
            "source_lock": SOURCE_LOCK_PATH.relative_to(PROJECT_ROOT).as_posix(),
            "debian_snapshot": lock["debian_snapshot"],
            "package_versions": package_versions,
        }
        metadata_path = output_dir / f"{filename}.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, choices=("mediainfo", "mkvtoolnix", "ffmpeg", "assfonts"))
    parser.add_argument("--architecture", required=True, choices=tuple(ARCHITECTURES))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.tool, args.architecture, args.output.resolve(strict=False))
    except (OSError, KeyError, ValueError, PortableToolError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "OK", "artifact": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
