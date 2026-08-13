"""Plan, apply, and verify deterministic Plex tracker column updates."""

from __future__ import annotations

import copy
import ctypes
import functools
import hashlib
import json
import os
import subprocess
from typing import Any

from common import decode_output

COLUMNS = ("Anime1", "Anime2", "Anime3", "Movie1", "Movie2", "Movie3")
STATUS_COLORS = {
    "complete-bdrip": 0xFF00FFFF,
    "incomplete-bdrip": 0xFFB7FFFF,
    "mixed-bdrip": 0xFFB7FFFF,
    "webrip": 0xFFFFFFFF,
    "default": 0xFFFFFFFF,
}
STATUS_ALIASES = {
    "complete bdrip": "complete-bdrip",
    "incomplete bdrip": "incomplete-bdrip",
    "mixed bdrip": "mixed-bdrip",
    "webrip": "webrip",
    "default": "default",
}
MAX_KDOCS_OPERATIONS = 100
TRACKER_INITIAL_ROW_TO = 200
TRACKER_ROW_STEP = 200
TRACKER_MAX_ROW_TO = 5000


class TrackerError(RuntimeError):
    def __init__(self, code: str, message: str, category: str = "FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.category = category


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def snapshot_fingerprint(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "fileId": snapshot["fileId"],
        "worksheetId": int(snapshot["worksheetId"]),
        "range": snapshot["range"],
        "matrix": snapshot["matrix"],
    }


def unwrap_data(value: Any) -> Any:
    current = value
    for _ in range(4):
        if isinstance(current, dict) and "data" in current and len(current) <= 4:
            current = current["data"]
        else:
            break
    return current


def run_kdocs(executable: str, service: str, action: str, payload: dict[str, Any], retries: int = 0) -> Any:
    stdin = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempts = []
    for _ in range(retries + 1):
        completed = subprocess.run(
            [executable, service, action, "--output", "json", "--silent"],
            input=stdin,
            capture_output=True,
            check=False,
        )
        attempts.append(completed)
        if completed.returncode == 0:
            try:
                return unwrap_data(json.loads(completed.stdout.decode("utf-8-sig")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TrackerError("KDOCS_JSON_INVALID", f"Invalid KDocs JSON response: {exc}") from exc
    stderr = decode_output(attempts[-1].stderr)
    raise TrackerError("KDOCS_CALL_FAILED", stderr or f"KDocs {service} {action} failed")


def find_first(value: Any, keys: tuple[str, ...]) -> Any | None:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            result = find_first(child, keys)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_first(child, keys)
            if result is not None:
                return result
    return None


def resolve_file_id(executable: str, tracker_name: str) -> str:
    result = run_kdocs(
        executable,
        "drive",
        "search-files",
        {"keyword": tracker_name, "type": "file_name", "file_type": "file", "page_size": 100, "with_total": True},
    )
    items = find_first(result, ("items", "files", "records", "list"))
    if not isinstance(items, list):
        items = result if isinstance(result, list) else []
    exact = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("file") if isinstance(item.get("file"), dict) else item
        name = info.get("name") or info.get("file_name") or info.get("fname")
        if name == tracker_name:
            file_id = info.get("file_id") or info.get("id") or info.get("fileId")
            if file_id:
                exact.append(str(file_id))
    if len(set(exact)) != 1:
        raise TrackerError("TRACKER_FILE_AMBIGUOUS", f"Expected one exact tracker file named {tracker_name!r}, found {len(set(exact))}", "DECISION_REQUIRED")
    return exact[0]


def resolve_worksheet(executable: str, file_id: str, worksheet_name: str | None = None) -> tuple[int, str]:
    result = run_kdocs(executable, "sheet", "get-sheets-info", {"file_id": file_id})
    sheets = find_first(result, ("sheets", "worksheets", "sheetsInfo", "items", "list"))
    if not isinstance(sheets, list):
        sheets = result if isinstance(result, list) else []
    candidates = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        name = str(sheet.get("name") or sheet.get("sheet_name") or sheet.get("sheetName") or sheet.get("title") or "")
        identifier = sheet.get("id") or sheet.get("worksheet_id") or sheet.get("sheetId")
        if identifier is None:
            continue
        if worksheet_name is None or name == worksheet_name:
            candidates.append((int(identifier), name))
    if len(candidates) != 1:
        raise TrackerError("WORKSHEET_AMBIGUOUS", f"Expected one worksheet, found {len(candidates)}", "DECISION_REQUIRED")
    return candidates[0]


def cell_value(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, (int, float, bool)):
        return str(cell)
    if isinstance(cell, dict):
        for key in ("value", "text", "displayValue", "display_value", "formula", "cellText", "originalCellValue"):
            if key in cell and cell[key] is not None:
                return str(cell[key]).lstrip("=")
    return ""


def cell_xf(cell: Any) -> dict[str, Any]:
    if isinstance(cell, dict):
        value = cell.get("xf") or cell.get("format") or cell.get("style")
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if any(key in cell for key in ("fonts", "alignment", "cell_background_color")):
            fonts = cell.get("fonts") or {}
            alignment = cell.get("alignment") or {}
            background = cell.get("cell_background_color")
            try:
                background_value = int(str(background).lstrip("#"), 16) if background else 0xFFFFFFFF
            except ValueError:
                background_value = 0xFFFFFFFF
            try:
                font_color = int(str(fonts.get("color", "#FF000000")).lstrip("#"), 16)
            except ValueError:
                font_color = 0xFF000000
            horizontal = {"haGeneral": 0, "haCenter": 2, "haLeft": 1, "haRight": 3}.get(alignment.get("horizontal"), 0)
            vertical = {"vaCenter": 1, "vaTop": 0, "vaBottom": 2}.get(alignment.get("vertical"), 1)
            return {
                "font": {
                    "name": fonts.get("font_east_asia", "微软雅黑"),
                    "dyHeight": int(fonts.get("size", 11) or 11) * 20,
                    "color": {"type": 2, "value": font_color},
                },
                "alcH": horizontal,
                "alcV": vertical,
                "wrap": bool(cell.get("wrap", False)),
                "fill": {"type": 1, "back": {"type": 2, "value": background_value}, "fore": {"type": 255, "value": 0, "tint": 0}},
            }
    return {}


def extract_matrix(result: Any, rows: int, cols: int, row_from: int = 0, col_from: int = 0) -> list[list[dict[str, Any]]]:
    result = unwrap_data(result)
    candidate = find_first(result, ("values", "matrix", "cells", "rangeData", "range_data", "rows"))
    if candidate is None:
        candidate = result
    matrix: list[list[Any]] = []
    if isinstance(candidate, list) and candidate and all(isinstance(row, list) for row in candidate):
        matrix = candidate
    elif isinstance(candidate, list) and candidate and all(isinstance(row, dict) for row in candidate):
        flat_cells = [row for row in candidate if "originRow" in row and "originCol" in row]
        if flat_cells:
            matrix = [[None for _ in range(cols)] for _ in range(rows)]
            for item in flat_cells:
                relative_row = int(item["originRow"]) - row_from
                relative_col = int(item["originCol"]) - col_from
                if 0 <= relative_row < rows and 0 <= relative_col < cols:
                    matrix[relative_row][relative_col] = item
        else:
            for row in candidate:
                values = row.get("values") or row.get("cells") or row.get("data")
                if isinstance(values, list):
                    matrix.append(values)
    if not matrix:
        raise TrackerError("KDOCS_RANGE_SHAPE", "Could not extract a cell matrix from KDocs response")
    normalized = []
    for row_index in range(rows):
        source_row = matrix[row_index] if row_index < len(matrix) else []
        normalized.append(
            [
                {"value": cell_value(source_row[col_index] if col_index < len(source_row) else None), "xf": cell_xf(source_row[col_index] if col_index < len(source_row) else None)}
                for col_index in range(cols)
            ]
        )
    return normalized


def read_live_snapshot(
    executable: str,
    file_id: str,
    worksheet_id: int,
    row_from: int,
    row_to: int,
    col_from: int,
    col_to: int,
) -> dict[str, Any]:
    payload = {
        "file_id": file_id,
        "worksheet_id": worksheet_id,
        "range": {"rowFrom": row_from, "rowTo": row_to, "colFrom": col_from, "colTo": col_to},
    }
    result = run_kdocs(executable, "sheet", "get-range-data", payload, retries=1)
    matrix = extract_matrix(result, row_to - row_from + 1, col_to - col_from + 1, row_from, col_from)
    return {
        "fileId": file_id,
        "worksheetId": worksheet_id,
        "range": {"rowFrom": row_from, "rowTo": row_to, "colFrom": col_from, "colTo": col_to},
        "matrix": matrix,
    }


def snapshot_needs_expansion(snapshot: dict[str, Any]) -> bool:
    matrix = snapshot.get("matrix")
    selected_range = snapshot.get("range")
    if not isinstance(matrix, list) or not matrix or not isinstance(selected_range, dict):
        return False
    try:
        columns = header_columns(snapshot)
        col_origin = int(selected_range["colFrom"])
    except (KeyError, TypeError, ValueError, TrackerError):
        return False
    boundary = matrix[-1]
    return any(
        column in columns
        and 0 <= columns[column] - col_origin < len(boundary)
        and bool(cell_value(boundary[columns[column] - col_origin]))
        for column in COLUMNS
    )


def read_complete_snapshot(
    executable: str,
    file_id: str,
    worksheet_id: int,
    col_from: int,
    col_to: int,
) -> dict[str, Any]:
    row_to = TRACKER_INITIAL_ROW_TO
    while True:
        snapshot = read_live_snapshot(executable, file_id, worksheet_id, 0, row_to, col_from, col_to)
        if not snapshot_needs_expansion(snapshot):
            return snapshot
        if row_to >= TRACKER_MAX_ROW_TO:
            raise TrackerError(
                "TRACKER_RANGE_LIMIT",
                f"Tracker data still reaches configured row limit {TRACKER_MAX_ROW_TO}",
            )
        row_to = min(row_to + TRACKER_ROW_STEP, TRACKER_MAX_ROW_TO)


def header_columns(snapshot: dict[str, Any]) -> dict[str, int]:
    matrix = snapshot["matrix"]
    if not matrix:
        raise TrackerError("TRACKER_EMPTY", "Tracker snapshot is empty")
    offset = int(snapshot["range"]["colFrom"])
    result = {}
    for row in matrix:
        for index, cell in enumerate(row):
            value = cell_value(cell)
            if value in COLUMNS:
                result[value] = offset + index
    return result


def header_row(snapshot: dict[str, Any]) -> int:
    row_offset = int(snapshot["range"]["rowFrom"])
    for index, row in enumerate(snapshot["matrix"]):
        if sum(1 for cell in row if cell_value(cell) in COLUMNS) >= 2:
            return row_offset + index
    raise TrackerError("TRACKER_HEADER_NOT_FOUND", "Anime/Movie tracker headers were not found")


def compare_strings_windows(left: str, right: str) -> int:
    left_category = 0 if left and left[0].isascii() and left[0].isalnum() else 1
    right_category = 0 if right and right[0].isascii() and right[0].isalnum() else 1
    if left_category != right_category:
        return -1 if left_category < right_category else 1
    if os.name != "nt":
        return (left.casefold() > right.casefold()) - (left.casefold() < right.casefold())
    compare = ctypes.windll.kernel32.CompareStringEx
    compare.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    result = compare("zh-CN", 0x00000008, left, len(left), right, len(right), None, None, 0)
    if result == 0:
        raise TrackerError("COLLATION_UNAVAILABLE", "Windows zh-CN collation failed", "DECISION_REQUIRED")
    return result - 2


def standard_xf(status: str, base: dict[str, Any] | None = None, *, preserve_fill: bool = False) -> dict[str, Any]:
    xf = copy.deepcopy(base or {})
    font = copy.deepcopy(xf.get("font") or {})
    font.setdefault("name", "微软雅黑")
    font.setdefault("dyHeight", 220)
    font.setdefault("color", {"type": 2, "value": 0xFF000000})
    font["bls"] = True
    status_key = STATUS_ALIASES.get(str(status).strip().casefold(), str(status).strip().casefold())
    color = STATUS_COLORS.get(status_key)
    if color is None:
        raise TrackerError("STATUS_INVALID", f"Unknown tracker status: {status}", "DECISION_REQUIRED")
    fill = copy.deepcopy(xf.get("fill")) if preserve_fill and isinstance(xf.get("fill"), dict) else None
    if fill is None:
        fill = {
            "type": 1,
            "back": {"type": 2, "value": color},
            "fore": {"type": 255, "value": 0, "tint": 0},
        }
    xf.update({"font": font, "alcH": 0, "alcV": 1, "wrap": True, "fill": fill})
    return xf


def clear_xf(base: dict[str, Any] | None = None) -> dict[str, Any]:
    return standard_xf("default", base)


def snapshot_column(snapshot: dict[str, Any], absolute_col: int, data_start_row: int) -> list[dict[str, Any]]:
    col_offset = absolute_col - int(snapshot["range"]["colFrom"])
    row_offset = data_start_row - int(snapshot["range"]["rowFrom"])
    cells = []
    for relative_row, row in enumerate(snapshot["matrix"][row_offset:], start=data_start_row):
        if col_offset >= len(row):
            cell = {"value": "", "xf": {}}
        else:
            cell = copy.deepcopy(row[col_offset])
        cells.append({"row": relative_row, "value": cell_value(cell), "xf": cell_xf(cell)})
    return cells


def plan_column_update(
    snapshot: dict[str, Any],
    column: str,
    title: str,
    status: str,
    data_start_row: int = 1,
    *,
    replace_title: str | None = None,
) -> dict[str, Any]:
    if column not in COLUMNS:
        raise TrackerError("COLUMN_INVALID", f"Unsupported tracker column: {column}", "DECISION_REQUIRED")
    columns = header_columns(snapshot)
    if column not in columns:
        raise TrackerError("COLUMN_NOT_FOUND", f"Tracker header not found: {column}")
    absolute_col = columns[column]
    original = snapshot_column(snapshot, absolute_col, data_start_row)
    entries = [
        {"value": item["value"], "xf": item["xf"], "status": "default", "preserveFill": True}
        for item in original
        if item["value"]
    ]
    exact = [item for item in entries if item["value"] == title]
    if len(exact) > 1:
        raise TrackerError("TITLE_DUPLICATE", f"Tracker contains duplicate title: {title}", "DECISION_REQUIRED")
    source_title = str(replace_title or "").strip()
    if source_title and source_title != title:
        sources = [item for item in entries if item["value"] == source_title]
        if len(sources) > 1:
            raise TrackerError(
                "TITLE_RENAME_SOURCE_DUPLICATE",
                f"Tracker contains duplicate WebRip source title: {source_title}",
                "DECISION_REQUIRED",
            )
        if exact:
            raise TrackerError(
                "TITLE_RENAME_CONFLICT",
                f"Clean title already exists while renaming WebRip entry: {title}",
                "DECISION_REQUIRED",
            )
        if not sources:
            raise TrackerError(
                "TITLE_RENAME_SOURCE_MISSING",
                f"Confirmed WebRip source title is missing: {source_title}",
                "FAILED",
            )
        target = sources[0]
        target["value"] = title
        target["status"] = status
        target["xf"] = standard_xf(status, target["xf"])
        target["preserveFill"] = False
    elif exact:
        exact[0]["status"] = status
        exact[0]["xf"] = standard_xf(status, exact[0]["xf"])
        exact[0]["preserveFill"] = False
    else:
        entries.append({"value": title, "xf": standard_xf(status), "status": status, "preserveFill": False})
    entries.sort(key=functools.cmp_to_key(lambda left, right: compare_strings_windows(left["value"], right["value"])))

    original_used = max((index + 1 for index, item in enumerate(original) if item["value"]), default=0)
    final_length = max(original_used, len(entries)) + 1
    operations = []
    final_cells = []
    for index in range(final_length):
        row = data_start_row + index
        if index < len(entries):
            entry = entries[index]
            value = entry["value"]
            xf = standard_xf(
                entry.get("status", "default"),
                entry.get("xf"),
                preserve_fill=bool(entry.get("preserveFill")),
            )
        else:
            value = ""
            base = original[index]["xf"] if index < len(original) else {}
            xf = clear_xf(base)
        final_cells.append({"row": row, "value": value, "xf": xf})
        operations.extend(
            [
                {"opType": "formula", "rowFrom": row, "rowTo": row, "colFrom": absolute_col, "colTo": absolute_col, "formula": value},
                {"opType": "format", "rowFrom": row, "rowTo": row, "colFrom": absolute_col, "colTo": absolute_col, "xf": xf},
            ]
        )
    chunks = [operations[index : index + MAX_KDOCS_OPERATIONS] for index in range(0, len(operations), MAX_KDOCS_OPERATIONS)]
    return {
        "schemaVersion": 1,
        "fileId": snapshot["fileId"],
        "worksheetId": snapshot["worksheetId"],
        "snapshotHash": canonical_hash(snapshot_fingerprint(snapshot)),
        "snapshot": snapshot,
        "column": column,
        "columnIndex": absolute_col,
        "title": title,
        "replaceTitle": source_title or None,
        "status": status,
        "dataStartRow": data_start_row,
        "finalCells": final_cells,
        "operationChunks": chunks,
        "operationCount": len(operations),
    }


def comparable_xf(xf: dict[str, Any]) -> dict[str, Any]:
    font = xf.get("font") or {}
    fill = xf.get("fill") or {}
    return {
        "font": {key: font.get(key) for key in ("name", "dyHeight", "color")},
        "fill": fill,
        "alcH": xf.get("alcH"),
        "alcV": xf.get("alcV"),
        "wrap": xf.get("wrap"),
    }


def verify_plan(plan: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before = plan["snapshot"]
    target_col = int(plan["columnIndex"])
    start = int(plan["dataStartRow"])
    after_cells = snapshot_column(after, target_col, start)
    expected = plan["finalCells"]
    problems = []
    for index, wanted in enumerate(expected):
        actual = after_cells[index] if index < len(after_cells) else {"value": "", "xf": {}}
        if actual["value"] != wanted["value"]:
            problems.append({"row": wanted["row"], "field": "value", "expected": wanted["value"], "actual": actual["value"]})
        # KDocs may omit or normalize wrapping on readback, and an empty
        # trailing row may return no XF at all.  Those are not reliable
        # evidence that the written title, fill, font, or alignment failed.
        if actual["value"] == wanted["value"] == "":
            continue
        wanted_xf = comparable_xf(wanted["xf"])
        actual_xf = comparable_xf(actual["xf"])
        wanted_xf.pop("wrap", None)
        actual_xf.pop("wrap", None)
        if actual_xf != wanted_xf:
            problems.append({"row": wanted["row"], "field": "xf", "expected": wanted_xf, "actual": actual_xf})

    before_range = before["range"]
    before_matrix = before["matrix"]
    after_matrix = after["matrix"]
    for row_index in range(min(len(before_matrix), len(after_matrix))):
        for relative_col in range(min(len(before_matrix[row_index]), len(after_matrix[row_index]))):
            absolute_col = int(before_range["colFrom"]) + relative_col
            if absolute_col == target_col:
                continue
            if before_matrix[row_index][relative_col] != after_matrix[row_index][relative_col]:
                problems.append({"row": int(before_range["rowFrom"]) + row_index, "column": absolute_col, "field": "other-column-changed"})
    return {"status": "OK" if not problems else "FAILED", "problems": problems, "boldVerification": "NOT_AVAILABLE_BY_POLICY"}


def comparable_column(snapshot: dict[str, Any], absolute_col: int, data_start_row: int) -> list[dict[str, Any]]:
    return [
        {"row": item["row"], "value": item["value"], "xf": comparable_xf(item["xf"])}
        for item in snapshot_column(snapshot, absolute_col, data_start_row)
    ]


def snapshot_after_chunks(plan: dict[str, Any], completed_chunks: int) -> dict[str, Any]:
    snapshot = copy.deepcopy(plan["snapshot"])
    selected_range = snapshot["range"]
    row_origin = int(selected_range["rowFrom"])
    col_origin = int(selected_range["colFrom"])
    chunks = plan["operationChunks"]
    if completed_chunks < 0 or completed_chunks > len(chunks):
        raise TrackerError("TRACKER_CHUNK_PROGRESS_INVALID", "Completed tracker chunk count is invalid")
    for chunk in chunks[:completed_chunks]:
        for operation in chunk:
            for row in range(int(operation["rowFrom"]), int(operation["rowTo"]) + 1):
                for column in range(int(operation["colFrom"]), int(operation["colTo"]) + 1):
                    cell = snapshot["matrix"][row - row_origin][column - col_origin]
                    if operation.get("opType") == "formula":
                        cell["value"] = operation.get("formula", "")
                    elif operation.get("opType") == "format":
                        cell["xf"] = copy.deepcopy(operation.get("xf") or {})
    return snapshot


def matching_chunk_prefix(plan: dict[str, Any], live: dict[str, Any]) -> int | None:
    target_col = int(plan["columnIndex"])
    data_start_row = int(plan["dataStartRow"])
    live_column = comparable_column(live, target_col, data_start_row)
    matches = [
        count
        for count in range(len(plan["operationChunks"]) + 1)
        if comparable_column(snapshot_after_chunks(plan, count), target_col, data_start_row) == live_column
    ]
    return max(matches) if matches else None


def apply_static_plan(
    plan: dict[str, Any],
    kdocs: str,
    *,
    completed_chunks: int = 0,
    on_chunk_complete=None,
) -> dict[str, Any]:
    snapshot = plan["snapshot"]
    selected_range = snapshot["range"]
    target_col = int(plan["columnIndex"])
    data_start_row = int(plan["dataStartRow"])
    live_before = read_live_snapshot(
        kdocs,
        plan["fileId"],
        int(plan["worksheetId"]),
        int(selected_range["rowFrom"]),
        int(selected_range["rowTo"]),
        int(selected_range["colFrom"]),
        int(selected_range["colTo"]),
    )
    chunks = plan["operationChunks"]
    matched_chunks = matching_chunk_prefix(plan, live_before)
    if matched_chunks is None or matched_chunks < completed_chunks:
        raise TrackerError(
            "TRACKER_CONCURRENT_CHANGE",
            f"Tracker column {plan['column']} no longer matches this batch's resumable state; no write was performed",
        )
    if matched_chunks > completed_chunks and callable(on_chunk_complete):
        on_chunk_complete(matched_chunks, len(chunks))
    applied = 0
    for chunk_index, chunk in enumerate(chunks[matched_chunks:], start=matched_chunks):
        if len(chunk) > MAX_KDOCS_OPERATIONS:
            raise TrackerError("KDOCS_CHUNK_LIMIT", f"Operation chunk exceeds {MAX_KDOCS_OPERATIONS}")
        run_kdocs(
            kdocs,
            "sheet",
            "update-range-data",
            {"file_id": plan["fileId"], "worksheet_id": int(plan["worksheetId"]), "rangeData": chunk},
            retries=1,
        )
        applied += len(chunk)
        if callable(on_chunk_complete):
            on_chunk_complete(chunk_index + 1, len(chunks))
    after = read_live_snapshot(
        kdocs,
        plan["fileId"],
        int(plan["worksheetId"]),
        int(selected_range["rowFrom"]),
        int(selected_range["rowTo"]),
        int(selected_range["colFrom"]),
        int(selected_range["colTo"]),
    )
    verification = verify_plan(plan, after)
    if verification["status"] != "OK":
        raise TrackerError("TRACKER_VERIFY_FAILED", json.dumps(verification["problems"], ensure_ascii=False))
    return {
        "status": "COMPLETE",
        "operations": applied,
        "completedChunks": len(chunks),
        "verification": verification,
        "note": "加粗已写入，但按策略不自动读取验证",
    }
