from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/wave_debug.py"
FIXTURE = ROOT / "tests/fixtures/wave.vcd"


def invoke(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


class CliTests(unittest.TestCase):
    def test_doctor_reports_backend_provenance_and_authority_confidence(self) -> None:
        result = json.loads(invoke("doctor", "--json").stdout)
        direct = result["capabilities"]["fst_direct"]
        self.assertIn(direct["source"], {"bundled", "installed", None})
        self.assertIn("soabi", direct["runtime"])
        authority = result["capabilities"]["rtl_authority"]
        self.assertEqual(authority["default_backend"], "auto")
        self.assertEqual(authority["backends"]["static"]["match_status"], "static-source-match")
        self.assertFalse(authority["backends"]["static"]["exact"])
        self.assertEqual(authority["backends"]["verilator"]["match_status"], "exact")

    def test_waveform_only_probe_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "wave.vcd")
            inspected = json.loads(invoke("inspect", "--workspace", str(root), "--json").stdout)
            self.assertEqual(inspected["source"]["files"], 0)
            result = json.loads(
                invoke(
                    "probe", "--workspace", str(root), "--scope", "top_tb.u_dut",
                    "--start", "0", "--end", "20", "--max-changes", "2",
                ).stdout
            )
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["changes"]), 2)
            self.assertEqual(result["signals"][0]["rtl"]["reason"], "authority-not-provided")

    def test_multiple_waveforms_require_an_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "older.vcd")
            shutil.copy2(FIXTURE, root / "newer.vcd")
            inspected = json.loads(invoke("inspect", "--workspace", str(root), "--json").stdout)
            self.assertIsNone(inspected["waveform"]["selected"])
            self.assertEqual(inspected["waveform"]["selection"], "explicit --waveform required: multiple candidates found")
            self.assertEqual(len(inspected["waveform"]["candidates"]), 2)
            self.assertIn("modified_at", inspected["waveform"]["candidates"][0])
            rejected = invoke("scopes", "--workspace", str(root), check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("multiple waveform candidates found", rejected.stderr)

    def test_literal_match_regex_and_hierarchy_suggestions_are_explicit(self) -> None:
        common = ("--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE), "--json")
        literal = json.loads(invoke("signals", *common, "--match", "valid|clk").stdout)
        self.assertEqual(literal["count"], 0)
        self.assertEqual(literal["matching"]["match"], "case-insensitive local signal-name substring; repeated terms are ANDed")
        regex = json.loads(invoke("signals", *common, "--regex", "valid|clk").stdout)
        self.assertGreaterEqual(regex["count"], 2)
        scopes = json.loads(invoke("scopes", *common, "--scope", "top_tb.dut").stdout)
        self.assertEqual(scopes["scopes"], [])
        self.assertIn("top_tb.u_dut", scopes["suggestions"])

    def test_snapshot_table_uses_clock_edges(self) -> None:
        result = invoke(
            "probe", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
            "--scope", "top_tb.u_dut", "--start", "0ns", "--end", "20ns",
            "--match", "valid", "--signal", "top_tb.u_dut.clk", "--clock", "top_tb.u_dut.clk",
            "--format", "table", "--view", "snapshots",
        )
        self.assertIn("5ns", result.stdout)
        self.assertIn("top_tb.u_dut.valid_o=0", result.stdout)

    def test_metadata_cache_invalidates_with_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wave = root / "wave.vcd"
            shutil.copy2(FIXTURE, wave)
            common = ("inspect", "--workspace", str(root), "--out-dir", "out", "--json")
            invoke(*common)
            cache = root / "out/cache/waveform_meta"
            self.assertEqual(len(list(cache.iterdir())), 1)
            wave.write_text(wave.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            invoke(*common)
            self.assertEqual(len(list(cache.iterdir())), 2)

    def test_ambiguous_top_requires_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "two.sv").write_text("module alpha; endmodule\nmodule beta; endmodule\n", encoding="utf-8")
            result = invoke(
                "authority", "--workspace", str(root), "--waveform", str(FIXTURE),
                "--out-dir", str(root / "out"), check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("top module is ambiguous", result.stderr)

    def test_invalid_time_has_actionable_error(self) -> None:
        result = invoke(
            "signal", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
            "--signal", "top_tb.u_dut.valid_o", "--time", "half-a-cycle", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("use ticks or a value such as 42ns", result.stderr)

    def test_protocol_probe_preserves_reset_x_and_payload_violation(self) -> None:
        waveform = ROOT / "tests/fixtures/protocol_bad.vcd"
        result = json.loads(
            invoke(
                "probe", "--workspace", str(ROOT / "tests/fixtures"),
                "--waveform", str(waveform), "--scope", "handshake_tb",
                "--start", "0ns", "--end", "20ns", "--max-changes", "40",
            ).stdout
        )
        changes = {(row["time"]["ticks"], row["signal"], row["value"], row["value_bits"]) for row in result["changes"]}
        self.assertIn((0, "handshake_tb.rst_n", "x", "x"), changes)
        self.assertIn((10, "handshake_tb.valid", "1", "1"), changes)
        self.assertIn((15, "handshake_tb.data", "0x22", "00100010"), changes)
        self.assertIn((0, "handshake_tb.data", "0bxxxxxxxx", "xxxxxxxx"), changes)
        self.assertEqual(result["sampling"]["phase"], "waveform-observed")

    def test_matching_provenance_report_and_phase_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "provenance.json"
            invoke(
                "provenance", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--simulator", "example-sim", "--parameter", "WIDTH=8", "--failure-label", "example-failure",
                "--out", str(manifest),
            )
            provenance = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(provenance["schema_version"], "1.0")
            self.assertEqual(provenance["compilation"]["parameter_overrides"], ["WIDTH=8"])
            inspected = json.loads(invoke(
                "inspect", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--provenance-file", str(manifest), "--json",
            ).stdout)
            self.assertTrue(inspected["provenance"]["provided"]["waveform_matches_current"])
            authority_dir = Path(invoke(
                "authority", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--provenance-file", str(manifest), "--authority-backend", "static", "--out-dir", str(root / "authority"),
            ).stdout.strip())
            self.assertTrue((authority_dir / "rtl_authority.sqlite3").is_file())
            direct = json.loads(invoke(
                "signals", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--scope", "top_tb", "--no-recursive", "--path-regex", r"^top_tb\.", "--json",
            ).stdout)
            self.assertEqual({row["scope"] for row in direct["signals"]}, {"top_tb"})
            report = root / "evidence.md"
            invoke(
                "probe", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--scope", "top_tb.u_dut", "--start", "0", "--end", "20", "--report", str(report),
                "--inference", "recorded interpretation", "--hypothesis", "recorded hypothesis",
            )
            text = report.read_text(encoding="utf-8")
            self.assertIn("## Observed", text)
            self.assertIn("recorded hypothesis", text)
            phase = invoke(
                "probe", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--scope", "top_tb", "--start", "0", "--end", "1", "--sample-phase", "post-nba", check=False,
            )
            self.assertEqual(phase.returncode, 2)
            self.assertIn("requires simulator-time instrumentation", phase.stderr)

    def test_compare_can_align_clock_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.vcd"
            bad = root / "bad.vcd"
            source = FIXTURE.read_text(encoding="utf-8")
            good.write_text(source, encoding="utf-8")
            shifted = source
            for timestamp in (20, 15, 10, 5, 0):
                shifted = shifted.replace(f"#{timestamp}\n", f"#{timestamp + 20}\n")
            bad.write_text(shifted, encoding="utf-8")
            absolute = json.loads(invoke("compare", str(good), str(bad), "--workspace", str(root)).stdout)
            self.assertIsNotNone(absolute["first_divergence"])
            aligned = json.loads(invoke(
                "compare", str(good), str(bad), "--workspace", str(root),
                "--align", "clock-edge", "--align-signal", "top_tb.clk",
            ).stdout)
            self.assertIsNone(aligned["first_divergence"])
            self.assertEqual(aligned["alignment"]["mode"], "clock-edge")

    def test_probe_preserves_static_authority_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            common = (
                "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
                "--out-dir", str(output), "--top", "top_tb",
            )
            invoke("authority", *common, "--authority-backend", "static")
            result = json.loads(
                invoke("probe", *common, "--scope", "top_tb.u_dut", "--start", "0", "--end", "20").stdout
            )
            self.assertTrue(result["signals"])
            self.assertTrue(all(row["rtl"]["match_status"] == "static-source-match" for row in result["signals"]))
            self.assertTrue(all(row["rtl"]["width_status"] == "match" for row in result["signals"]))
            authority = result["provenance"]["authority"]
            self.assertEqual(authority["schema_version"], "0.4")
            self.assertTrue(authority["limitations"])


if __name__ == "__main__":
    unittest.main()
