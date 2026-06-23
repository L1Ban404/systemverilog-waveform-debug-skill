from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
from typing import Iterator

from . import SCHEMA_VERSION
from .vcd import Header, Scope, Signal, Timescale, iter_changes, normalize_value, read_header


@dataclass
class WaveBackend:
    path: Path
    format: str
    backend: str
    header: Header
    waveform: object | None = None
    source_path: Path | None = None

    def changes(self, selected: list[Signal]) -> Iterator[tuple[int, str, str]]:
        if self.waveform is None:
            yield from iter_changes(self.path, {signal.id_code for signal in selected})
            return
        assert self.waveform is not None
        for signal in selected:
            wave_signal = self.waveform.get_signal_from_path(signal.path)
            for timestamp, value in wave_signal.all_changes():
                if isinstance(value, int):
                    rendered = format(value, f"0{signal.width}b") if signal.width > 1 else str(value)
                else:
                    rendered = str(value)
                yield int(timestamp), signal.id_code, normalize_value(rendered, signal.width)


def _load_pywellen(skill_root: Path):
    if os.environ.get("SV_WAVE_DEBUG_DISABLE_PYWELLEN", "").lower() in {"1", "true", "yes", "on"}:
        return None
    vendored = skill_root / "third_party/pywellen"
    sys.path.insert(0, str(vendored))
    try:
        return importlib.import_module("pywellen")
    except (ImportError, OSError):
        sys.modules.pop("pywellen", None)
        try:
            sys.path.remove(str(vendored))
        except ValueError:
            pass
        try:
            return importlib.import_module("pywellen")
        except (ImportError, OSError):
            return None


def pywellen_available(skill_root: Path) -> tuple[bool, str | None]:
    diagnostics = pywellen_diagnostics(skill_root)
    error = diagnostics.get("error")
    return bool(diagnostics["available"]), str(error) if error is not None else None


def pywellen_diagnostics(skill_root: Path) -> dict[str, object]:
    try:
        module = _load_pywellen(skill_root)
    except Exception as error:  # pragma: no cover - defensive ABI diagnostic
        module = None
        load_error: str | None = str(error)
    else:
        load_error = None if module is not None else "module not importable"
    module_file = Path(str(getattr(module, "__file__", ""))).resolve() if module is not None else None
    bundled_root = (skill_root / "third_party/pywellen").resolve()
    bundled = bool(module_file and (module_file == bundled_root or bundled_root in module_file.parents))
    native_files = sorted(str(path) for path in module_file.parent.glob("*.so")) if module_file else []
    return {
        "available": module is not None,
        "backend": "pywellen",
        "source": "bundled" if bundled else "installed" if module is not None else None,
        "module_path": str(module_file) if module_file else None,
        "native_extensions": native_files,
        "runtime": {
            "implementation": sys.implementation.name,
            "python": platform.python_version(),
            "soabi": sysconfig.get_config_var("SOABI"),
            "machine": platform.machine(),
            "platform": sys.platform,
        },
        "error": load_error,
    }


def _pywellen_can_open(path: Path, module: object) -> bool:
    if os.environ.get("SV_WAVE_DEBUG_DISABLE_PYWELLEN", "").lower() in {"1", "true", "yes", "on"}:
        return False
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return False
    module_root = Path(str(module_file)).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(module_root), env.get("PYTHONPATH"))))
    probe = subprocess.run(
        [sys.executable, "-c", "import pywellen,sys; pywellen.Waveform(path=sys.argv[1])", str(path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0


def _header_from_wellen(waveform: object) -> Header:
    hierarchy = waveform.hierarchy
    scopes: list[Scope] = []
    signals: list[Signal] = []
    counter = 0

    def visit(scope: object, parent: str | None) -> None:
        nonlocal counter
        path = scope.full_name(hierarchy)
        scopes.append(Scope(path, scope.name(hierarchy), scope.scope_type(), parent))
        for var in scope.vars(hierarchy):
            width = int(var.bitwidth() or 1)
            signals.append(
                Signal(var.full_name(hierarchy), var.name(hierarchy), path, f"w{counter}", width, var.var_type())
            )
            counter += 1
        for child in scope.scopes(hierarchy):
            visit(child, path)

    for top_scope in hierarchy.top_scopes():
        visit(top_scope, None)
    raw_timescale = hierarchy.timescale()
    if raw_timescale is None:
        timescale = Timescale(1, "ns")
    else:
        unit = str(raw_timescale.unit)
        timescale = Timescale(int(raw_timescale.factor), unit if unit in {"s", "ms", "us", "ns", "ps", "fs"} else "ns")
    return Header(timescale, tuple(scopes), tuple(signals))


def _converted_fst(source: Path, output_root: Path) -> Path | None:
    converter = shutil.which("fst2vcd")
    if converter is None:
        return None
    identity = hashlib.sha256(
        f"{source.resolve()}:{source.stat().st_size}:{source.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    converted = output_root / "converted" / f"{source.stem}-{identity}.vcd"
    if converted.is_file() and converted.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return converted
    converted.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{converted}.{os.getpid()}.tmp")
    with temporary.open("wb") as output:
        subprocess.run([converter, str(source)], stdout=output, check=True)
    temporary.replace(converted)
    return converted


def _cached_vcd_header(path: Path, output_root: Path) -> Header:
    stat = path.stat()
    identity = {
        "schema": SCHEMA_VERSION,
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    cache = output_root / "cache/waveform_meta" / digest / "header.json"
    if cache.is_file():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return Header(
            Timescale(**raw["timescale"]),
            tuple(Scope(**row) for row in raw["scopes"]),
            tuple(Signal(**row) for row in raw["signals"]),
        )
    header = read_header(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
            {
                "identity": identity,
                "timescale": {"factor": header.timescale.factor, "unit": header.timescale.unit},
                "scopes": [
                    {"path": row.path, "local_name": row.local_name, "kind": row.kind, "parent": row.parent}
                    for row in header.scopes
                ],
                "signals": [
                    {
                        "path": row.path, "local_name": row.local_name, "scope": row.scope,
                        "id_code": row.id_code, "width": row.width, "kind": row.kind,
                    }
                    for row in header.signals
                ],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    temporary = Path(f"{cache}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(cache)
    return header


def open_waveform(path: Path, skill_root: Path, output_root: Path) -> WaveBackend:
    suffix = path.suffix.lower()
    if suffix == ".vcd":
        return WaveBackend(path, "vcd", "python-vcd", _cached_vcd_header(path, output_root), source_path=path)
    if suffix != ".fst":
        raise ValueError(f"unsupported waveform format: {path}")
    pywellen = _load_pywellen(skill_root)
    if pywellen is not None and _pywellen_can_open(path, pywellen):
        try:
            waveform = pywellen.Waveform(path=str(path))
            return WaveBackend(path, "fst", "pywellen", _header_from_wellen(waveform), waveform, path)
        except BaseException as error:  # PyO3 PanicException is intentionally outside Exception.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
    converted = _converted_fst(path, output_root)
    if converted is not None:
        return WaveBackend(converted, "fst", "fst2vcd+python-vcd", _cached_vcd_header(converted, output_root), source_path=path)
    raise RuntimeError(
        "FST cannot be read: install a compatible pywellen or fst2vcd; run `wave_debug.py doctor` for details"
    )
