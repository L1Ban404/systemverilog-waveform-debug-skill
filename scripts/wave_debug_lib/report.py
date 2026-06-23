"""Small, reviewable Markdown reports derived from bounded probe evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def write_probe_report(
    path: Path, evidence: dict[str, Any], inferences: list[str], hypotheses: list[str], command: str,
) -> None:
    waveform = evidence["waveform"]
    lines = [
        "# Waveform evidence report", "", "## Provenance", "",
        f"- Waveform: `{waveform['path']}`",
        f"- Format/backend: `{waveform['format']}` / `{waveform['backend']}`",
        f"- Window: `{evidence['window']['start']['display']}` to `{evidence['window']['end']['display']}`",
        f"- Sampling: `{evidence['sampling']['phase']}` (offline event-region ordering is unavailable)",
        f"- Command: `{command}`", "", "## Observed", "",
        "| Time | Signal | Value |", "| --- | --- | --- |",
    ]
    changes = evidence.get("changes", [])
    for change in changes:
        lines.append(f"| {change['time']['display']} | `{change['signal']}` | `{change['value']}` |")
    if not changes:
        lines.append("| — | No selected signal changes in the window | — |")
    lines.extend(["", "## Inferred", ""])
    lines.extend(f"- {item}" for item in inferences) if inferences else lines.append("- None supplied.")
    lines.extend(["", "## Hypothesis", ""])
    lines.extend(f"- {item}" for item in hypotheses) if hypotheses else lines.append("- None supplied.")
    lines.extend(["", "## Next probe", "", evidence.get("next_probe") or "Narrow to one causal hypothesis or extend the bounded window.", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
