from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import re

from .rtl_authority import authority_cache_matches, authority_identity, parse_modules, write_authority_artifacts


VERILATOR_BACKEND = "verilator-json-elaboration"
VERILATOR_MATCH_STATUS = "exact"
VERILATOR_LIMITATIONS = (
    "authority reflects Verilator's accepted elaborated design for the supplied sources, -I paths, and -D definitions",
    "waveform hierarchy can still differ when the simulator applies different elaboration options or VPI visibility rules",
)


def verilator_diagnostics() -> dict[str, object]:
    executable = shutil.which("verilator")
    result: dict[str, object] = {"installed": executable is not None, "available": False, "path": executable}
    if executable:
        version = subprocess.run([executable, "--version"], text=True, capture_output=True, check=False)
        result["version"] = (version.stdout or version.stderr).strip()
        help_output = subprocess.run([executable, "--help"], text=True, capture_output=True, check=False)
        result["available"] = "--json-only" in (help_output.stdout + help_output.stderr)
    return result


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _address_index(netlist: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["addr"]: item for item in _walk(netlist)
        if isinstance(item.get("addr"), str)
    }


def _dtype_width(variable: dict[str, Any], addresses: dict[str, dict[str, Any]]) -> int | None:
    """Follow Verilator dtype links; scalar types intentionally resolve to one bit."""
    current = variable
    seen: set[str] = set()
    while True:
        for key in ("width", "widthMin"):
            value = current.get(key)
            if isinstance(value, int) and value > 0:
                return value
        range_text = current.get("range")
        if isinstance(range_text, str):
            match = re.fullmatch(r"\s*(-?\d+)\s*:\s*(-?\d+)\s*", range_text)
            if match:
                return abs(int(match.group(1)) - int(match.group(2))) + 1
        pointer = current.get("dtypep")
        if not isinstance(pointer, str) or pointer in seen or pointer not in addresses:
            return 1 if variable.get("dtypeName") else None
        seen.add(pointer)
        current = addresses[pointer]


def _module_cells(module: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _walk(module) if item.get("type") == "CELL"]


def _module_variables(module: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in module.get("stmtsp", []):
        if not isinstance(item, dict) or item.get("type") != "VAR":
            continue
        if item.get("varType") in {"PARAM", "LPARAM"} or str(item.get("name", "")).startswith("__V"):
            continue
        rows.append(item)
    return rows


def _elaborated_rows(netlist: dict[str, Any], files: list[Path], top: str) -> list[dict[str, object]]:
    addresses = _address_index(netlist)
    modules = {
        item["addr"]: item for item in _walk(netlist)
        if item.get("type") == "MODULE" and isinstance(item.get("addr"), str)
    }
    top_module = next(
        (module for module in modules.values() if module.get("name") == top and module.get("level") == 1), None
    )
    if top_module is None:
        raise RuntimeError(f"Verilator did not elaborate requested top module: {top}")
    static_modules = parse_modules(files)
    rows: list[dict[str, object]] = []

    def visit(module: dict[str, Any], instance_path: str, ancestry: tuple[str, ...]) -> None:
        module_name = str(module.get("origName") or module.get("name"))
        if module_name in ancestry:
            return
        source_file = static_modules.get(module_name).source_file if module_name in static_modules else None
        for variable in _module_variables(module):
            name = str(variable.get("origName") or variable.get("name"))
            if not name:
                continue
            direction = variable.get("direction")
            rows.append({
                "full_signal_name": f"{instance_path}.{name}",
                "module_type": module_name,
                "instance_path": instance_path,
                "local_signal_name": name,
                "signal_kind": variable.get("dtypeName") or variable.get("varType"),
                "direction": None if direction in {None, "NONE"} else str(direction).lower(),
                "decl_width_bits": _dtype_width(variable, addresses),
                "source_file": source_file,
                "provenance": VERILATOR_BACKEND,
                "match_status": VERILATOR_MATCH_STATUS,
                "confidence": "high",
            })
        for cell in _module_cells(module):
            child = modules.get(cell.get("modp"))
            name = cell.get("origName") or cell.get("name")
            if child is not None and isinstance(name, str) and name:
                visit(child, f"{instance_path}.{name}", ancestry + (module_name,))

    visit(top_module, top, ())
    return rows


def build_verilator_authority(
    files: list[Path], top: str, output: Path, force: bool = False,
    include_dirs: list[Path] | None = None, defines: list[str] | None = None, parameters: list[str] | None = None,
) -> None:
    diagnostics = verilator_diagnostics()
    executable = diagnostics.get("path")
    if not isinstance(executable, str) or not diagnostics["available"]:
        raise RuntimeError("Verilator JSON elaboration is unavailable; upgrade Verilator or use --authority-backend static")
    identity = authority_identity(files, top, VERILATOR_BACKEND, include_dirs, defines, parameters)
    # Do not invoke the compiler when an identical, complete authority cache already exists.
    if not force and authority_cache_matches(output, identity):
        return
    with tempfile.TemporaryDirectory(prefix="wave-debug-verilator-") as temporary:
        json_path = Path(temporary) / "elaborated.tree.json"
        command = [
            executable, "--json-only", "--json-only-output", str(json_path), "--top-module", top,
            "--Mdir", str(Path(temporary) / "obj_dir"), "-Wno-fatal",
        ]
        command.extend(f"-I{path}" for path in include_dirs or [])
        command.extend(f"-D{definition}" for definition in defines or [])
        command.extend(f"-G{parameter}" for parameter in parameters or [])
        command.extend(str(path) for path in files)
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode or not json_path.is_file():
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise RuntimeError("Verilator elaboration failed: " + (detail[-1] if detail else "no JSON output"))
        netlist = json.loads(json_path.read_text(encoding="utf-8"))
    rows = _elaborated_rows(netlist, files, top)
    write_authority_artifacts(
        rows, top, output, identity,
        {
            "backend": VERILATOR_BACKEND,
            "match_status": VERILATOR_MATCH_STATUS,
            "limitations": list(VERILATOR_LIMITATIONS),
            "tool": {"path": executable, "version": diagnostics.get("version")},
            "parameter_overrides": list(parameters or []),
        },
        force,
    )
