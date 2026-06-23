from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from . import SCHEMA_VERSION, TOOL_VERSION
from .analysis import cache_key, compare_waveforms, infer_roles, probe, select_signals, signal_value, suggest_paths
from .authority import authority_diagnostics, authority_fingerprint, build_authority
from .project import SourceManifest, infer_top, module_candidates, resolve_waveform, source_manifest, waveform_candidates
from .vcd import parse_time
from .wave import open_waveform, pywellen_diagnostics


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _add_wave_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--waveform", type=Path)
    parser.add_argument("--out-dir", type=Path)


def _add_source_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--filelist", type=Path, action="append", default=[])
    parser.add_argument("--include", type=Path, action="append", default=[])
    parser.add_argument("--define", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--top")


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path, list[Path]]:
    workspace = args.workspace.resolve()
    waveform, candidates = resolve_waveform(workspace, args.waveform)
    assert waveform is not None
    output = args.out_dir or Path("build/wave-debug")
    output = output if output.is_absolute() else workspace / output
    return workspace, waveform, output, candidates


def _manifest(args: argparse.Namespace, workspace: Path) -> SourceManifest:
    return source_manifest(
        workspace,
        getattr(args, "source_root", None),
        getattr(args, "source", []),
        getattr(args, "filelist", []),
        getattr(args, "include", []),
        getattr(args, "define", []),
        getattr(args, "exclude", []),
    )


def _top(args: argparse.Namespace, manifest: SourceManifest, wave: Any, waveform: Path, required: bool) -> tuple[str | None, list[str]]:
    candidates = module_candidates(manifest.files)
    root_scopes = {scope.local_name for scope in wave.header.scopes if scope.parent is None}
    selected = args.top or infer_top(candidates, root_scopes, waveform)
    if required and not selected:
        rendered = ", ".join(candidates) if candidates else "<none>"
        raise ValueError(f"top module is ambiguous; pass --top (candidates: {rendered})")
    return selected, candidates


def _top_selection(args: argparse.Namespace, candidates: list[str], selected: str | None) -> str:
    if args.top:
        return "explicit --top"
    if selected is None:
        return "none: pass --top when source/elaboration needs a top"
    if len(candidates) == 1:
        return "only discovered source top candidate"
    return "unique discovered source top candidate matching a waveform root scope"


def _authority_db(args: argparse.Namespace, workspace: Path, output: Path, top: str | None) -> Path | None:
    explicit = getattr(args, "authority_db", None)
    if explicit:
        return explicit if explicit.is_absolute() else workspace / explicit
    if top:
        candidate = output / "authority" / top / "rtl_authority.sqlite3"
        if candidate.is_file():
            return candidate
    return None


def _doctor(as_json: bool) -> int:
    root = skill_root()
    pywellen = pywellen_diagnostics(root)
    result = {
        "schema_version": SCHEMA_VERSION,
        "python": {"version": sys.version.split()[0], "supported_vcd": sys.version_info >= (3, 10)},
        "capabilities": {
            "vcd": {"available": True, "backend": "python-vcd"},
            "fst_direct": pywellen,
            "fst_conversion": {"available": shutil.which("fst2vcd") is not None, "path": shutil.which("fst2vcd")},
            "rtl_authority": authority_diagnostics(),
        },
        "ready": sys.version_info >= (3, 10),
        "remediation": [],
    }
    if not pywellen["available"] and shutil.which("fst2vcd") is None:
        result["remediation"].append("install a compatible pywellen or fst2vcd to read FST waveforms")
    if as_json:
        print(_json(result), end="")
    else:
        print(f"Python: {result['python']['version']} (portable VCD: yes)")
        for name, capability in result["capabilities"].items():
            print(f"{name}: {'available' if capability['available'] else 'unavailable'}")
        for item in result["remediation"]:
            print(f"remediation: {item}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    waveform, candidates = resolve_waveform(workspace, args.waveform, allow_ambiguous=True)
    output = args.out_dir or Path("build/wave-debug")
    output = output if output.is_absolute() else workspace / output
    candidate_rows = waveform_candidates(candidates)
    if waveform is None:
        result = {
            "schema_version": SCHEMA_VERSION,
            "workspace": str(workspace),
            "waveform": {
                "selected": None,
                "selection": "explicit --waveform required: multiple candidates found",
                "candidates": candidate_rows,
            },
            "output_dir": str(output),
            "next_command": "rerun the failed test, then pass its new waveform with --waveform PATH",
        }
        if args.json:
            print(_json(result), end="")
        else:
            print("waveform: <none; explicit selection required>")
            for row in candidate_rows:
                print(f"candidate: {row['path']}  modified={row['modified_at']}  size={row['size_bytes']}")
            print(f"next: {result['next_command']}")
        return 0
    wave = open_waveform(waveform, skill_root(), output)
    manifest = _manifest(args, workspace)
    selected_top, top_candidates = _top(args, manifest, wave, waveform, False)
    result = {
        "schema_version": SCHEMA_VERSION,
        "workspace": str(workspace),
        "waveform": {
            "selected": str(waveform),
            "selection": "explicit --waveform" if args.waveform else "only waveform candidate",
            "format": wave.format,
            "backend": wave.backend,
            "candidates": candidate_rows,
        },
        "timescale": wave.header.timescale.as_dict(),
        "hierarchy": {"scope_count": len(wave.header.scopes), "signal_count": len(wave.header.signals)},
        "source": {
            "files": len(manifest.files),
            "filelists": [str(path) for path in manifest.filelists],
            "include_dirs": [str(path) for path in manifest.include_dirs],
            "defines": manifest.defines,
        },
        "top_candidates": top_candidates,
        "selected_top": selected_top,
        "top_selection": _top_selection(args, top_candidates, selected_top),
        "role_candidates": infer_roles(wave.header.signals),
        "output_dir": str(output),
    }
    if args.json:
        print(_json(result), end="")
    else:
        print(f"workspace: {workspace}")
        print(f"waveform: {waveform}")
        print(f"waveform-format: {wave.format}")
        print(f"waveform-backend: {wave.backend}")
        print(f"waveform-selection: {result['waveform']['selection']}")
        print(f"waveform-candidates: {len(candidates)}")
        print(f"timescale: {wave.header.timescale.factor}{wave.header.timescale.unit}")
        print(f"scopes: {len(wave.header.scopes)}")
        print(f"signals: {len(wave.header.signals)}")
        print(f"hdl-files: {len(manifest.files)}")
        print(f"top-candidates: {', '.join(top_candidates) if top_candidates else '<none>'}")
        print(f"selected-top: {selected_top or '<ambiguous>'}")
        print(f"top-selection: {result['top_selection']}")
        print(f"output-dir: {output}")
    return 0


def _scopes(args: argparse.Namespace) -> int:
    _workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    scope = args.scope[4:] if args.scope and args.scope.startswith("TOP.") else args.scope
    rows = [
        item.as_dict() for item in wave.header.scopes
        if (not scope or item.path == scope or item.path.startswith(scope + "."))
        and (not args.match or all(term.lower() in item.path.lower() for term in args.match))
    ]
    suggestions = suggest_paths(scope or (args.match[-1] if args.match else None), (item.path for item in wave.header.scopes)) if not rows else []
    if args.json:
        print(_json({"schema_version": SCHEMA_VERSION, "waveform": str(waveform), "scopes": rows, "suggestions": suggestions}), end="")
    else:
        for row in rows:
            print(f"{row['path']} ({row['kind']})")
        if suggestions:
            print("no exact hierarchy match; suggestions: " + ", ".join(suggestions))
    return 0


def _signals(args: argparse.Namespace) -> int:
    _workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    selected, truncated = select_signals(
        wave.header.signals, scope=args.scope, matches=args.match, regexes=args.regex, limit=args.limit,
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "waveform": str(waveform),
        "count": len(selected),
        "truncated": truncated,
        "matching": {
            "match": "case-insensitive literal substring; repeated --match terms are ANDed",
            "regex": "case-insensitive regular expression; repeated --regex terms are ANDed",
        },
        "suggestions": suggest_paths(args.scope or ((args.match or args.regex)[-1] if args.match or args.regex else None), (signal.path for signal in wave.header.signals)) if not selected else [],
        "signals": [signal.as_dict() for signal in selected],
    }
    if args.json:
        print(_json(result), end="")
    else:
        for signal in selected:
            print(f"{signal.path} width={signal.width} kind={signal.kind}")
        if truncated:
            print(f"... truncated at --limit {args.limit}")
        elif not selected and result["suggestions"]:
            print("no signals matched; hierarchy suggestions: " + ", ".join(result["suggestions"]))
    return 0


def _resolve_signal(wave: Any, path: str):
    normalized = path[4:] if path.startswith("TOP.") else path
    matches = [signal for signal in wave.header.signals if signal.path == path or signal.path == normalized or signal.path == f"TOP.{normalized}"]
    if len(matches) != 1:
        raise ValueError(f"signal path must resolve exactly once: {path} (matches={len(matches)})")
    return matches[0]


def _signal(args: argparse.Namespace) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    timestamp = parse_time(args.time, wave.header.timescale)
    result = signal_value(
        wave, _resolve_signal(wave, args.signal_path), timestamp,
        _authority_db(args, workspace, output, None),
    )
    print(_json(result), end="")
    return 0


def _probe(args: argparse.Namespace, save_packet: bool = False) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    if args.around is not None:
        around = parse_time(args.around, wave.header.timescale)
        radius = parse_time(args.radius, wave.header.timescale)
        start, end = max(0, around - radius), around + radius
    else:
        start = parse_time(args.start, wave.header.timescale)
        end = parse_time(args.end, wave.header.timescale)
    explicit = list(args.signal_path)
    if args.clock and args.clock not in explicit:
        explicit.append(args.clock)
    selected, signal_truncated = select_signals(
        wave.header.signals, scope=args.scope, matches=args.match, regexes=args.regex,
        paths=explicit, limit=args.max_signals,
    )
    if not selected:
        raise ValueError("probe selected no signals; use `signals` to discover paths")
    manifest = _manifest(args, workspace) if hasattr(args, "source") else SourceManifest([], [], [], [])
    top, top_candidates = _top(args, manifest, wave, waveform, False) if hasattr(args, "top") else (None, [])
    database = _authority_db(args, workspace, output, top)
    result = probe(wave, selected, start, end, args.max_changes, database, args.clock, args.edge)
    result["truncated"] = bool(result["truncated"] or signal_truncated)
    result["provenance"] = {
        "source_file_count": len(manifest.files),
        "source_files": [str(path) for path in manifest.files[:20]],
        "source_files_truncated": len(manifest.files) > 20,
        "include_dirs": [str(path) for path in manifest.include_dirs],
        "defines": manifest.defines,
        "top": top,
        "top_selection": _top_selection(args, top_candidates, top),
        "authority": authority_fingerprint(database),
    }
    if save_packet:
        source_identity = []
        for path in manifest.files:
            source_identity.append(
                {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
        identity = cache_key(
            {
                "waveform": str(waveform.resolve()), "size": waveform.stat().st_size,
                "mtime_ns": waveform.stat().st_mtime_ns, "top": top, "scope": args.scope,
                "start": start, "end": end, "schema": SCHEMA_VERSION,
                "sources": source_identity, "authority": authority_fingerprint(database),
            }
        )
        path = output / "packets" / identity / f"packet_{start}_{end}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json(result), encoding="utf-8")
        print(path)
    elif args.format == "json":
        print(_json(result), end="")
    else:
        if args.view in {"changes", "both"}:
            for change in result["changes"]:
                print(f"{change['time']['display']:>10}  {change['signal']} = {change['value']}")
        if args.view in {"snapshots", "both"}:
            samples = result["clock_samples"]
            if not args.clock:
                raise ValueError("--view snapshots requires --clock")
            if args.view == "both" and samples and result["changes"]:
                print()
            for sample in samples:
                values = sample["values"]
                rendered = "  ".join(f"{path}={values.get(path, '?')}" for path in sorted(values))
                print(f"{sample['time']['display']:>10}  {rendered}")
        if result["truncated"]:
            print("... output truncated; narrow the query")
    return 0


def _authority(args: argparse.Namespace) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    manifest = _manifest(args, workspace)
    if not manifest.files:
        raise ValueError("authority requires Verilog/SystemVerilog source")
    top, _ = _top(args, manifest, wave, waveform, True)
    assert top is not None
    destination = output / "authority" / top
    build_authority(
        manifest.files, top, destination, args.force, args.authority_backend,
        manifest.include_dirs, manifest.defines,
    )
    print(destination)
    return 0


def _compare(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    output = args.out_dir or workspace / "build/wave-debug"
    output = output if output.is_absolute() else workspace / output
    good_path = args.good if args.good.is_absolute() else workspace / args.good
    bad_path = args.bad if args.bad.is_absolute() else workspace / args.bad
    good = open_waveform(good_path.resolve(), skill_root(), output)
    bad = open_waveform(bad_path.resolve(), skill_root(), output)
    result = compare_waveforms(good, bad, args.scope, args.match, args.limit, args.regex)
    print(_json(result), end="")
    return 0


def _probe_parser(sub: Any, name: str, help_text: str, window_required: bool = True) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    _add_wave_inputs(parser)
    _add_source_inputs(parser)
    parser.add_argument("--authority-db", type=Path)
    window = parser.add_mutually_exclusive_group(required=window_required)
    window.add_argument("--around")
    window.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--radius", default="100")
    parser.add_argument("--scope", "--focus-scope", dest="scope")
    parser.add_argument("--match", action="append", default=[], help="case-insensitive literal substring (repeat to AND)")
    parser.add_argument("--regex", action="append", default=[], help="case-insensitive regular expression (repeat to AND)")
    parser.add_argument("--signal", dest="signal_path", action="append", default=[])
    parser.add_argument("--clock")
    parser.add_argument("--edge", choices=("rising", "falling", "both"), default="rising")
    parser.add_argument("--max-signals", type=int, default=64)
    parser.add_argument("--max-changes", type=int, default=200)
    parser.add_argument("--format", choices=("json", "table"), default="json")
    parser.add_argument(
        "--view", choices=("changes", "snapshots", "both"), default="changes",
        help="table output: change log, post-edge snapshots, or both (snapshots require --clock)",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigate FST/VCD waveforms with Verilog/SystemVerilog RTL.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="check waveform and RTL analysis capabilities")
    doctor.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="discover inputs and summarize waveform metadata")
    _add_wave_inputs(inspect)
    _add_source_inputs(inspect)
    inspect.add_argument("--json", action="store_true")
    scopes = sub.add_parser("scopes", help="list waveform scopes")
    _add_wave_inputs(scopes)
    scopes.add_argument("--scope")
    scopes.add_argument("--match", action="append", default=[], help="case-insensitive literal substring (repeat to AND)")
    scopes.add_argument("--json", action="store_true")
    signals = sub.add_parser("signals", help="discover waveform signals")
    _add_wave_inputs(signals)
    signals.add_argument("--scope")
    signals.add_argument("--match", action="append", default=[], help="case-insensitive literal substring (repeat to AND)")
    signals.add_argument("--regex", action="append", default=[], help="case-insensitive regular expression (repeat to AND)")
    signals.add_argument("--limit", type=int, default=100)
    signals.add_argument("--json", action="store_true")
    authority = sub.add_parser("authority", help="build elaborated RTL ownership when available, otherwise static candidates")
    _add_wave_inputs(authority)
    _add_source_inputs(authority)
    authority.add_argument("--force", action="store_true")
    authority.add_argument("--authority-backend", choices=("auto", "verilator", "static"), default="auto")
    signal = sub.add_parser("signal", help="query one signal at a timestamp")
    _add_wave_inputs(signal)
    signal.add_argument("--signal", required=True, dest="signal_path")
    signal.add_argument("--time", required=True)
    signal.add_argument("--window-len", help=argparse.SUPPRESS)
    signal.add_argument("--authority-db", type=Path)
    _probe_parser(sub, "probe", "query a compact bounded evidence window")
    packet = _probe_parser(sub, "packet", "write a compact evidence packet", window_required=False)
    packet.add_argument("--window", type=int)
    packet.add_argument("--window-len")
    compare = sub.add_parser("compare", help="find first divergence between two waveforms")
    compare.add_argument("good", type=Path)
    compare.add_argument("bad", type=Path)
    compare.add_argument("--workspace", type=Path, default=Path.cwd())
    compare.add_argument("--out-dir", type=Path)
    compare.add_argument("--scope")
    compare.add_argument("--match", action="append", default=[])
    compare.add_argument("--regex", action="append", default=[])
    compare.add_argument("--limit", type=int, default=64)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args.json)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "scopes":
            return _scopes(args)
        if args.command == "signals":
            return _signals(args)
        if args.command == "authority":
            return _authority(args)
        if args.command == "signal":
            return _signal(args)
        if args.command == "probe":
            if args.start is not None and args.end is None:
                raise ValueError("--start requires --end")
            return _probe(args)
        if args.command == "packet":
            if args.window is not None:
                if args.window_len is None:
                    raise ValueError("--window requires --window-len")
                _workspace, waveform, output, _ = _paths(args)
                wave = open_waveform(waveform, skill_root(), output)
                length = parse_time(args.window_len, wave.header.timescale)
                args.start, args.end, args.around = str(args.window * length), str((args.window + 1) * length - 1), None
            elif args.start is None and args.around is None:
                raise ValueError("packet requires --window/--window-len, --start/--end, or --around")
            elif args.start is not None and args.end is None:
                raise ValueError("--start requires --end")
            return _probe(args, True)
        if args.command == "compare":
            return _compare(args)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0
