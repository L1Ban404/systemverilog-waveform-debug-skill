---
name: systemverilog-waveform-debug-skill
description: Investigate and explain Verilog or SystemVerilog failures from FST/VCD waveform evidence and HDL source. Use for simulation failures, assertion violations, protocol bugs, pipeline stalls, FSM errors, data/control mismatches, X/Z propagation, reset/clock problems, or requests to find and optionally fix an RTL root cause. Discover scopes and signals, compare good and bad traces, query bounded time windows, map waveform paths to RTL, test causal hypotheses, and support waveform-only triage when source is unavailable.
---

# SystemVerilog Waveform Debug Skill

Work from observable behavior toward the earliest causal RTL transition. Keep facts, interpretations, and hypotheses separate.

## Start safely

Run from the HDL workspace root. Check capabilities, then inspect inputs:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py doctor
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py inspect --json
```

Pass `--waveform`, `--source-root`, `--filelist`, and `--top` when discovery is ambiguous. Never silently choose among multiple plausible tops or traces.

## Investigate iteratively

1. Establish waveform provenance, timescale, clocks, resets, failure time, and the first incorrect externally visible signal.
2. Discover paths instead of guessing them:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py scopes --json
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py signals \
  --scope tb.dut --match valid --limit 40 --json
```

3. Probe a small window and a falsifiable set of signals:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py probe \
  --around 420ns --radius 30ns --scope tb.dut \
  --match ready --signal tb.dut.clk --clock tb.dut.clk
```

Use `--start/--end` for explicit windows and `--max-signals`/`--max-changes` to control evidence size. Preserve `X/Z`; never reinterpret them as zero.

4. When a known-good trace exists, locate the earliest divergence before reading downstream symptoms:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py compare good.vcd bad.vcd \
  --scope tb.dut --match state
```

5. Build RTL authority only when source mapping is needed:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py authority \
  --waveform bad.fst --filelist sim/files.f --top tb_top
```

Re-run `probe` with the same source/top options. Treat `static-source-match` authority as an ownership candidate and verify it against waveform hierarchy for generate-, interface-, package-, or macro-heavy RTL. Treat `heuristic-text-match` source context only as a navigation candidate. Reserve `exact` for a future compiler-elaborated backend.

6. Form one causal hypothesis at a time. State what the next probe should show if it is true and what would falsify it. Narrow or extend the window only as evidence requires.

Read [references/debug-methodology.md](references/debug-methodology.md) when reasoning about sequential timing, protocols, pipelines, memories, CDC, reset, or unknown propagation.

## Diagnose and fix

Report these sections:

- `Observed`: waveform facts with timestamp/cycle and signal paths.
- `Inferred`: source semantics that follow from the observed facts.
- `Hypothesis`: unproven causal claims plus the falsifying probe.
- `Root cause`: module, condition, bug class, source location, and confidence.
- `Fix or next probe`: a concrete change only at high confidence; otherwise the smallest next query.
- `Verification`: targeted and broader regressions run, or remaining gaps.

Do not edit RTL unless the user asks for a fix or the request clearly includes implementation. When authorized, preserve unrelated changes, add a regression that fails for the diagnosed behavior, make the smallest RTL change, run the targeted test, then run the relevant broader suite.

## Bound the evidence

Prefer fewer than 64 signals and 200 changes per probe. If output is truncated, narrow by scope, name, time, or clock edge. Avoid raw waveform dumps, exhaustive signal inventories, and large source excerpts.

Use VCD directly on Python 3.10+. For FST, the tool tries compatible `pywellen`, then cached `fst2vcd` conversion. Run `doctor` for actionable dependency failures.

Keep generic waveform and RTL-authority behavior in this repository. Do not introduce project-specific hierarchy assumptions into the parser.
