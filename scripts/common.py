"""Shared runtime for the archive workflow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from archive_rules import (
    TEXT_ENCODINGS,
    backend_cache_path,
    state_is_current,
    state_path,
)


class WorkflowIssue(RuntimeError):
    def __init__(
        self,
        status: str,
        message: str,
        *,
        code: str = "",
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = normalize_status(status)
        self.code = code
        self.details = details or {}
        self.retryable = retryable


def configure_utf8_stdio() -> None:
    """Make public CLI JSON deterministic when Python stdio is redirected on Windows."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, OSError, ValueError):
            continue


def normalize_status(status: str) -> str:
    return "NEEDS_USER" if str(status).upper() == "DECISION_REQUIRED" else str(status).upper()


def decode_output(data: bytes) -> str:
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError("subprocess output is not UTF-8, UTF-8-SIG, or GB18030")


def read_text(path: Path) -> str:
    data = path.read_bytes()
    return decode_output(data)


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_state(work_dir: Path) -> dict[str, Any]:
    path = state_path(work_dir)
    if not path.is_file():
        return {}
    state = read_json(path)
    if not state_is_current(state):
        raise WorkflowIssue("FAILED", "task state contract mismatch")
    return state


def save_state(work_dir: Path, state: dict[str, Any]) -> None:
    if not state_is_current(state):
        raise WorkflowIssue("FAILED", "task state contract mismatch")
    write_json_atomic(state_path(work_dir), state)


def config_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "archive-plex-anime" / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected = (path or config_path()).resolve(strict=False)
    config = read_json(selected)
    if not isinstance(config, dict):
        raise RuntimeError(f"configuration root must be an object: {selected}")
    config["_path"] = str(selected)
    return config


def python_executable(config: dict[str, Any]) -> str:
    return str(config.get("tools", {}).get("python") or sys.executable)


def run_process(
    arguments: list[str],
    *,
    stdin: bytes | None = None,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if progress is None:
        completed = subprocess.run(arguments, input=stdin, capture_output=True, check=False)
        return {
            "code": completed.returncode,
            "stdout": decode_output(completed.stdout),
            "stderr": decode_output(completed.stderr),
        }
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            process.stdin.close()
        while True:
            if progress is not None:
                try:
                    progress()
                except Exception:
                    progress = None
            try:
                process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if progress is not None:
            try:
                progress()
            except Exception:
                pass
        stdout.seek(0)
        stderr.seek(0)
        completed = {
            "code": process.returncode,
            "stdout": decode_output(stdout.read()),
            "stderr": decode_output(stderr.read()),
        }
    return completed


def result(status: str, summary: str, warnings: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"status": normalize_status(status), "summary": summary, "warnings": warnings or [], **extra}


def backend_command(
    work_dir: Path,
    command: str,
    extra: list[str],
    *,
    execute: bool = False,
    approved_plan: str = "",
    stdin_json: Any | None = None,
    progress: Callable[[], None] | None = None,
    allow_needs_user: bool = False,
) -> dict[str, Any]:
    config = load_config()
    backend = Path(__file__).resolve().parent / "internal" / "archive_backend.py"
    arguments = [python_executable(config), str(backend), command, "--manifest", str(backend_cache_path(work_dir))]
    if execute:
        arguments.append("--execute")
        arguments.extend(["--approved-plan", approved_plan or "confirmed by archive workflow"])
    arguments.extend(extra)
    stdin = json.dumps(stdin_json, ensure_ascii=False).encode("utf-8") if stdin_json is not None else None
    completed = run_process(arguments, stdin=stdin, progress=progress)
    if completed["code"] != 0:
        raw = completed["stderr"] or completed["stdout"]
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        status = normalize_status(str(payload.get("status") or "FAILED"))
        try:
            state = load_state(work_dir)
        except WorkflowIssue:
            state = {}
        if status == "NEEDS_USER" and state.get("approvals", {}).get("preflight"):
            status = "FAILED"
        if allow_needs_user and status == "NEEDS_USER":
            return payload
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        raise WorkflowIssue(
            status,
            str(payload.get("error") or raw or f"backend step failed: {command}"),
            code=str(payload.get("code") or ""),
            details=details,
            retryable=bool(payload.get("retryable", False)),
        )
    try:
        return json.loads(completed["stdout"])
    except json.JSONDecodeError:
        return {"status": "COMPLETE", "output": completed["stdout"]}
