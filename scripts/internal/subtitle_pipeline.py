"""ASS font discovery, assfonts execution/recovery, and subtitle renaming."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from archive_rules import ALLOWED_HIDDEN_NAMES, log_path, numbered_output_path, resolve_path, temporary_path
from common import read_json, read_text
from internal.errors import WorkflowError
from internal.signatures import file_signature, signature_matches


FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
SUBTITLE_SUFFIXES = {".ass", ".ssa"}


def read_ass_text(path: Path) -> str:
    try:
        return read_text(path)
    except UnicodeError as exc:
        raise WorkflowError("SUBTITLE_ENCODING", f"ASS/SSA must be UTF-8, UTF-8 with BOM, or GB18030: {path}") from exc


def write_ass_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def normalize_font_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lstrip("@")).casefold()


def indexed_path_is_under(value: str, root: Path) -> bool:
    candidate = os.path.normcase(
        os.path.abspath(os.path.normpath(os.path.expandvars(os.path.expanduser(value))))
    ).rstrip("\\/")
    parent = os.path.normcase(os.path.abspath(os.path.normpath(str(root)))).rstrip("\\/")
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        return False


def parse_ass_font_requirements(path: Path) -> list[dict[str, Any]]:
    text = read_ass_text(path)
    section = ""
    format_fields: list[str] = []
    found: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.casefold()
            format_fields = []
            continue
        if section in {"[v4 styles]", "[v4+ styles]"}:
            if line.casefold().startswith("format:"):
                format_fields = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
            elif line.casefold().startswith("style:"):
                values = [part.strip() for part in line.split(":", 1)[1].split(",")]
                if "fontname" in format_fields:
                    index = format_fields.index("fontname")
                    if index < len(values) and values[index]:
                        name = values[index]
                        key = normalize_font_name(name)
                        found.setdefault(key, {"name": name, "sources": set(), "contexts": set()})
                        found[key]["sources"].add(str(path))
                        found[key]["contexts"].add("style")
        if line.casefold().startswith("dialogue:"):
            for match in re.finditer(r"\\fn([^\\}\r\n]+)", line, flags=re.IGNORECASE):
                name = match.group(1).strip()
                if not name:
                    continue
                key = normalize_font_name(name)
                found.setdefault(key, {"name": name, "sources": set(), "contexts": set()})
                found[key]["sources"].add(str(path))
                found[key]["contexts"].add("inline-fn")
    return [
        {"normalized": key, "name": value["name"], "sources": sorted(value["sources"]), "contexts": sorted(value["contexts"])}
        for key, value in sorted(found.items())
    ]


def load_assfonts_database(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise WorkflowError("ASSFONTS_DATABASE_NOT_FOUND", f"assfonts database not found: {path}")
    value = read_json(path)
    if not isinstance(value, list):
        raise WorkflowError("ASSFONTS_DATABASE_INVALID", f"assfonts database root must be an array: {path}")
    validate_assfonts_database_records(value, path)
    return value


def validate_assfonts_database_records(records: list[dict[str, Any]], path: Path | None = None) -> None:
    source = f": {path}" if path is not None else ""
    seen: set[tuple[str, int]] = set()
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise WorkflowError(
                "ASSFONTS_DATABASE_INVALID",
                f"assfonts database record {position} must be an object{source}",
            )
        font_path = record.get("path")
        try:
            face_index = int(record.get("index", 0))
        except (TypeError, ValueError) as exc:
            raise WorkflowError(
                "ASSFONTS_DATABASE_INVALID",
                f"assfonts database record {position} has an invalid face index{source}",
            ) from exc
        if not isinstance(font_path, str) or not font_path.strip() or face_index < 0:
            raise WorkflowError(
                "ASSFONTS_DATABASE_INVALID",
                f"assfonts database record {position} has an invalid path or face index{source}",
            )
        normalized_path = os.path.abspath(
            os.path.normpath(os.path.expandvars(os.path.expanduser(font_path)))
        )
        key = (os.path.normcase(normalized_path), face_index)
        if key in seen:
            raise WorkflowError(
                "ASSFONTS_DATABASE_DUPLICATE",
                f"assfonts database contains duplicate path/index record: {font_path}#{face_index}{source}",
            )
        seen.add(key)


def refresh_assfonts_database(
    assfonts: str,
    primary: Path,
    database: Path,
    *,
    runner: Callable[[list[str]], dict[str, Any]],
    clean: bool,
) -> dict[str, Any]:
    """Build in a sibling directory, validate, then atomically replace fonts.json."""

    primary = primary.resolve(strict=False)
    database = database.resolve(strict=False)
    database.mkdir(parents=True, exist_ok=True)
    live = database / "fonts.json"
    temporary = Path(tempfile.mkdtemp(prefix=".archive-assfonts-", dir=str(database.parent)))
    build_file = temporary / "fonts.json"
    try:
        if not clean:
            load_assfonts_database(live)
            shutil.copy2(live, build_file)
        result = runner([assfonts, "-f", str(primary), "-b", "-d", str(temporary)])
        records = load_assfonts_database(build_file)
        if not records:
            raise WorkflowError(
                "ASSFONTS_DATABASE_EMPTY",
                f"assfonts generated an empty database: {build_file}",
            )
        os.replace(build_file, live)
        return {
            "records": len(records),
            "database": str(live),
            "temporaryDatabase": str(temporary),
            "exitCode": result.get("exitCode"),
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def load_fallback_font_database(path: Path, fallback_root: Path) -> dict[str, Any]:
    """Read FontLoaderSub's fldd index without walking the fallback font tree."""

    path = path.resolve(strict=False)
    fallback_root = fallback_root.resolve(strict=False)
    if not path.is_file():
        raise WorkflowError(
            "FALLBACK_FONT_DATABASE_NOT_FOUND",
            f"fallback font database not found: {path}",
        )
    data = path.read_bytes()
    if len(data) < 16:
        raise WorkflowError("FALLBACK_FONT_DATABASE_INVALID", f"fallback font database header is truncated: {path}")
    magic, expected_files, expected_names, expected_size = struct.unpack("<4sIII", data[:16])
    if magic != b"fldd" or expected_size != len(data) or len(data[16:]) % 2:
        raise WorkflowError("FALLBACK_FONT_DATABASE_INVALID", f"invalid fallback font database header: {path}")
    try:
        fields = data[16:].decode("utf-16le").split("\0")
    except UnicodeDecodeError as exc:
        raise WorkflowError(
            "FALLBACK_FONT_DATABASE_INVALID",
            f"fallback font database is not valid UTF-16LE: {path}",
        ) from exc

    records: list[dict[str, Any]] = []
    file_count = 0
    name_count = 0
    relative_path: str | None = None
    resolved_path: Path | None = None
    extension = ""
    face_index = -1
    names: list[str] = []

    def flush_face() -> None:
        nonlocal names
        if resolved_path is not None and face_index >= 0 and names:
            records.append(
                {
                    "families": list(dict.fromkeys(names)),
                    "fullnames": [],
                    "psnames": [],
                    "path": str(resolved_path),
                    "index": face_index,
                    "outline": "truetype" if extension in {"ttf", "ttc"} else "otf-cff",
                }
            )
        names = []

    def flush_file() -> None:
        nonlocal relative_path, resolved_path, extension, face_index, file_count
        if relative_path is None:
            return
        flush_face()
        file_count += 1
        relative_path = None
        resolved_path = None
        extension = ""
        face_index = -1

    for raw_field in fields:
        field = raw_field[1:] if raw_field.startswith("\n") else raw_field
        field = field.strip("\r\n")
        if not field:
            flush_file()
            continue
        if relative_path is None:
            candidate = Path(field.replace("/", os.sep).replace("\\", os.sep))
            if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
                raise WorkflowError(
                    "FALLBACK_FONT_DATABASE_PATH",
                    f"fallback font database path escapes its root: {field}",
                )
            target = Path(os.path.abspath(os.path.normpath(str(fallback_root / candidate))))
            try:
                inside = os.path.commonpath([str(fallback_root), str(target)]) == str(fallback_root)
            except ValueError:
                inside = False
            if not inside:
                raise WorkflowError(
                    "FALLBACK_FONT_DATABASE_PATH",
                    f"fallback font database path escapes its root: {field}",
                )
            relative_path = field
            resolved_path = target
            continue
        if field.startswith("\tt:"):
            extension = field[3:].strip().casefold()
            if extension not in {suffix.lstrip(".") for suffix in FONT_SUFFIXES}:
                raise WorkflowError(
                    "FALLBACK_FONT_DATABASE_INVALID",
                    f"unsupported font type in fallback database: {field}",
                )
            continue
        if field.startswith("\tv:"):
            flush_face()
            face_index += 1
            continue
        if field.startswith("\t"):
            continue
        if face_index < 0:
            face_index = 0
        names.append(field)
        name_count += 1
    flush_file()

    if file_count != expected_files or name_count != expected_names:
        raise WorkflowError(
            "FALLBACK_FONT_DATABASE_INVALID",
            f"fallback font database count mismatch: files {file_count}/{expected_files}, names {name_count}/{expected_names}",
        )
    database_mtime = path.stat().st_mtime_ns
    stale = fallback_root.exists() and fallback_root.stat().st_mtime_ns > database_mtime
    return {
        "path": str(path),
        "fileCount": file_count,
        "nameCount": name_count,
        "records": records,
        "stale": stale,
    }


def fallback_font_database_path(config: dict[str, Any], fallback_root: Path) -> Path:
    configured = str(config.get("paths", {}).get("fallbackFontDatabase") or "").strip()
    return resolve_path(configured) if configured else fallback_root / "fc-subs.db"


def font_aliases(record: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for key in ("families", "fullnames", "psnames"):
        for value in record.get(key, []) or []:
            if isinstance(value, str) and value.strip():
                aliases.add(normalize_font_name(value))
    return aliases


def build_font_lookup(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    lookup: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for alias in font_aliases(record):
            lookup.setdefault(alias, []).append(record)
    return lookup


def font_file_records(path: Path) -> list[dict[str, Any]]:
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError as exc:
        raise WorkflowError("FONTTOOLS_NOT_FOUND", "fontTools is required for font inspection") from exc
    records: list[dict[str, Any]] = []
    fonts: list[tuple[int, Any]] = []
    try:
        with path.open("rb") as header_stream:
            header = header_stream.read(4)
        if header not in {b"OTTO", b"ttcf", b"true", b"typ1", b"\x00\x01\x00\x00"}:
            return [{"path": str(path.resolve()), "index": 0, "names": [], "outline": "unreadable", "error": "invalid sfnt header"}]
        if path.suffix.casefold() in {".ttc", ".otc"}:
            collection = TTCollection(str(path), lazy=True)
            fonts = list(enumerate(collection.fonts))
        else:
            fonts = [(0, TTFont(str(path), lazy=True))]
        for index, font in fonts:
            names: set[str] = set()
            if "name" in font:
                for item in font["name"].names:
                    if item.nameID not in {1, 2, 4, 6, 16, 17}:
                        continue
                    try:
                        value = item.toUnicode().strip()
                    except Exception:
                        continue
                    if value:
                        names.add(value)
            outline = "otf-cff" if "CFF " in font or "CFF2" in font else "truetype" if "glyf" in font else "unknown"
            records.append({"path": str(path.resolve()), "index": index, "names": sorted(names), "outline": outline})
    except Exception as exc:
        return [{"path": str(path.resolve()), "index": 0, "names": [], "outline": "unreadable", "error": str(exc)}]
    finally:
        for _, font in fonts:
            try:
                font.close()
            except Exception:
                pass
    return records


def search_missing_fonts(
    requirements: list[dict[str, Any]],
    roots: list[Path],
    *,
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    inspector = inspector or font_file_records
    wanted = {item["normalized"] for item in requirements}
    results: dict[str, list[dict[str, Any]]] = {name: [] for name in wanted}
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.casefold() not in FONT_SUFFIXES
                or any(part in ALLOWED_HIDDEN_NAMES for part in path.parts)
                or any(part.casefold().endswith("_subsetted") for part in path.parts)
            ):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            for record in inspector(resolved):
                aliases = {normalize_font_name(name) for name in record.get("names", [])}
                for name in wanted & aliases:
                    results[name].append(record)
    return results


def readable_font_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in records if item.get("outline") not in {"unreadable", "unknown", "missing"}]


def locate_font_sources(
    requirements: list[dict[str, Any]],
    records: list[dict[str, Any]],
    work: Path,
    primary: Path,
    fallback_loader: Callable[[], list[dict[str, Any]]] | None = None,
    *,
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    scan = lambda wanted, roots: search_missing_fonts(wanted, roots, inspector=inspector)
    work_map = {
        key: readable_font_records(values)
        for key, values in (scan(requirements, [work]) if requirements else {}).items()
    }
    primary_lookup = build_font_lookup(
        [record for record in records if record.get("path") and indexed_path_is_under(record["path"], primary)]
    )
    primary_database = {
        item["normalized"]: indexed_font_candidates(primary_lookup, item["normalized"], "primary")
        for item in requirements
    }
    unresolved_for_fallback = [
        item
        for item in requirements
        if not work_map.get(item["normalized"]) and not primary_database.get(item["normalized"])
    ]
    fallback_lookup = build_font_lookup(fallback_loader()) if unresolved_for_fallback and fallback_loader else {}
    fallback_database = {
        item["normalized"]: indexed_font_candidates(fallback_lookup, item["normalized"], "fallback")
        for item in unresolved_for_fallback
    }
    locations: list[dict[str, Any]] = []
    for requirement in requirements:
        key = requirement["normalized"]
        tier = "work"
        sources = work_map.get(key, [])
        if not sources:
            tier = "primary"
            sources = primary_database.get(key, [])
        if not sources:
            tier = "fallback"
            sources = fallback_database.get(key, [])
        locations.append({**requirement, "tier": tier, "sources": sources})
    return locations


def resolve_font_availability(
    requirements: list[dict[str, Any]],
    records: list[dict[str, Any]],
    work: Path,
    primary: Path,
    fallback_loader: Callable[[], list[dict[str, Any]]] | None = None,
    *,
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    availability: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for location in locate_font_sources(
        requirements, records, work, primary, fallback_loader, inspector=inspector
    ):
        available = bool(location["sources"])
        item = {key: value for key, value in location.items() if key != "sources"}
        item["available"] = available
        if available:
            selected = location["sources"][0]
            item["selectedSource"] = {
                key: selected[key]
                for key in ("path", "index", "outline")
                if key in selected
            }
            item["selectedSource"]["file"] = file_signature(resolve_path(selected["path"]))
        availability.append(item)
        if not available:
            issues.append({"code": "FONT_NOT_FOUND", "font": location["name"]})
    return availability, issues


def font_content_identity(path: Path, cache: dict[str, tuple[int, str]] | None = None) -> tuple[int, str]:
    resolved = path.resolve()
    key = os.path.normcase(str(resolved))
    if cache is not None and key in cache:
        return cache[key]
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = (resolved.stat().st_size, digest.hexdigest())
    if cache is not None:
        cache[key] = identity
    return identity


def font_candidate_sort_key(record: dict[str, Any]) -> tuple[int, int, str, int]:
    tier_rank = {"work": 0, "primary": 1, "fallback": 2}
    return (
        tier_rank.get(str(record.get("tier")), 99),
        0 if record.get("outline") == "truetype" else 1,
        os.path.normcase(str(record.get("path", ""))),
        int(record.get("index", 0) or 0),
    )


def indexed_font_candidates(
    lookup: dict[str, list[dict[str, Any]]],
    name: str,
    tier: str,
) -> list[dict[str, Any]]:
    candidates = []
    for record in lookup.get(normalize_font_name(name), []):
        font_path = record.get("path")
        if not font_path:
            continue
        path = resolve_path(font_path)
        if not path.is_file():
            continue
        outline = record.get("outline")
        if not outline:
            outline = "truetype" if path.suffix.casefold() in {".ttf", ".ttc"} else "otf-cff"
        candidates.append({**record, "path": str(path), "tier": tier, "outline": outline})
    return sorted(candidates, key=font_candidate_sort_key)


def stable_font_destination(
    source: Path,
    primary: Path,
    internal_name: str,
    *,
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> tuple[Path, str]:
    inspector = inspector or font_file_records

    def identities(path: Path) -> set[tuple[Any, ...]]:
        size = path.stat().st_size
        return {
            (tuple(normalize_font_name(name) for name in record.get("names", [])), record.get("outline"), int(record.get("index", 0)), size)
            for record in inspector(path)
            if record.get("outline") not in {"unreadable", "unknown", "missing"}
        }

    direct = primary / source.name
    if not direct.exists():
        return direct, "copy"
    source_identities = identities(source)
    if source_identities & identities(direct):
        return direct, "reuse-identical"
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", internal_name).strip("-.") or source.stem
    face_index = min((item[2] for item in source_identities), default=0)
    candidate = primary / f"{safe}.face{face_index}.{source.stat().st_size}{source.suffix.casefold()}"
    if candidate.exists():
        if source_identities & identities(candidate):
            return candidate, "reuse-identical"
        raise WorkflowError("FONT_DESTINATION_COLLISION", f"Distinct font shares stable destination: {candidate}", "DECISION_REQUIRED")
    return candidate, "copy-renamed"


def cleanup_assfonts_intermediates(work: Path) -> list[str]:
    removed = []
    stale_files = [path for path in work.rglob("*") if path.is_file() and path.name.casefold().endswith(".assfonts.ass")]
    stale_directories = [
        path for path in work.rglob("*") if path.is_dir() and path.name.casefold().endswith("_subsetted")
    ]
    for stale in stale_files:
        stale.unlink()
        removed.append(str(stale))
    for stale in sorted(stale_directories, key=lambda item: len(item.parts), reverse=True):
        if stale.exists():
            shutil.rmtree(stale)
            removed.append(str(stale))
    return removed


def expected_assfonts_output(source: Path) -> Path:
    return source.with_suffix(".assfonts" + source.suffix)


def validate_subset_output(path: Path, log: str, required_names: set[str] | None = None) -> None:
    if "[ERROR]" in log:
        raise WorkflowError("ASSFONTS_ERROR", f"assfonts reported [ERROR] for {path}")
    if not path.is_file():
        raise WorkflowError("ASSFONTS_OUTPUT_MISSING", f"Expected assfonts output missing: {path}")
    text = read_ass_text(path)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if not re.search(r"(?im)^\[Script Info\]\s*$", normalized):
        normalized = "[Script Info]\r\n" + normalized.lstrip("\r\n")
    if normalized != text or not path.read_bytes().startswith(b"\xef\xbb\xbf"):
        path.write_bytes(b"\xef\xbb\xbf" + normalized.encode("utf-8"))
        text = normalized
    if "[Fonts]" not in text or "fontname:" not in text.casefold():
        raise WorkflowError("ASSFONTS_FONT_SECTION_INVALID", f"Invalid [Fonts] section: {path}")


def recovery_font_sources(
    requirement: dict[str, Any],
    recovery_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return list(recovery_index.get(requirement["normalized"], []))[:8]


def build_recovery_font_index(
    requirements: list[dict[str, Any]],
    work: Path,
    primary: Path,
    assfonts_records: list[dict[str, Any]],
    fallback_records: list[dict[str, Any]],
    *,
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    keys = [str(item.get("normalized") or "") for item in requirements if item.get("normalized")]
    found: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    scanned = search_missing_fonts(requirements, [work], inspector=inspector)
    for key in keys:
        found[key].extend({**record, "tier": "work"} for record in scanned.get(key, []))
    primary_lookup = build_font_lookup(
        [
            record
            for record in assfonts_records
            if record.get("path") and indexed_path_is_under(record["path"], primary)
        ]
    )
    fallback_lookup = build_font_lookup(fallback_records)
    for requirement in requirements:
        key = requirement["normalized"]
        found[key].extend(indexed_font_candidates(primary_lookup, key, "primary"))
        found[key].extend(indexed_font_candidates(fallback_lookup, key, "fallback"))

    digest_cache: dict[str, tuple[int, str]] = {}
    indexed: dict[str, list[dict[str, Any]]] = {}
    for key in keys:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[int, str, int]] = set()
        for source in sorted(found.get(key, []), key=font_candidate_sort_key):
            path = resolve_path(source.get("path", ""))
            if not path.is_file() or source.get("outline") in {"unreadable", "unknown", "missing"}:
                continue
            size, digest = font_content_identity(path, digest_cache)
            identity = (size, digest, int(source.get("index", 0) or 0))
            if identity in seen:
                continue
            seen.add(identity)
            unique.append({**source, "path": str(path)})
        indexed[key] = unique
    return indexed


def failed_font_requirements(
    manifest: dict[str, Any],
    log: str,
    required: set[str],
    recovery_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    normalized_log = normalize_font_name(log)
    matched = []
    for requirement in manifest.get("discovery", {}).get("fontRequirements", []):
        key = requirement.get("normalized")
        if key not in required:
            continue
        tokens = [str(requirement.get("name") or "")]
        for source in recovery_font_sources(requirement, recovery_index):
            source_path = Path(str(source.get("path") or ""))
            tokens.extend([source_path.name, source_path.stem])

        def explicitly_named(token: str) -> bool:
            normalized = normalize_font_name(token)
            if not normalized:
                return False
            if len(normalized) <= 2:
                return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", normalized_log) is not None
            return normalized in normalized_log

        if any(explicitly_named(token) for token in tokens):
            matched.append(requirement)
    return matched


def stage_recovery_font(
    source_record: dict[str, Any],
    work: Path,
    attempt: int,
) -> Path:
    root = temporary_path(work, "fonts") / "recovery" / f"attempt-{attempt:03d}"
    if root.exists():
        shutil.rmtree(root)
    selected = root / "selected"
    selected.mkdir(parents=True)
    source = resolve_path(source_record["path"])
    shutil.copy2(source, selected / f"000-{source.name}")
    return selected


def convert_recovery_font(
    source_record: dict[str, Any],
    work: Path,
    attempt: int,
    runner: Callable[[list[str]], dict[str, Any]],
    otf2ttf: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = resolve_path(source_record["path"])
    face_index = int(source_record.get("index", 0) or 0)
    original_dir = temporary_path(work, "fonts") / "original"
    converted_dir = temporary_path(work, "fonts") / "converted"
    original_dir.mkdir(parents=True, exist_ok=True)
    converted_dir.mkdir(parents=True, exist_ok=True)
    original_suffix = ".otf" if source.suffix.casefold() == ".ttf" else source.suffix
    local_original = original_dir / f"{source.stem}.face{face_index}{original_suffix}"
    if not local_original.exists():
        shutil.copy2(source, local_original)
    output = converted_dir / f"{source.stem}.face{face_index}.{attempt:03d}.ttf"
    converted = runner([otf2ttf, str(local_original), "-o", str(output), "--face-index", str(face_index), "--overwrite"])
    if converted["exitCode"] != 0 or not output.is_file():
        raise WorkflowError("OTF_CONVERSION_FAILED", converted["stderr"] or converted["stdout"])
    record = {
        "path": str(output),
        "index": 0,
        "names": source_record.get("names", []),
        "outline": "truetype",
        "tier": source_record.get("tier"),
    }
    return record, {"source": str(source), "faceIndex": face_index, "output": str(output)}


def prepare_fonts(
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    assfonts: str,
    database: Path,
    runner: Callable[[list[str]], dict[str, Any]],
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    work = resolve_path(manifest["workPath"])
    removed = cleanup_assfonts_intermediates(work)
    required = manifest["discovery"].get("fontRequirements", [])
    if not required:
        return {"status": "SKIPPED", "reason": "NO_ASS_SSA_FONT_REQUIREMENTS"}
    primary = resolve_path(config["paths"].get("primaryFonts", ""))
    if not primary.is_dir():
        raise WorkflowError("PRIMARY_FONTS_NOT_FOUND", f"Primary font directory not found: {primary}")
    db_file = database / "fonts.json"
    current_records = load_assfonts_database(db_file)
    current_lookup = build_font_lookup(current_records)
    availability = {
        item.get("normalized"): item
        for item in manifest.get("discovery", {}).get("fontAvailability", [])
        if item.get("normalized")
    }
    imports: list[dict[str, Any]] = []
    seen_content: set[tuple[int, str]] = set()
    digest_cache: dict[str, tuple[int, str]] = {}
    rebuild_required = False
    for requirement in sorted(required, key=lambda value: str(value.get("normalized") or "")):
        key = requirement["normalized"]
        if indexed_font_candidates(current_lookup, key, "primary"):
            continue
        location = availability.get(key, {})
        selected = location.get("selectedSource", {})
        if not location.get("available") or location.get("tier") not in {"work", "fallback"} or not selected.get("path"):
            raise WorkflowError(
                "FONT_PREFLIGHT_INCOMPLETE",
                f"Required font has no approved indexed source: {requirement['name']}",
            )
        source = resolve_path(selected["path"])
        if not source.is_file() or (selected.get("file") and not signature_matches(selected["file"])):
            raise WorkflowError("FONT_SOURCE_CHANGED", f"Indexed font source is missing: {source}")
        rebuild_required = True
        content_key = font_content_identity(source, digest_cache)
        if content_key in seen_content:
            continue
        seen_content.add(content_key)
        destination, action = stable_font_destination(
            source, primary, requirement["name"], inspector=inspector
        )
        if action.startswith("copy"):
            shutil.copy2(source, destination)
        imports.append(
            {
                "font": requirement["name"],
                "tier": location.get("tier"),
                "source": str(source),
                "destination": str(destination),
                "action": action,
            }
        )

    rebuild = None
    if rebuild_required:
        rebuild = refresh_assfonts_database(
            assfonts, primary, database, runner=runner, clean=False
        )
    records = load_assfonts_database(db_file)
    lookup = build_font_lookup(records)
    unresolved_after = [
        item["name"]
        for item in manifest["discovery"].get("fontRequirements", [])
        if item["normalized"] not in lookup
    ]
    if unresolved_after:
        raise WorkflowError("ASSFONTS_DATABASE_VERIFY_FAILED", f"Fonts remain unresolved after rebuild: {unresolved_after}")

    task_font_root = temporary_path(work, "fonts")
    converted_dir = task_font_root / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)
    manifest["fontPreparation"] = {
        "imports": imports,
        "globalDatabaseRebuilt": rebuild is not None,
        "globalDatabase": str(db_file),
        "convertedDirectory": str(converted_dir),
        "conversions": [],
        "removedStaleIntermediates": removed,
    }
    return {
        "status": "COMPLETE",
        "imports": imports,
        "conversions": [],
        "globalDatabase": str(db_file),
        "stage": {"imported": len(imports), "converted": 0},
    }


def subset_subtitles(
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    assfonts: str,
    database: Path,
    runner: Callable[[list[str]], dict[str, Any]],
    tool_resolver: Callable[[str], str],
    inspector: Callable[[Path], list[dict[str, Any]]] | None = None,
    on_failure: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    work = resolve_path(manifest["workPath"])
    groups = manifest.get("plan", {}).get("subtitleGroups")
    if not groups:
        by_group: dict[str, list[str]] = {}
        for subtitle in manifest["discovery"].get("subtitles", []):
            by_group.setdefault(subtitle["group"], []).append(subtitle["file"]["path"])
        groups = [{"name": group, "inputs": inputs} for group, inputs in sorted(by_group.items())]
    if not groups:
        return {"status": "SKIPPED", "reason": "NO_ASS_SSA"}
    if manifest.get("stages", {}).get("prepare-fonts", {}).get("status") != "COMPLETE":
        raise WorkflowError("FONT_PREPARATION_REQUIRED", "prepare-fonts must complete before subset")

    logical = os.cpu_count() or 1
    group_count = len(groups)
    threads = max(1, logical // group_count)
    primary = resolve_path(config["paths"]["primaryFonts"])
    prep = manifest["fontPreparation"]
    font_paths: list[str] = []
    log_dir = log_path(work)
    log_dir.mkdir(parents=True, exist_ok=True)
    discovery_by_path = {
        os.path.normcase(str(resolve_path(item["file"]["path"]))): {font["normalized"] for font in item.get("fonts", [])}
        for item in manifest.get("discovery", {}).get("subtitles", [])
    }

    def run_group(
        index_group: tuple[int, dict[str, Any]],
        *,
        retry: bool = False,
        override_font_paths: list[str] | None = None,
        override_database: Path | None = None,
        log_tag: str = "",
    ) -> dict[str, Any]:
        index, group = index_group
        inputs = [resolve_path(value) for value in group.get("inputs", [])]
        if not inputs:
            raise WorkflowError("SUBTITLE_GROUP_EMPTY", f"Subtitle group has no inputs: {group.get('name', index)}")
        active_font_paths = override_font_paths or font_paths
        active_database = override_database or database
        inputs_by_directory: dict[str, list[Path]] = {}
        for item in inputs:
            inputs_by_directory.setdefault(os.path.normcase(str(item.parent)), []).append(item)
        logs = []
        for directory_inputs in inputs_by_directory.values():
            command = [assfonts]
            if active_font_paths:
                command.extend(["-f", *active_font_paths])
            command.extend([
                "-d",
                str(active_database),
                "-m",
                str(threads),
                "-i",
                *[str(item) for item in directory_inputs],
            ])
            result = runner(command)
            logs.append(result["stdout"] + "\n" + result["stderr"])
        log = "\n".join(logs)
        suffix = f"-{log_tag}" if log_tag else ""
        output_log = log_dir / f"subset-{index + 1:02d}{suffix}.log"
        output_log.write_text(log, encoding="utf-8")
        outputs = []
        group_requirements: set[str] = set()
        expected_values = group.get("outputs") or [str(expected_assfonts_output(item)) for item in inputs]
        for input_value, output_value in zip(inputs, expected_values):
            output = resolve_path(output_value)
            required_names = discovery_by_path.get(os.path.normcase(str(input_value)), set())
            group_requirements.update(required_names)
            validate_subset_output(output, log, required_names)
            outputs.append(file_signature(output))
        warning = "not a .ttf" in log and "[ERROR]" not in log
        return {
            "name": group.get("name", str(index)),
            "index": index,
            "inputs": [str(item) for item in inputs],
            "requiredFonts": sorted(group_requirements),
            "outputs": outputs,
            "log": str(output_log),
            "warning": warning,
            "retry": retry,
        }

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=group_count) as executor:
        futures = {executor.submit(run_group, item): item for item in enumerate(groups)}
        for future, item in futures.items():
            try:
                results.append(future.result())
            except Exception as exc:
                index, _ = item
                initial_log = log_dir / f"subset-{index + 1:02d}.log"
                failures.append({
                    "item": item,
                    "error": str(exc),
                    "log": read_text(initial_log) if initial_log.is_file() else str(exc),
                })

    conversions = []
    if failures:
        fallback_value = str(config.get("paths", {}).get("fallbackFonts") or "").strip()
        fallback = resolve_path(fallback_value) if fallback_value else None
        fallback_records: list[dict[str, Any]] = []
        if fallback is not None:
            fallback_records = load_fallback_font_database(
                fallback_font_database_path(config, fallback), fallback
            )["records"]
        recovery_candidates = build_recovery_font_index(
            list(manifest.get("discovery", {}).get("fontRequirements", [])),
            work,
            primary,
            load_assfonts_database(database / "fonts.json"),
            fallback_records,
            inspector=inspector,
        )
        recovery_attempt = 0
        for failure in failures:
            _, group = failure["item"]
            required_keys: set[str] = set()
            for value in group.get("inputs", []):
                required_keys.update(discovery_by_path.get(os.path.normcase(str(resolve_path(value))), set()))
            requirements = failed_font_requirements(
                manifest, failure["log"], required_keys, recovery_candidates
            )
            if not requirements:
                if on_failure:
                    on_failure({"failures": failures, "reason": "ERROR_FONT_UNRESOLVED"})
                raise WorkflowError(
                    "ASSFONTS_ERROR_FONT_UNRESOLVED",
                    f"assfonts error did not identify a required font: {failure['log']}",
                )

            recovered = None
            attempt_errors = []
            for requirement in requirements:
                sources = recovery_font_sources(requirement, recovery_candidates)
                for source_record in sources:
                    recovery_attempt += 1
                    effective = source_record
                    if source_record.get("outline") == "otf-cff":
                        try:
                            effective, conversion = convert_recovery_font(
                                source_record,
                                work,
                                recovery_attempt,
                                runner,
                                tool_resolver("otf2ttf"),
                            )
                            conversions.append(conversion)
                        except Exception as exc:
                            attempt_errors.append(str(exc))
                            continue
                    try:
                        recovery_dir = stage_recovery_font(effective, work, recovery_attempt)
                        recovered = run_group(
                            failure["item"],
                            retry=True,
                            override_font_paths=[str(recovery_dir)],
                            override_database=database,
                            log_tag=f"retry-{recovery_attempt:03d}",
                        )
                        break
                    except Exception as exc:
                        attempt_errors.append(str(exc))
                if recovered is not None:
                    break
            if recovered is None:
                if on_failure:
                    on_failure({
                        "error": attempt_errors[-1] if attempt_errors else failure["error"],
                        "recoveryAttempts": recovery_attempt,
                    })
                raise WorkflowError("ASSFONTS_RECOVERY_FAILED", str(attempt_errors or failures))
            results.append(recovered)
        prep["conversions"] = conversions
        manifest["fontPreparation"] = prep
    manifest["subsetResults"] = sorted(results, key=lambda item: item["name"])
    warning_groups = [result["name"] for result in results if result["warning"]]
    return {
        "status": "COMPLETE",
        "groups": results,
        "threadsPerGroup": threads,
        "warningGroups": warning_groups,
        "stage": {
            "groups": len(results),
            "threadsPerGroup": threads,
            "convertedAfterFailure": len(conversions),
        },
    }


def rename_subtitles(manifest: dict[str, Any], *, direct_output: bool = False) -> dict[str, Any]:
    jobs = manifest.get("plan", {}).get("renameJobs", [])
    if not jobs:
        return {"status": "SKIPPED", "reason": "NO_RENAME_JOBS"}
    completed = []
    for job in jobs:
        source, planned_target = resolve_path(job["source"]), resolve_path(job["target"])
        target = planned_target
        if direct_output and target.exists() and target.resolve() != source.resolve():
            target = numbered_output_path(target)
        if not source.is_file():
            raise WorkflowError("RENAME_SOURCE_MISSING", f"Rename source missing: {source}")
        if target.exists() and target.resolve() != source.resolve():
            raise WorkflowError("RENAME_TARGET_EXISTS", f"Rename target exists: {target}", "DECISION_REQUIRED")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = read_ass_text(source)
        write_ass_text(target, text)
        if source.resolve() != target.resolve():
            source.unlink()
        if target != planned_target:
            for remux_job in manifest.get("plan", {}).get("remuxJobs", []):
                remux_job["arguments"] = [
                    str(target) if os.path.normcase(str(value)) == os.path.normcase(str(planned_target)) else value
                    for value in remux_job.get("arguments", [])
                ]
            package = manifest.get("plan", {}).get("package") or {}
            for entry in package.get("entries", []):
                if os.path.normcase(str(entry.get("source", ""))) != os.path.normcase(str(planned_target)):
                    continue
                entry["source"] = str(target)
                arcname = Path(str(entry.get("arcname") or "").replace("\\", "/"))
                entry["arcname"] = (arcname.parent / target.name).as_posix()
        job["target"] = str(target)
        completed.append({"source": str(source), "target": str(target), "signature": file_signature(target)})
    manifest["renameResults"] = completed
    return {"status": "COMPLETE", "files": completed, "stage": {"files": len(completed)}}
