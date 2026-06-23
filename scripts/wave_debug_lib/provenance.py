"""Portable simulation-context manifests for waveform evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .project import SourceManifest
from .wave import WaveBackend


PROVENANCE_SCHEMA_VERSION = "1.0"


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def build_provenance(
    wave: WaveBackend,
    manifest: SourceManifest,
    top: str | None,
    *,
    simulator: str | None = None,
    simulator_version: str | None = None,
    simulation_command: str | None = None,
    parameter_overrides: list[str] | None = None,
    failure_time: str | None = None,
    failure_label: str | None = None,
) -> dict[str, object]:
    source = wave.source_path or wave.path
    stat = source.stat()
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "waveform": {
            "path": str(source.resolve()),
            "analysis_path": str(wave.path.resolve()),
            "format": wave.format,
            "backend": wave.backend,
            "size_bytes": stat.st_size,
            "modified_at": _timestamp(stat.st_mtime),
            "mtime_ns": stat.st_mtime_ns,
            "timescale": wave.header.timescale.as_dict(),
        },
        "simulation": {
            "simulator": simulator,
            "simulator_version": simulator_version,
            "command": simulation_command,
        },
        "compilation": {
            "top": top,
            "source_files": [str(path) for path in manifest.files],
            "filelists": [str(path) for path in manifest.filelists],
            "include_dirs": [str(path) for path in manifest.include_dirs],
            "defines": manifest.defines,
            "parameter_overrides": parameter_overrides or [],
        },
        "failure": {"time": failure_time, "label": failure_label},
    }


def read_provenance(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError(f"unsupported provenance manifest: {path}")
    if not isinstance(payload.get("waveform"), dict):
        raise ValueError(f"provenance manifest has no waveform record: {path}")
    return payload


def write_provenance(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
