from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex


HDL_SUFFIXES = {".sv", ".v"}
WAVE_SUFFIXES = {".fst", ".vcd"}
DEFAULT_SKIP = {".git", ".codex", "node_modules", "wave-debug", "__pycache__"}


@dataclass
class SourceManifest:
    files: list[Path]
    include_dirs: list[Path]
    defines: list[str]
    filelists: list[Path]


def _skipped(path: Path, root: Path, excludes: list[str]) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part in DEFAULT_SKIP for part in relative.parts) or any(relative.match(pattern) for pattern in excludes)


def discover_files(root: Path, suffixes: set[str], excludes: list[str] | None = None) -> list[Path]:
    excludes = excludes or []
    return sorted(
        (
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes and not _skipped(path, root, excludes)
        ),
        key=lambda path: (-path.stat().st_mtime_ns, str(path)),
    )


def waveform_candidates(paths: list[Path]) -> list[dict[str, object]]:
    """Return stable, user-facing provenance for discovered waveforms."""
    return [
        {
            "path": str(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "mtime_ns": path.stat().st_mtime_ns,
            "size_bytes": path.stat().st_size,
        }
        for path in paths
    ]


def render_waveform_candidates(paths: list[Path]) -> str:
    return "; ".join(
        f"{path} (modified {datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})"
        for path in paths
    )


def resolve_waveform(
    workspace: Path, explicit: Path | None, *, allow_ambiguous: bool = False,
) -> tuple[Path | None, list[Path]]:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else workspace / explicit
        if not path.is_file() or path.suffix.lower() not in WAVE_SUFFIXES:
            raise ValueError(f"waveform must be an existing .fst or .vcd file: {path}")
        return path.resolve(), [path.resolve()]
    candidates = discover_files(workspace, WAVE_SUFFIXES)
    if not candidates:
        raise ValueError(f"no .fst or .vcd waveform found under {workspace}")
    if len(candidates) == 1:
        return candidates[0], candidates
    if allow_ambiguous:
        return None, candidates
    raise ValueError(
        "multiple waveform candidates found; pass --waveform explicitly. Candidates: "
        + render_waveform_candidates(candidates)
    )


def _resolve_token(base: Path, token: str) -> Path:
    path = Path(os.path.expandvars(token)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def parse_filelist(path: Path, seen: set[Path] | None = None) -> SourceManifest:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return SourceManifest([], [], [], [])
    seen.add(path)
    files: list[Path] = []
    includes: list[Path] = []
    defines: list[str] = []
    filelists = [path]
    tokens = shlex.split(path.read_text(encoding="utf-8", errors="replace"), comments=True)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            child = parse_filelist(_resolve_token(path.parent, tokens[index + 1]), seen)
            files.extend(child.files)
            includes.extend(child.include_dirs)
            defines.extend(child.defines)
            filelists.extend(child.filelists)
            index += 2
            continue
        if token in {"-I", "-D", "-v"} and index + 1 < len(tokens):
            value = tokens[index + 1]
            if token == "-I":
                includes.append(_resolve_token(path.parent, value))
            elif token == "-D":
                defines.append(value)
            else:
                files.append(_resolve_token(path.parent, value))
            index += 2
            continue
        if token.startswith("+incdir+"):
            includes.extend(_resolve_token(path.parent, value) for value in token[len("+incdir+") :].split("+") if value)
        elif token.startswith("+define+"):
            defines.extend(value for value in token[len("+define+") :].split("+") if value)
        elif token.startswith("-I") and len(token) > 2:
            includes.append(_resolve_token(path.parent, token[2:]))
        elif token.startswith("-D") and len(token) > 2:
            defines.append(token[2:])
        elif Path(token).suffix.lower() in HDL_SUFFIXES:
            files.append(_resolve_token(path.parent, token))
        index += 1
    return SourceManifest(files, includes, defines, filelists)


def source_manifest(
    workspace: Path,
    source_root: Path | None,
    explicit_sources: list[Path],
    filelists: list[Path],
    include_dirs: list[Path],
    defines: list[str],
    excludes: list[str],
) -> SourceManifest:
    root = source_root or workspace
    root = root if root.is_absolute() else workspace / root
    files = [(_resolve_token(workspace, str(path))) for path in explicit_sources]
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise ValueError(f"source file does not exist: {missing[0]}")
    includes = [(_resolve_token(workspace, str(path))) for path in include_dirs]
    parsed_filelists: list[Path] = []
    parsed_defines = list(defines)
    for filelist in filelists:
        resolved = _resolve_token(workspace, str(filelist))
        parsed = parse_filelist(resolved)
        files.extend(parsed.files)
        includes.extend(parsed.include_dirs)
        parsed_defines.extend(parsed.defines)
        parsed_filelists.extend(parsed.filelists)
    if not files and root.is_dir():
        files = discover_files(root.resolve(), HDL_SUFFIXES, excludes)
    unique_files = list(dict.fromkeys(path for path in files if path.is_file()))
    return SourceManifest(unique_files, list(dict.fromkeys(includes)), list(dict.fromkeys(parsed_defines)), parsed_filelists)


def module_candidates(files: list[Path]) -> list[str]:
    declared: set[str] = set()
    instantiated: set[str] = set()
    module_re = re.compile(r"\bmodule\s+(?:automatic\s+)?([A-Za-z_][A-Za-z0-9_$]*)")
    instance_re = re.compile(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\([^;]*?\)\s*)?"
        r"[A-Za-z_][A-Za-z0-9_$]*\s*\(",
        re.DOTALL,
    )
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"//.*", " ", text)
        declared.update(module_re.findall(text))
        instantiated.update(match.group(1) for match in instance_re.finditer(text))
    roots = sorted(declared - instantiated)
    preferred = [name for name in roots if re.search(r"(?:^|_)(?:tb|test|top)$", name, re.I)]
    return preferred or roots


def infer_top(candidates: list[str], root_scopes: set[str], waveform: Path) -> str | None:
    matches = sorted(set(candidates) & root_scopes)
    if len(matches) == 1:
        return matches[0]
    path_text = waveform.as_posix().lower()
    scored: list[tuple[int, str]] = []
    for candidate in candidates:
        lowered = candidate.lower()
        stems = {lowered, re.sub(r"(?:_?(?:tb|test|top))$", "", lowered)}
        score = max((len(stem) for stem in stems if stem and stem in path_text), default=0)
        if score:
            scored.append((score, candidate))
    if scored:
        best = max(score for score, _ in scored)
        winners = sorted(name for score, name in scored if score == best)
        if len(winners) == 1:
            return winners[0]
    return candidates[0] if len(candidates) == 1 else None
