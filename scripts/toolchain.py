"""Deterministic tool manifest, path checks, and entrypoint projections."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from common import config_path, read_json, write_json_atomic


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "toolchain" / "manifest.json"
SCHEMA_PATH = PROJECT_ROOT / "toolchain" / "manifest.schema.json"
ENTRYPOINTS = ("cli", "skill", "hub")
HUB_TOOL_IDS = ("mediainfo", "mkvtoolnix", "ffmpeg", "assfonts")
SUPPORTED_PLATFORMS = {
    ("windows", "x64"),
    ("linux", "amd64"),
    ("linux", "arm64"),
}


class ToolchainError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validate_artifact_path(tool_id: str, executable: str, raw_path: str, platform_name: str) -> None:
    path = PurePosixPath(raw_path)
    segments = raw_path.split("/")
    if (
        not raw_path
        or "\\" in raw_path
        or re.match(r"^[A-Za-z]:", raw_path) is not None
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in segments)
    ):
        raise ToolchainError("MANIFEST_ARTIFACT_PATH_UNSAFE", f"unsafe executable path for {tool_id}: {raw_path}")
    expected = f"{executable}.exe" if platform_name == "windows" else executable
    matches = path.name.casefold() == expected.casefold() if platform_name == "windows" else path.name == expected
    if not matches:
        raise ToolchainError(
            "MANIFEST_ARTIFACT_EXECUTABLE_MISMATCH",
            f"artifact executable for {tool_id} must end with {expected}: {raw_path}",
        )


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _schema_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ToolchainError("MANIFEST_SCHEMA_UNSUPPORTED", f"unsupported schema reference: {reference}")
    current: Any = root
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"schema reference not found: {reference}")
        current = current[key]
    if not isinstance(current, dict):
        raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"schema reference is not an object: {reference}")
    return current


def _matches_type(value: Any, expected: str) -> bool:
    checks: dict[str, Callable[[Any], bool]] = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return expected in checks and checks[expected](value)


def _validate_json_schema(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> None:
    if "$ref" in schema:
        _validate_json_schema(value, _schema_ref(root, str(schema["$ref"])), root, path)
        return
    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, str(item)) for item in expected_types):
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} has invalid type")
    if "const" in schema and value != schema["const"]:
        raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} has unsupported value: {value!r}")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} is empty")
        pattern = schema.get("pattern")
        if pattern and re.search(str(pattern), value) is None:
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} does not match its schema pattern")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        if value < int(schema["minimum"]):
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} is below its minimum")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} has too few items")
        if schema.get("uniqueItems") and len({_json_key(item) for item in value}) != len(value):
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} contains duplicate items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, root, f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} is missing fields: {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ToolchainError("MANIFEST_SCHEMA_INVALID", f"{path} has unsupported fields: {extras}")
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_json_schema(child, child_schema, root, f"{path}.{key}")


def validate_manifest(manifest: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
    selected_schema = schema if schema is not None else read_json(SCHEMA_PATH)
    if not isinstance(manifest, dict) or not isinstance(selected_schema, dict):
        raise ToolchainError("MANIFEST_SCHEMA_INVALID", "manifest and schema roots must be objects")
    _validate_json_schema(manifest, selected_schema, selected_schema)

    tool_ids: set[str] = set()
    artifact_keys: set[tuple[str, str, str]] = set()
    hub_targets: dict[str, set[tuple[str, str]]] = {}
    for tool in manifest["tools"]:
        tool_id = str(tool["tool_id"])
        if tool_id in tool_ids:
            raise ToolchainError("MANIFEST_TOOL_DUPLICATE", f"duplicate tool_id: {tool_id}")
        tool_ids.add(tool_id)
        capability_ids: set[str] = set()
        for capability in tool["capability_checks"]:
            capability_id = str(capability["capability_id"])
            if capability_id in capability_ids:
                raise ToolchainError(
                    "MANIFEST_CAPABILITY_DUPLICATE", f"duplicate capability_id for {tool_id}: {capability_id}"
                )
            capability_ids.add(capability_id)
        for artifact in tool["artifacts"]:
            key = (tool_id, str(artifact["platform"]), str(artifact["architecture"]))
            if key in artifact_keys:
                raise ToolchainError("MANIFEST_ARTIFACT_DUPLICATE", f"duplicate artifact target: {key}")
            artifact_keys.add(key)
            target = (key[1], key[2])
            if target not in SUPPORTED_PLATFORMS:
                raise ToolchainError("MANIFEST_ARTIFACT_TARGET_UNSUPPORTED", f"unsupported artifact target: {key}")
            paths = artifact["executable_paths"]
            executables = tool["executables"]
            if len(paths) != len(executables):
                raise ToolchainError(
                    "MANIFEST_ARTIFACT_EXECUTABLE_COUNT",
                    f"artifact executable count does not match {tool_id}: {key[1:]}",
                )
            for executable, raw_path in zip(executables, paths, strict=True):
                _validate_artifact_path(tool_id, str(executable), str(raw_path), str(artifact["platform"]))
            if tool_id in HUB_TOOL_IDS:
                hub_targets.setdefault(tool_id, set()).add(target)

    actual_hub_ids = tuple(tool["tool_id"] for tool in manifest["tools"] if "hub" in tool["entrypoints"])
    if actual_hub_ids != HUB_TOOL_IDS:
        raise ToolchainError("MANIFEST_HUB_SCOPE_INVALID", f"Hub tools must be exactly: {', '.join(HUB_TOOL_IDS)}")
    for tool_id in HUB_TOOL_IDS:
        missing = sorted(SUPPORTED_PLATFORMS - hub_targets.get(tool_id, set()))
        if missing:
            raise ToolchainError(
                "MANIFEST_HUB_ARTIFACT_MISSING", f"missing Hub artifact targets for {tool_id}: {missing}"
            )
    return manifest


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    selected = (path or MANIFEST_PATH).resolve(strict=False)
    try:
        manifest = read_json(selected)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolchainError("MANIFEST_LOAD_FAILED", f"cannot load tool manifest: {selected}") from exc
    return validate_manifest(manifest)


def visible_tools(manifest: dict[str, Any], entrypoint: str) -> list[dict[str, Any]]:
    if entrypoint not in ENTRYPOINTS:
        raise ToolchainError("ENTRYPOINT_UNKNOWN", f"unsupported entrypoint: {entrypoint}")
    return [copy.deepcopy(tool) for tool in manifest["tools"] if entrypoint in tool["entrypoints"]]


def select_artifact(
    tool: dict[str, Any], platform_name: str | None = None, architecture: str | None = None
) -> dict[str, Any] | None:
    system, machine = current_platform()
    selected_system = platform_name or system
    selected_machine = architecture or machine
    return next(
        (
            copy.deepcopy(artifact)
            for artifact in tool["artifacts"]
            if artifact["platform"] == selected_system and artifact["architecture"] == selected_machine
        ),
        None,
    )


def current_platform() -> tuple[str, str]:
    system = "windows" if os.name == "nt" else "linux" if platform.system().casefold() == "linux" else platform.system().casefold()
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64"}:
        architecture = "x64" if system == "windows" else "amd64"
    elif machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    else:
        architecture = machine or "unknown"
    return system, architecture


def _executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _optional_config(path: Path | None = None) -> tuple[dict[str, Any], Path]:
    selected = (path or config_path()).resolve(strict=False)
    if not selected.is_file():
        return {}, selected
    try:
        value = read_json(selected)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolchainError("CONFIG_INVALID", f"configuration is not valid JSON: {selected}") from exc
    if not isinstance(value, dict):
        raise ToolchainError("CONFIG_INVALID", f"configuration root must be an object: {selected}")
    return value, selected


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _paths_from_base(tool: dict[str, Any], selected: Path) -> dict[str, Path]:
    if tool["path_kind"] == "file":
        return {tool["executables"][0]: selected.resolve(strict=False)}
    return {name: (selected / _executable_name(name)).resolve(strict=False) for name in tool["executables"]}


def resolve_configured_paths(
    tool: dict[str, Any], config: dict[str, Any] | None = None, candidate: Path | None = None
) -> tuple[str, dict[str, Path]]:
    if candidate is not None:
        return "candidate", _paths_from_base(tool, candidate)
    tools = config.get("tools", {}) if isinstance(config, dict) and isinstance(config.get("tools"), dict) else {}
    configured = str(tools.get(tool["path_setting"]) or "").strip()
    if configured:
        return "configured", _paths_from_base(tool, Path(configured).expanduser())
    individual = {name: str(tools.get(name) or "").strip() for name in tool["executables"]}
    if any(individual.values()):
        first = next(Path(value).expanduser() for value in individual.values() if value)
        paths = {
            name: (Path(value).expanduser() if value else first.with_name(_executable_name(name))).resolve(strict=False)
            for name, value in individual.items()
        }
        return "configured", paths
    found: dict[str, Path] = {}
    for name in tool["executables"]:
        executable = shutil.which(name)
        if not executable:
            return "not_configured", {}
        found[name] = Path(executable).resolve()
    return "path", found


def _path_ready(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _decode(value: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _run_command(arguments: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(arguments, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "stdout": _decode(completed.stdout),
        "stderr": _decode(completed.stderr),
    }


def _run_json(runner: Callable[[list[str]], dict[str, Any]], arguments: list[str]) -> tuple[bool, dict[str, Any] | None]:
    completed = runner(arguments)
    if not completed.get("ok"):
        return False, None
    try:
        value = json.loads(str(completed.get("stdout") or ""))
    except json.JSONDecodeError:
        return False, None
    return isinstance(value, dict), value if isinstance(value, dict) else None


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(b"\x00\x00" * 800)


def _font_for_check() -> tuple[Path, str] | None:
    candidates = (
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf", "Arial"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), "DejaVu Sans"),
        (Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"), "Liberation Sans"),
    )
    return next(((path, family) for path, family in candidates if path.is_file()), None)


def _write_ass(path: Path, family: str) -> None:
    path.write_text(
        "\n".join(
            (
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 640",
                "PlayResY: 360",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
                "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                f"Style: Default,{family},32,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,"
                "1,2,0,2,10,10,10,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,工具检查",
            )
        ),
        encoding="utf-8-sig",
    )


def _version_command(tool: dict[str, Any], paths: dict[str, Path]) -> list[str]:
    tool_id = tool["tool_id"]
    if tool_id == "mediainfo":
        return [str(paths["mediainfo"]), "--Version"]
    if tool_id == "mkvtoolnix":
        return [str(paths["mkvmerge"]), "--version"]
    if tool_id == "ffmpeg":
        return [str(paths["ffmpeg"]), "-version"]
    if tool_id == "kdocs-cli":
        return [str(paths["kdocs-cli"]), "--version"]
    return [str(paths[tool["executables"][0]]), "--help"]


def _probe_version(tool_id: str, output: str) -> tuple[bool, str]:
    if tool_id == "otf2ttf":
        recognized = re.search(r"(?im)^\s*usage:\s*otf2ttf(?:\.py)?(?:\s|$)", output) is not None
        return recognized, ""
    patterns = {
        "mediainfo": r"(?im)MediaInfo(?:Lib)?[^\r\n]*?v?(\d+(?:\.\d+)+)",
        "mkvtoolnix": r"(?im)^\s*mkvmerge\s+v([0-9]+(?:\.[0-9]+)*(?:[-+._][A-Za-z0-9.-]+)?)\b",
        "ffmpeg": r"(?im)^\s*ffmpeg\s+version\s+([A-Za-z0-9][^\s]*)\b",
        "assfonts": r"(?im)^\s*assfonts\s+v?(\d+(?:\.\d+)+(?:[-+._][A-Za-z0-9.-]+)?)\s*$",
        "kdocs-cli": r"^\s*(\d+(?:\.\d+){1,3})\s*$",
    }
    pattern = patterns.get(tool_id)
    if pattern is None:
        return False, ""
    matched = re.search(pattern, output)
    return (True, matched.group(1)[:80]) if matched else (False, "")


def _version_from_output(tool_id: str, output: str) -> str:
    return _probe_version(tool_id, output)[1]


def _capability_results(
    tool: dict[str, Any], paths: dict[str, Path], runner: Callable[[list[str]], dict[str, Any]]
) -> tuple[list[dict[str, str]], str]:
    labels = {item["capability_id"]: item["label"] for item in tool["capability_checks"]}
    results: dict[str, dict[str, str]] = {}

    def record(capability_id: str, success: bool, detail: str) -> None:
        results[capability_id] = {
            "capability_id": capability_id,
            "label": labels[capability_id],
            "status": "ready" if success else "capability_failed",
            "reason": "" if success else detail,
        }

    version = runner(_version_command(tool, paths))
    version_id = "version" if "version" in labels else None
    version_output = (str(version.get("stdout") or "") + "\n" + str(version.get("stderr") or "")).strip()
    version_recognized, installed_version = _probe_version(tool["tool_id"], version_output)
    if version_id:
        if not version.get("ok"):
            record(version_id, False, "version command failed")
        else:
            record(version_id, version_recognized, "version output is not recognizable")

    with tempfile.TemporaryDirectory(prefix="archive-tool-check-") as temporary:
        root = Path(temporary)
        wav = root / "中文音轨.wav"
        _write_wav(wav)
        tool_id = tool["tool_id"]
        if tool_id == "mediainfo":
            success, value = _run_json(runner, [str(paths["mediainfo"]), "--Output=JSON", "--", str(wav)])
            tracks = ((value or {}).get("media") or {}).get("track") or []
            record(
                "media_json",
                success and any(item.get("@type") == "Audio" for item in tracks if isinstance(item, dict)),
                "MediaInfo did not return readable audio JSON",
            )
        elif tool_id == "ffmpeg":
            decoded = runner(
                [str(paths["ffmpeg"]), "-v", "error", "-i", str(wav), "-map", "0:a:0", "-f", "null", "-"]
            )
            record("pcm_decode", bool(decoded.get("ok")), "FFmpeg PCM decode failed")
            success, value = _run_json(
                runner,
                [str(paths["ffprobe"]), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(wav)],
            )
            streams = (value or {}).get("streams") or []
            record(
                "probe",
                success and any(item.get("codec_type") == "audio" for item in streams if isinstance(item, dict)),
                "FFprobe did not return readable audio JSON",
            )
        elif tool_id == "mkvtoolnix":
            media = root / "中文封装.mka"
            attachment = root / "附件.txt"
            attachment.write_text("attachment check", encoding="utf-8")
            muxed = runner(
                [
                    str(paths["mkvmerge"]),
                    "-o",
                    str(media),
                    "--attachment-mime-type",
                    "text/plain",
                    "--attach-file",
                    str(attachment),
                    str(wav),
                ]
            )
            media_ready = bool(muxed.get("ok")) and media.is_file()
            identified_ok, identified = (
                _run_json(runner, [str(paths["mkvmerge"]), "-J", str(media)]) if media_ready else (False, None)
            )
            record(
                "identify",
                identified_ok and bool((identified or {}).get("tracks")),
                "mkvmerge could not identify the generated MKV",
            )
            inspected = runner([str(paths["mkvinfo"]), str(media)]) if media_ready else {"ok": False}
            record("inspect", bool(inspected.get("ok")), "mkvinfo could not inspect the generated MKV")
            attachments = (identified or {}).get("attachments") or []
            attachment_id = next(
                (item.get("id") for item in attachments if isinstance(item, dict) and item.get("id") is not None),
                None,
            )
            extracted = root / "提取附件.txt"
            extraction = (
                runner([str(paths["mkvextract"]), "attachments", str(media), f"{attachment_id}:{extracted}"])
                if media_ready and attachment_id is not None
                else {"ok": False}
            )
            record(
                "extract",
                bool(extraction.get("ok")) and extracted.is_file() and extracted.stat().st_size > 0,
                "mkvextract could not extract the generated attachment",
            )
        elif tool_id == "assfonts":
            font = _font_for_check()
            if font is None:
                record("subtitle_subset", False, "no controlled system font is available")
            else:
                font_path, family = font
                fonts = root / "字体"
                database = root / "字体数据库"
                fonts.mkdir()
                database.mkdir()
                shutil.copy2(font_path, fonts / font_path.name)
                built = runner([str(paths["assfonts"]), "-f", str(fonts), "-b", "-d", str(database)])
                subtitle = root / "中文字幕.ass"
                _write_ass(subtitle, family)
                subset = (
                    runner([str(paths["assfonts"]), "-d", str(database), "-i", str(subtitle)])
                    if built.get("ok")
                    else {"ok": False, "stdout": "", "stderr": ""}
                )
                subset_output = str(subset.get("stdout") or "") + str(subset.get("stderr") or "")
                output = subtitle.with_suffix(".assfonts.ass")
                record(
                    "subtitle_subset",
                    bool(subset.get("ok"))
                    and "[ERROR]" not in subset_output
                    and output.is_file()
                    and output.stat().st_size > 0,
                    "assfonts did not produce a readable subset subtitle",
                )

    missing_contracts = [capability_id for capability_id in labels if capability_id not in results]
    for capability_id in missing_contracts:
        record(capability_id, False, "capability contract is not implemented")
    ordered = [results[item["capability_id"]] for item in tool["capability_checks"]]
    return ordered, installed_version


def check_capability(
    tool: dict[str, Any],
    capability_id: str,
    paths: dict[str, Path],
    runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, str]:
    results, _version = _capability_results(tool, paths, runner or _run_command)
    selected = next((item for item in results if item["capability_id"] == capability_id), None)
    if selected is None:
        raise ToolchainError("CAPABILITY_UNKNOWN", f"unknown capability: {capability_id}")
    return selected


def _static_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_id": tool["tool_id"],
        "name": tool["name"],
        "purpose": tool["purpose"],
        "project_url": tool["project_url"],
        "download_page_url": tool["download_page_url"],
        "binary_source_url": tool["binary_source_url"],
        "license": tool["license"],
        "capability_checks": copy.deepcopy(tool["capability_checks"]),
        "artifacts": copy.deepcopy(tool["artifacts"]),
        "managed_install_supported": False,
    }


def check_tool(
    tool: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    candidate: Path | None = None,
    run_capabilities: bool = True,
    runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    system, architecture = current_platform()
    base = {**_static_tool(tool), "platform": system, "architecture": architecture}
    if (system, architecture) not in SUPPORTED_PLATFORMS:
        return {
            **base,
            "status": "unsupported_platform",
            "source": "none",
            "paths": {},
            "installed_version": "",
            "capabilities": [],
            "reason": f"unsupported platform: {system}-{architecture}",
        }
    source, paths = resolve_configured_paths(tool, config, candidate)
    if not paths:
        return {
            **base,
            "status": "not_configured",
            "source": source,
            "paths": {},
            "installed_version": "",
            "capabilities": [
                {**item, "status": "not_configured", "reason": "tool path is not configured"}
                for item in tool["capability_checks"]
            ],
            "reason": "tool path is not configured and the executable was not found on PATH",
        }
    missing = [name for name, path in paths.items() if not _path_ready(path)]
    if missing:
        return {
            **base,
            "status": "missing",
            "source": source,
            "paths": {name: str(path) for name, path in paths.items()},
            "installed_version": "",
            "capabilities": [
                {**item, "status": "missing", "reason": "required executable is missing"}
                for item in tool["capability_checks"]
            ],
            "reason": f"missing executables: {', '.join(missing)}",
        }
    selected_runner = runner or _run_command
    if not run_capabilities:
        version = selected_runner(_version_command(tool, paths))
        version_output = (str(version.get("stdout") or "") + "\n" + str(version.get("stderr") or "")).strip()
        return {
            **base,
            "status": "needs_recheck",
            "source": source,
            "paths": {name: str(path) for name, path in paths.items()},
            "installed_version": _version_from_output(tool["tool_id"], version_output),
            "capabilities": [
                {**item, "status": "needs_recheck", "reason": "full capability check has not run"}
                for item in tool["capability_checks"]
            ],
            "reason": "paths are present; run tools check to verify capabilities",
        }
    capabilities, installed_version = _capability_results(tool, paths, selected_runner)
    ready = all(item["status"] == "ready" for item in capabilities)
    return {
        **base,
        "status": "ready" if ready else "capability_failed",
        "source": source,
        "paths": {name: str(path) for name, path in paths.items()},
        "installed_version": installed_version,
        "capabilities": capabilities,
        "reason": "" if ready else "one or more minimum capability checks failed",
    }


def _tool_by_id(manifest: dict[str, Any], tool_id: str, entrypoint: str) -> dict[str, Any]:
    selected = next((tool for tool in visible_tools(manifest, entrypoint) if tool["tool_id"] == tool_id), None)
    if selected is None:
        raise ToolchainError("TOOL_UNKNOWN", f"tool is not available for {entrypoint}: {tool_id}")
    return selected


def list_tools(entrypoint: str, config_file: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    config, _selected = _optional_config(config_file)
    system, architecture = current_platform()
    return {
        "status": "OK",
        "schema_version": manifest["schema_version"],
        "manifest_version": manifest["manifest_version"],
        "entrypoint": entrypoint,
        "platform": {"system": system, "architecture": architecture},
        "tools": [check_tool(tool, config=config, run_capabilities=False) for tool in visible_tools(manifest, entrypoint)],
    }


def check_tools(entrypoint: str, tool_ids: list[str] | None = None, config_file: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    config, _selected = _optional_config(config_file)
    selected = visible_tools(manifest, entrypoint)
    if tool_ids:
        requested = list(dict.fromkeys(tool_ids))
        selected = [_tool_by_id(manifest, tool_id, entrypoint) for tool_id in requested]
    system, architecture = current_platform()
    return {
        "status": "OK",
        "schema_version": manifest["schema_version"],
        "manifest_version": manifest["manifest_version"],
        "entrypoint": entrypoint,
        "platform": {"system": system, "architecture": architecture},
        "tools": [check_tool(tool, config=config) for tool in selected],
    }


def update_tool_path(tool_id: str, selected_path: Path, config_file: Path | None = None) -> dict[str, Any]:
    if not selected_path.is_absolute():
        raise ToolchainError("ABSOLUTE_PATH_REQUIRED", "tool path must be absolute")
    manifest = load_manifest()
    tool = _tool_by_id(manifest, tool_id, "cli")
    normalized = selected_path.resolve(strict=False)
    if tool["path_kind"] == "file" and not normalized.is_file():
        raise ToolchainError("TOOL_PATH_INVALID", f"tool file does not exist: {normalized}")
    if tool["path_kind"] == "directory" and not normalized.is_dir():
        raise ToolchainError("TOOL_PATH_INVALID", f"tool directory does not exist: {normalized}")
    config, selected_config = _optional_config(config_file)
    if not selected_config.is_file():
        raise ToolchainError("CONFIG_NOT_FOUND", f"configuration file does not exist: {selected_config}")
    checked = check_tool(tool, config=config, candidate=normalized)
    if checked["status"] != "ready":
        raise ToolchainError("TOOL_CHECK_FAILED", f"{tool_id} capability check failed: {checked['reason']}")

    original_bytes = selected_config.read_bytes()
    updated = copy.deepcopy(config)
    settings = updated.setdefault("tools", {})
    if not isinstance(settings, dict):
        raise ToolchainError("CONFIG_INVALID", "configuration tools field must be an object")
    settings[tool["path_setting"]] = str(normalized)
    resolved = _paths_from_base(tool, normalized)
    for name, path in resolved.items():
        settings[name] = str(path)
    try:
        write_json_atomic(selected_config, updated)
        reread, _path = _optional_config(selected_config)
        saved = check_tool(tool, config=reread)
        if saved["status"] != "ready":
            raise ToolchainError("TOOL_RECHECK_FAILED", f"saved {tool_id} path did not pass recheck")
    except Exception:
        _write_bytes_atomic(selected_config, original_bytes)
        raise
    return {
        "status": "OK",
        "manifest_version": manifest["manifest_version"],
        "tool": saved,
        "config_path": str(selected_config),
    }


def _source_commit() -> str:
    supplied = str(os.environ.get("ARCHIVE_SOURCE_COMMIT") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", supplied):
        return supplied.lower()
    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    value = _decode(completed.stdout).strip()
    return value.lower() if completed.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else "unknown"


def _projection_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tool["tool_id"],
        "name": tool["name"],
        "purpose": tool["purpose"],
        "project_url": tool["project_url"],
        "download_url": tool["download_page_url"],
        "binary_source_url": tool["binary_source_url"],
        "license": tool["license"],
        "path_kind": tool["path_kind"],
        "path_setting": tool["path_setting"],
        "executables": copy.deepcopy(tool["executables"]),
        "capabilities": [
            {"id": item["capability_id"], "label": item["label"]} for item in tool["capability_checks"]
        ],
        "artifacts": copy.deepcopy(tool["artifacts"]),
    }


def export_projection(entrypoint: str, output: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    canonical = _json_key(manifest).encode("utf-8")
    projection = {
        "schema_version": manifest["generated_contract_version"],
        "manifest_version": manifest["manifest_version"],
        "source_version": hashlib.sha256(canonical).hexdigest(),
        "source_commit": _source_commit(),
        "entrypoint": entrypoint,
        "tools": [_projection_tool(tool) for tool in visible_tools(manifest, entrypoint)],
    }
    if output is not None:
        write_json_atomic(output.resolve(strict=False), projection)
    return projection
