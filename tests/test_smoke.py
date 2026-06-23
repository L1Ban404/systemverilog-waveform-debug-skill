#!/usr/bin/env python3
"""End-to-end smoke test for discovery, authority, and waveform queries."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures"
CLI = ROOT / "scripts/wave_debug.py"


def run(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="systemverilog-waveform-debug-skill-") as temporary:
        output = Path(temporary)
        common = (
            "--workspace", str(FIXTURE),
            "--waveform", str(FIXTURE / "wave.vcd"),
            "--out-dir", str(output),
        )
        doctor = json.loads(run("doctor", "--json"))
        assert doctor["capabilities"]["vcd"]["available"] is True

        inspected = json.loads(run("inspect", *common, "--json"))
        assert inspected["selected_top"] == "top_tb"
        assert inspected["waveform"]["backend"] == "python-vcd"
        assert inspected["timescale"]["unit"] == "ns"

        scopes = json.loads(run("scopes", *common, "--json"))
        assert {scope["path"] for scope in scopes["scopes"]} == {"top_tb", "top_tb.u_dut"}

        discovered = json.loads(
            run("signals", *common, "--scope", "top_tb.u_dut", "--match", "valid", "--json")
        )
        assert [signal["path"] for signal in discovered["signals"]] == ["top_tb.u_dut.valid_o"]

        run("authority", *common, "--force")
        packet_path = Path(
            run(
                "packet",
                *common,
                "--window",
                "0",
                "--window-len",
                "10",
                "--focus-scope",
                "TOP.top_tb.u_dut",
            ).strip()
        )
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        assert packet["schema_version"] == "0.3"
        signals = packet["signals"]
        assert len(signals) == 3
        assert all(signal["rtl"]["match_status"] == "static-source-match" for signal in signals)

        evidence = json.loads(
            run(
                "probe",
                *common,
                "--scope",
                "top_tb.u_dut",
                "--match",
                "valid",
                "--signal",
                "top_tb.u_dut.clk",
                "--clock",
                "top_tb.u_dut.clk",
                "--start",
                "0ns",
                "--end",
                "20ns",
            )
        )
        assert len(evidence["signals"]) == 2
        assert evidence["clock_samples"][0]["time"]["ticks"] == 5

        point = json.loads(
            run(
                "signal",
                *common,
                "--signal",
                "TOP.top_tb.u_dut.valid_o",
                "--time",
                "16ns",
                "--window-len",
                "10",
            )
        )
        assert point["value_at_time"]["value"] == "1"
        assert point["value_at_time"]["time"]["ticks"] == 15

        compared = json.loads(
            run("compare", str(FIXTURE / "wave.vcd"), str(FIXTURE / "wave_bad.vcd"))
        )
        assert compared["first_divergence"]["signal"] == "top_tb.u_dut.valid_o"
        assert compared["first_divergence"]["time_ticks"] == 15

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
