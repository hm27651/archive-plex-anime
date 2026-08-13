"""Safe cumulative subtitle ZIP creation, merging, and verification."""

from __future__ import annotations

import copy
import os
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

from archive_rules import numbered_output_path, resolve_path, temporary_path
from internal.errors import WorkflowError
from internal.signatures import canonical_metadata_digest, file_signature


ZIP_METADATA_ENCODING = "gb18030"


def safe_zip_entry_name(value: str) -> str:
    raw = str(value).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise WorkflowError("ZIP_ENTRY_INVALID", f"Unsafe ZIP entry: {value}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkflowError("ZIP_ENTRY_INVALID", f"Unsafe ZIP entry: {value}")
    return "/".join(parts)


def zip_entry_key(value: str) -> str:
    return unicodedata.normalize("NFKC", safe_zip_entry_name(value)).casefold()


def zip_entry_sort_key(value: str) -> tuple[Any, ...]:
    normalized = unicodedata.normalize("NFKC", safe_zip_entry_name(value)).casefold()
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", normalized)
        if part
    )


def _zip_file_records(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = safe_zip_entry_name(info.filename)
        key = zip_entry_key(name)
        if key in seen:
            raise WorkflowError("ZIP_ENTRY_DUPLICATE", f"ZIP contains duplicate normalized entry: {name}")
        seen.add(key)
        records.append({"name": name, "key": key, "info": info})
    return records


def zip_inventory_signature(path: Path) -> dict[str, Any]:
    path = resolve_path(path)
    if not path.exists():
        return {"exists": False}
    if not path.is_file():
        raise WorkflowError("ZIP_TARGET_INVALID", f"ZIP target is not a file: {path}")
    try:
        with zipfile.ZipFile(path, "r", metadata_encoding=ZIP_METADATA_ENCODING) as archive:
            records = _zip_file_records(archive)
            inventory = sorted(
                [
                    {
                        "key": item["key"],
                        "crc32": int(item["info"].CRC),
                        "size": int(item["info"].file_size),
                    }
                    for item in records
                ],
                key=lambda item: item["key"],
            )
    except zipfile.BadZipFile as exc:
        raise WorkflowError("ZIP_INVALID", f"Invalid ZIP archive: {path}: {exc}") from exc
    return {
        "exists": True,
        "entries": len(inventory),
        "inventoryDigest": canonical_metadata_digest(inventory),
    }


def _copy_zip_entry(
    source_archive: zipfile.ZipFile,
    source_info: zipfile.ZipInfo,
    output_archive: zipfile.ZipFile,
    arcname: str,
) -> None:
    target_info = copy.copy(source_info)
    target_info.filename = safe_zip_entry_name(arcname)
    target_info.compress_type = zipfile.ZIP_STORED
    target_info.flag_bits &= ~0x1
    with source_archive.open(source_info, "r") as source_stream, output_archive.open(
        target_info, "w", force_zip64=True
    ) as output_stream:
        shutil.copyfileobj(source_stream, output_stream, length=16 * 1024 * 1024)


def merge_zip_archives(base: Path | None, incoming: Path, output: Path) -> dict[str, Any]:
    incoming = resolve_path(incoming)
    output = resolve_path(output)
    base_path = resolve_path(base) if base is not None else None
    if not incoming.is_file():
        raise WorkflowError("ZIP_MISSING", f"Incoming ZIP missing: {incoming}")
    base_signature = zip_inventory_signature(base_path) if base_path is not None else {"exists": False}
    incoming_signature = zip_inventory_signature(incoming)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(incoming, "r", metadata_encoding=ZIP_METADATA_ENCODING) as incoming_archive:
            incoming_records = _zip_file_records(incoming_archive)
            incoming_by_key = {item["key"]: item for item in incoming_records}
            if base_signature.get("exists"):
                assert base_path is not None
                with zipfile.ZipFile(base_path, "r", metadata_encoding=ZIP_METADATA_ENCODING) as base_archive:
                    base_records = _zip_file_records(base_archive)
                    base_by_key = {item["key"]: item for item in base_records}
                    selected = {
                        key: (base_archive, item)
                        for key, item in base_by_key.items()
                        if key not in incoming_by_key
                    }
                    selected.update({key: (incoming_archive, item) for key, item in incoming_by_key.items()})
                    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as merged:
                        for _, (source_archive, item) in sorted(
                            selected.items(), key=lambda pair: zip_entry_sort_key(pair[1][1]["name"])
                        ):
                            _copy_zip_entry(source_archive, item["info"], merged, item["name"])
                base_count = len(base_records)
                collisions = len(set(base_by_key) & set(incoming_by_key))
            else:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as merged:
                    for item in sorted(incoming_records, key=lambda value: zip_entry_sort_key(value["name"])):
                        _copy_zip_entry(incoming_archive, item["info"], merged, item["name"])
                base_count = 0
                collisions = 0
    except (OSError, zipfile.BadZipFile) as exc:
        output.unlink(missing_ok=True)
        if isinstance(exc, zipfile.BadZipFile):
            raise WorkflowError("ZIP_CRC_FAILED", f"ZIP merge failed CRC validation: {exc}") from exc
        raise
    result_signature = zip_inventory_signature(output)
    return {
        "baseSignature": base_signature,
        "incomingSignature": incoming_signature,
        "resultSignature": result_signature,
        "baseCount": base_count,
        "incomingCount": len(incoming_records),
        "collisions": collisions,
        "resultCount": int(result_signature.get("entries", 0)),
    }


def verify_zip(path: Path, expected: list[str] | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError("ZIP_MISSING", f"ZIP missing: {path}")
    with zipfile.ZipFile(path, "r", metadata_encoding=ZIP_METADATA_ENCODING) as archive:
        names = [safe_zip_entry_name(info.filename) for info in archive.infolist() if not info.is_dir()]
        _zip_file_records(archive)
        bad = archive.testzip()
    if bad:
        raise WorkflowError("ZIP_CRC_FAILED", f"ZIP CRC failed at {bad}: {path}")
    unrenamed = [name for name in names if name.casefold().endswith(".assfonts.ass")]
    if unrenamed:
        raise WorkflowError("ZIP_UNRENAMED_SUBTITLE", f"ZIP contains unrenamed subtitle outputs: {unrenamed}")
    if expected is not None and names != expected:
        raise WorkflowError("ZIP_INVENTORY_MISMATCH", f"ZIP entries differ: expected={expected}, actual={names}")
    return {"file": file_signature(path), "entries": names, "crc": "OK"}


def build_package(
    work: Path,
    package: dict[str, Any],
    final_zip_jobs: list[dict[str, Any]],
    *,
    direct_output: bool = False,
    defer_output_validation: bool = False,
    verifier: Any = None,
) -> dict[str, Any]:
    """Build one cumulative subtitle ZIP from a confirmed package plan."""
    verifier = verifier or verify_zip
    work = resolve_path(work)
    planned_output = resolve_path(package["output"])
    output = planned_output
    if direct_output and output.exists():
        output = numbered_output_path(output)
        package["output"] = str(output)
    entries = package.get("entries", [])
    if direct_output:
        for final_job in final_zip_jobs:
            if os.path.normcase(str(final_job.get("source", ""))) == os.path.normcase(str(planned_output)):
                final_job["source"] = str(output)
    incoming_temp = temporary_path(work, "package", f"{output.name}.incoming.tmp")
    temporary = temporary_path(work, "package", f"{output.name}.merged.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    incoming_temp.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    merge_policy = str(package.get("mergePolicy") or "preserve-existing-new-wins")
    if merge_policy != "preserve-existing-new-wins":
        raise WorkflowError("ZIP_MERGE_POLICY_INVALID", f"Unsupported ZIP merge policy: {merge_policy}")
    incoming_zip_value = str(package.get("incomingZip") or "").strip()
    if incoming_zip_value and entries:
        raise WorkflowError("ZIP_PACKAGE_INPUT_CONFLICT", "Package cannot use both incomingZip and loose entries")
    normalized_entries: list[tuple[str, Path]] = []
    seen_entry_keys: set[str] = set()
    for entry in entries:
        source = resolve_path(entry["source"])
        if not source.is_file():
            raise WorkflowError("ZIP_SOURCE_MISSING", f"ZIP source missing: {source}")
        arcname = safe_zip_entry_name(str(entry["arcname"]))
        key = zip_entry_key(arcname)
        if key in seen_entry_keys:
            raise WorkflowError("ZIP_ENTRY_DUPLICATE", f"Package contains duplicate normalized entry: {arcname}")
        seen_entry_keys.add(key)
        normalized_entries.append((arcname, source))
    if incoming_zip_value:
        incoming = resolve_path(incoming_zip_value)
        if not incoming.is_file():
            raise WorkflowError("ZIP_SOURCE_MISSING", f"Incoming ZIP missing: {incoming}")
        remove_incoming = False
    else:
        incoming = incoming_temp
        remove_incoming = True
        with zipfile.ZipFile(incoming, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for arcname, source in sorted(normalized_entries, key=lambda item: zip_entry_sort_key(item[0])):
                archive.write(source, arcname)
    merge_base_value = str(package.get("mergeBase") or "").strip()
    merge_base = resolve_path(merge_base_value) if merge_base_value else None
    try:
        merge_result = merge_zip_archives(merge_base, incoming, temporary)
        os.replace(temporary, output)
    finally:
        if remove_incoming:
            incoming.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(output, "r", metadata_encoding=ZIP_METADATA_ENCODING) as archive:
        expected_entries = [safe_zip_entry_name(info.filename) for info in archive.infolist() if not info.is_dir()]
    if defer_output_validation:
        result = {"file": file_signature(output), "entries": expected_entries, "crc": "DEFERRED"}
    else:
        result = verifier(output, expected_entries)
    result["merge"] = merge_result
    result["mergeBase"] = str(merge_base) if merge_base is not None else ""
    return result
