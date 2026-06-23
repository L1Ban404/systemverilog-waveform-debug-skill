from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from .rtl_authority import (
    AUTHORITY_BACKEND,
    AUTHORITY_LIMITATIONS,
    AUTHORITY_MATCH_STATUS,
    build_rtl_authority,
)
from .elaboration import VERILATOR_BACKEND, VERILATOR_MATCH_STATUS, build_verilator_authority, verilator_diagnostics


def mapping_confidence(match_status: str) -> str:
    """Stable user-facing authority tier, independent of backend internals."""
    return {
        "exact": "elaborated-exact",
        "static-source-match": "static-candidate",
    }.get(match_status, "heuristic-context")


def build_authority(
    files: list[Path], top: str, output: Path, force: bool, backend: str = "auto",
    include_dirs: list[Path] | None = None, defines: list[str] | None = None, parameters: list[str] | None = None,
) -> str:
    if backend not in {"auto", "verilator", "static"}:
        raise ValueError(f"unsupported authority backend: {backend}")
    if backend in {"auto", "verilator"}:
        try:
            build_verilator_authority(files, top, output, force, include_dirs, defines, parameters)
            return VERILATOR_BACKEND
        except RuntimeError:
            if backend == "verilator":
                raise
    build_rtl_authority(files, top, output, force, include_dirs, defines, parameters)
    return AUTHORITY_BACKEND


def authority_diagnostics() -> dict[str, object]:
    return {
        "available": True,
        "default_backend": "auto",
        "backends": {
            "static": {
                "available": True, "backend": AUTHORITY_BACKEND,
                "match_status": AUTHORITY_MATCH_STATUS, "exact": False,
                "limitations": list(AUTHORITY_LIMITATIONS),
            },
            "verilator": {
                **verilator_diagnostics(), "backend": VERILATOR_BACKEND,
                "match_status": VERILATOR_MATCH_STATUS, "exact": True,
            },
        },
    }


def _source_context(source_file: str | None, local_name: str | None, limit: int = 6) -> list[dict[str, object]]:
    if not source_file or not local_name:
        return []
    path = Path(source_file)
    if not path.is_file():
        return []
    pattern = re.compile(rf"\b{re.escape(local_name)}\b")
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if pattern.search(line):
            rows.append({"file": str(path), "line": line_number, "text": line.strip(), "provenance": "heuristic-text-match"})
            if len(rows) >= limit:
                break
    return rows


def lookup_authority(database: Path | None, paths: list[str]) -> dict[str, dict[str, object]]:
    if database is None or not database.is_file() or not paths:
        return {}
    candidates = list(dict.fromkeys(paths + [path[4:] for path in paths if path.startswith("TOP.")]))
    placeholders = ",".join("?" for _ in candidates)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("pragma table_info(authority_lookup)")}
        selected = [
            name for name in (
                "full_signal_name", "module_type", "instance_path", "local_signal_name",
                "signal_kind", "direction", "decl_width_bits", "source_file", "provenance",
                "match_status", "confidence",
            ) if name in columns
        ]
        rows = connection.execute(
            f"select {', '.join(selected)} from authority_lookup where full_signal_name in ({placeholders})",
            candidates,
        ).fetchall()
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        item = dict(row)
        item.setdefault("match_status", "legacy-authority")
        item.setdefault("confidence", "unknown")
        item["mapping_confidence"] = mapping_confidence(str(item["match_status"]))
        item["source_context"] = _source_context(item.get("source_file"), item.get("local_signal_name"))
        result[str(item["full_signal_name"])] = item
        result[f"TOP.{item['full_signal_name']}"] = item
    return result


def authority_fingerprint(database: Path | None) -> dict[str, object] | None:
    if database is None or not database.is_file():
        return None
    stat = database.stat()
    result: dict[str, object] = {
        "path": str(database.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
    }
    try:
        with sqlite3.connect(database) as connection:
            metadata = dict(connection.execute("select key, value from authority_metadata"))
        result.update({name: metadata[name] for name in ("schema_version", "backend", "match_status") if name in metadata})
        if "limitations" in metadata:
            result["limitations"] = json.loads(metadata["limitations"])
    except (sqlite3.Error, json.JSONDecodeError):
        result["schema_version"] = "legacy"
    return result
