from __future__ import annotations

import hashlib
import heapq
import json
from pathlib import Path
import re
from typing import Iterable

from . import SCHEMA_VERSION
from .authority import lookup_authority
from .vcd import Signal, format_time, normalize_value
from .wave import WaveBackend


def canonical_path(path: str) -> str:
    return path[4:] if path.startswith("TOP.") else path


def select_signals(
    signals: Iterable[Signal],
    scope: str | None = None,
    matches: list[str] | None = None,
    paths: list[str] | None = None,
    limit: int = 64,
) -> tuple[list[Signal], bool]:
    matches = [item.lower() for item in (matches or [])]
    wanted = {canonical_path(item) for item in (paths or [])}
    normalized_scope = canonical_path(scope) if scope else None
    selected: list[Signal] = []
    for signal in signals:
        normalized = canonical_path(signal.path)
        explicit = normalized in wanted
        if not explicit:
            if wanted and not matches:
                continue
            if normalized_scope and not (normalized == normalized_scope or normalized.startswith(normalized_scope + ".")):
                continue
            if matches and not all(item in normalized.lower() for item in matches):
                continue
        selected.append(signal)
    selected.sort(key=lambda signal: signal.path)
    return selected[:limit], len(selected) > limit


def infer_roles(signals: Iterable[Signal], limit: int = 20) -> list[dict[str, str]]:
    patterns = [
        ("clock", re.compile(r"(?:^|[._])(clk|clock)(?:$|[._])", re.I)),
        ("reset", re.compile(r"(?:^|[._])(rst|reset)(?:_?n|_?ni)?(?:$|[._])", re.I)),
        ("valid", re.compile(r"(?:^|[._])valid(?:$|[._])", re.I)),
        ("ready", re.compile(r"(?:^|[._])ready(?:$|[._])", re.I)),
        ("state", re.compile(r"(?:^|[._])(state|fsm)(?:$|[._])", re.I)),
        ("error", re.compile(r"(?:^|[._])(error|err|fail)(?:$|[._])", re.I)),
    ]
    result: list[dict[str, str]] = []
    for signal in signals:
        for role, pattern in patterns:
            if pattern.search(signal.path):
                result.append({"role": role, "path": signal.path})
                break
        if len(result) >= limit:
            break
    return result


def _rtl_info(path: str, authority: dict[str, dict[str, object]], authority_enabled: bool) -> dict[str, object]:
    row = authority.get(path) or authority.get(canonical_path(path))
    if row is not None:
        return row
    return {
        "match_status": "unresolved",
        "reason": "signal-not-in-authority" if authority_enabled else "authority-not-provided",
    }


def _waveform_info(wave: WaveBackend) -> dict[str, str]:
    source = wave.source_path or wave.path
    result = {"path": str(source), "format": wave.format, "backend": wave.backend}
    if source != wave.path:
        result["analysis_path"] = str(wave.path)
    return result


def probe(
    wave: WaveBackend,
    selected: list[Signal],
    start: int,
    end: int,
    max_changes: int,
    authority_db: Path | None = None,
    clock: str | None = None,
    edge: str = "rising",
) -> dict[str, object]:
    if start < 0 or end < start:
        raise ValueError("probe window must satisfy 0 <= start <= end")
    by_id: dict[str, list[Signal]] = {}
    for signal in selected:
        by_id.setdefault(signal.id_code, []).append(signal)
    initial: dict[str, str] = {}
    event_heap: list[tuple[int, int, dict[str, object]]] = []
    serial = 0
    truncated = False
    for timestamp, id_code, value in wave.changes(selected):
        if timestamp < start:
            initial[id_code] = value
            continue
        if timestamp > end:
            if wave.backend == "python-vcd":
                break
            continue
        for signal in by_id.get(id_code, []):
            event = {
                "time": format_time(timestamp, wave.header.timescale),
                "signal": signal.path,
                "value": normalize_value(value, signal.width),
            }
            item = (-timestamp, -serial, event)
            serial += 1
            if len(event_heap) < max_changes:
                heapq.heappush(event_heap, item)
            elif item > event_heap[0]:
                heapq.heapreplace(event_heap, item)
                truncated = True
            else:
                truncated = True
    events = [item[2] for item in event_heap]
    events.sort(key=lambda event: (int(event["time"]["ticks"]), str(event["signal"])))
    authority = lookup_authority(authority_db, [signal.path for signal in selected])
    signal_rows = []
    for signal in selected:
        row = signal.as_dict()
        row["value_before_start"] = initial.get(signal.id_code)
        rtl = _rtl_info(signal.path, authority, authority_db is not None)
        if rtl.get("match_status") not in {None, "unresolved"} and rtl.get("decl_width_bits") is not None:
            rtl["width_status"] = "match" if int(rtl["decl_width_bits"]) == signal.width else "mismatch"
        row["rtl"] = rtl
        signal_rows.append(row)
    samples: list[dict[str, object]] = []
    if clock:
        normalized_clock = canonical_path(clock)
        clock_signal = next((signal for signal in selected if canonical_path(signal.path) == normalized_clock), None)
        if clock_signal is None:
            raise ValueError("clock must be included in the selected probe signals")
        state = {row["path"]: row["value_before_start"] for row in signal_rows}
        index = 0
        while index < len(events):
            timestamp = int(events[index]["time"]["ticks"])
            group: list[dict[str, object]] = []
            while index < len(events) and int(events[index]["time"]["ticks"]) == timestamp:
                group.append(events[index])
                index += 1
            old_clock = state.get(clock_signal.path)
            for event in group:
                state[str(event["signal"])] = str(event["value"])
            new_clock = state.get(clock_signal.path)
            rising = old_clock == "0" and new_clock == "1"
            falling = old_clock == "1" and new_clock == "0"
            if edge == "both" or (edge == "rising" and rising) or (edge == "falling" and falling):
                samples.append({"time": format_time(timestamp, wave.header.timescale), "values": dict(state), "sampling": "post-delta"})
    return {
        "schema_version": SCHEMA_VERSION,
        "waveform": _waveform_info(wave),
        "timescale": wave.header.timescale.as_dict(),
        "window": {"start": format_time(start, wave.header.timescale), "end": format_time(end, wave.header.timescale)},
        "limits": {"selected_signal_count": len(selected), "max_changes": max_changes},
        "truncated": truncated,
        "signals": signal_rows,
        "changes": events,
        "clock_samples": samples,
        "next_probe": "narrow --scope/--match or reduce the time window" if truncated else None,
    }


def signal_value(wave: WaveBackend, signal: Signal, timestamp: int, authority_db: Path | None = None) -> dict[str, object]:
    latest: tuple[int, str] | None = None
    for change_time, _id_code, value in wave.changes([signal]):
        if change_time > timestamp:
            break
        latest = (change_time, normalize_value(value, signal.width))
    authority = lookup_authority(authority_db, [signal.path])
    return {
        "schema_version": SCHEMA_VERSION,
        "waveform": _waveform_info(wave),
        "timescale": wave.header.timescale.as_dict(),
        "query": {"signal": signal.path, "time": format_time(timestamp, wave.header.timescale)},
        "signal": {**signal.as_dict(), "rtl": _rtl_info(signal.path, authority, authority_db is not None)},
        "value_at_time": {
            "found": latest is not None,
            "time": format_time(latest[0], wave.header.timescale) if latest else None,
            "value": latest[1] if latest else None,
            "status": "ok" if latest else "uninitialized-before-time",
        },
    }


def _series(wave: WaveBackend, signals: list[Signal]) -> dict[str, list[tuple[int, str]]]:
    paths_by_id: dict[str, list[str]] = {}
    for signal in signals:
        paths_by_id.setdefault(signal.id_code, []).append(canonical_path(signal.path))
    widths = {signal.id_code: signal.width for signal in signals}
    result = {canonical_path(signal.path): [] for signal in signals}
    for timestamp, id_code, value in wave.changes(signals):
        for path in paths_by_id.get(id_code, []):
            result[path].append((timestamp * wave.header.timescale.tick_fs, normalize_value(value, widths[id_code])))
    return result


def compare_waveforms(good: WaveBackend, bad: WaveBackend, scope: str | None, matches: list[str], limit: int) -> dict[str, object]:
    good_selected, good_truncated = select_signals(good.header.signals, scope, matches, limit=limit)
    bad_selected, bad_truncated = select_signals(bad.header.signals, scope, matches, limit=limit)
    good_by_path = {canonical_path(signal.path): signal for signal in good_selected}
    bad_by_path = {canonical_path(signal.path): signal for signal in bad_selected}
    shared = sorted(set(good_by_path) & set(bad_by_path))
    good_series = _series(good, [good_by_path[path] for path in shared])
    bad_series = _series(bad, [bad_by_path[path] for path in shared])
    divergences: list[dict[str, object]] = []
    for path in shared:
        left, right = good_series[path], bad_series[path]
        event_times = sorted({time for time, _ in left} | {time for time, _ in right})
        left_index = right_index = 0
        left_value = right_value = None
        for timestamp in event_times:
            while left_index < len(left) and left[left_index][0] == timestamp:
                left_value = left[left_index][1]
                left_index += 1
            while right_index < len(right) and right[right_index][0] == timestamp:
                right_value = right[right_index][1]
                right_index += 1
            if left_value is not None and right_value is not None and left_value != right_value:
                same_resolution = good.header.timescale.tick_fs == bad.header.timescale.tick_fs
                divergences.append({
                    "signal": path,
                    "time_fs": timestamp,
                    "time_ticks": timestamp // good.header.timescale.tick_fs if same_resolution else None,
                    "good": left_value,
                    "bad": right_value,
                })
                break
    divergences.sort(key=lambda row: (int(row["time_fs"]), str(row["signal"])))
    return {
        "schema_version": SCHEMA_VERSION,
        "good": str(good.path),
        "bad": str(bad.path),
        "good_timescale": good.header.timescale.as_dict(),
        "bad_timescale": bad.header.timescale.as_dict(),
        "matching": "exact canonical path after optional TOP prefix removal",
        "shared_signal_count": len(shared),
        "truncated": good_truncated or bad_truncated,
        "first_divergence": divergences[0] if divergences else None,
        "signal_divergences": divergences,
    }


def cache_key(parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
