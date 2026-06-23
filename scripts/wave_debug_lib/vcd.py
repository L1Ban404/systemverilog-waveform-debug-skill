from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import re
from typing import Iterable, Iterator


UNIT_FS = {
    "s": 10**15,
    "ms": 10**12,
    "us": 10**9,
    "ns": 10**6,
    "ps": 10**3,
    "fs": 1,
}


@dataclass(frozen=True)
class Timescale:
    factor: int
    unit: str

    @property
    def tick_fs(self) -> int:
        return self.factor * UNIT_FS[self.unit]

    def as_dict(self) -> dict[str, int | str]:
        return {"factor": self.factor, "unit": self.unit, "tick_fs": self.tick_fs}


@dataclass(frozen=True)
class Scope:
    path: str
    local_name: str
    kind: str
    parent: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "local_name": self.local_name,
            "kind": self.kind,
            "parent": self.parent,
        }


@dataclass(frozen=True)
class Signal:
    path: str
    local_name: str
    scope: str
    id_code: str
    width: int
    kind: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "local_name": self.local_name,
            "scope": self.scope,
            "id_code": self.id_code,
            "width": self.width,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Header:
    timescale: Timescale
    scopes: tuple[Scope, ...]
    signals: tuple[Signal, ...]


def _directives(lines: Iterable[str]) -> Iterator[str]:
    pending: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if pending:
            pending.append(stripped)
            if "$end" in stripped:
                yield " ".join(pending)
                pending.clear()
            continue
        if stripped.startswith("$") and "$end" not in stripped:
            pending.append(stripped)
            continue
        yield stripped
        if "$enddefinitions" in stripped:
            return


def read_header(path: Path) -> Header:
    stack: list[str] = []
    scopes: list[Scope] = []
    signals: list[Signal] = []
    timescale = Timescale(1, "ns")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for directive in _directives(handle):
            if directive.startswith("$timescale"):
                match = re.search(r"\$timescale\s+(\d+)\s*([munpf]?s)\s+\$end", directive, re.I)
                if not match or match.group(2).lower() not in UNIT_FS:
                    raise ValueError(f"unsupported VCD timescale: {directive}")
                timescale = Timescale(int(match.group(1)), match.group(2).lower())
            elif directive.startswith("$scope"):
                parts = directive.split()
                if len(parts) < 4:
                    raise ValueError(f"malformed VCD scope: {directive}")
                kind, name = parts[1], parts[2]
                parent = ".".join(stack) or None
                stack.append(name)
                scopes.append(Scope(".".join(stack), name, kind, parent))
            elif directive.startswith("$upscope"):
                if stack:
                    stack.pop()
            elif directive.startswith("$var"):
                parts = directive.split()
                if len(parts) < 6 or not stack:
                    raise ValueError(f"malformed VCD variable: {directive}")
                kind, width, id_code = parts[1], int(parts[2]), parts[3]
                reference_parts = parts[4:-1]
                if len(reference_parts) > 1 and re.fullmatch(r"\[[^]]+\]", reference_parts[-1]):
                    reference_parts = reference_parts[:-1]
                reference = " ".join(reference_parts)
                path_name = f"{'.'.join(stack)}.{reference}"
                signals.append(Signal(path_name, reference, ".".join(stack), id_code, width, kind))
            elif directive.startswith("$enddefinitions"):
                break
    if not signals:
        raise ValueError(f"no signals found in VCD header: {path}")
    return Header(timescale, tuple(scopes), tuple(signals))


def parse_time(value: str | int, timescale: Timescale) -> int:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("time must be >= 0")
        return value
    text = str(value).strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|ms|us|ns|ps|fs)", text)
    if not match:
        raise ValueError(f"invalid time {value!r}; use ticks or a value such as 42ns")
    requested_fs = Fraction(match.group(1)) * UNIT_FS[match.group(2)]
    ticks = requested_fs / timescale.tick_fs
    if ticks.denominator != 1:
        raise ValueError(
            f"time {value!r} is not aligned to waveform resolution "
            f"{timescale.factor}{timescale.unit}"
        )
    return int(ticks)


def format_time(ticks: int, timescale: Timescale) -> dict[str, int | str]:
    # Keep one physical unit for an entire query. Mixed ns/ps logs are compact,
    # but make cycle-by-cycle reading needlessly error-prone.
    return {"ticks": ticks, "display": f"{ticks * timescale.factor}{timescale.unit}"}


def iter_changes(path: Path, watched_ids: set[str]) -> Iterator[tuple[int, str, str]]:
    current_time = 0
    in_values = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if not in_values:
                if "$enddefinitions" in line:
                    in_values = True
                continue
            if line.startswith("#"):
                current_time = int(line[1:].strip() or "0")
                continue
            first = line[0]
            if first in "01xXzZ":
                id_code = line[1:].strip()
                if id_code in watched_ids:
                    yield current_time, id_code, first.lower()
            elif first in "bB":
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip() in watched_ids:
                    yield current_time, parts[1].strip(), parts[0][1:].lower()
            elif first in "rR":
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip() in watched_ids:
                    yield current_time, parts[1].strip(), parts[0][1:]


def normalize_value(value: str, width: int) -> str:
    lowered = value.lower()
    if width > 1 and len(lowered) < width and set(lowered) <= {"0", "1"}:
        return lowered.zfill(width)
    return lowered
