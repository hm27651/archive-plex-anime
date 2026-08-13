"""Lightweight file and semantic signatures used by the archive workflow."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from archive_rules import resolve_path


def file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "kind": "file", "size": stat.st_size, "mtimeUtcNs": stat.st_mtime_ns}


def signature_matches(signature: dict[str, Any]) -> bool:
    try:
        current = file_signature(resolve_path(signature["path"]))
    except (FileNotFoundError, OSError, KeyError):
        return False
    recorded_mtime = signature.get("mtimeUtcNs", signature.get("mtimeNs"))
    return current["size"] == signature.get("size") and current["mtimeUtcNs"] == recorded_mtime


def canonical_metadata_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def final_batch_payload(final: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(final)
    payload.pop("batchId", None)
    payload.pop("batchDigest", None)
    return payload


def seal_final_batch(final: dict[str, Any]) -> dict[str, Any]:
    digest = canonical_metadata_digest(final_batch_payload(final))
    final["batchDigest"] = digest
    final["batchId"] = digest[:24]
    return final
