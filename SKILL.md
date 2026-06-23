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

Pass `--waveform` when more than one trace exists. `inspect` will list every candidate with its UTC modification time and require an explicit choice; all waveform-reading commands likewise reject an ambiguous choice. Re-run the failing test first and use the waveform it just wrote—do not assume a pre-existing waveform belongs to the failure.

`--top` is a source/elaboration option for `inspect`, `probe`, `packet`, and `authority`; `scopes` and `signals` intentionally do **not** accept it because they inspect the waveform's actual elaborated hierarchy. Use them first to discover `dut`/`u_dut`, generate, array, and struct paths.

Capture provenance when a failure is produced. The manifest is framework-neutral and records waveform identity/timescale, simulator details, sources, filelists, includes, defines, parameter overrides, command, and optional failure locator:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py provenance \
  --waveform <waveform-from-failing-run> --top <top> --filelist <filelist> \
  --simulator <simulator> --simulator-version <version> \
  --simulation-command '<reproducible-command>' --failure-label '<failure-label>' \
  --out build/wave-debug/provenance.json
```

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
  --match ready --signal tb.dut.clk --clock tb.dut.clk \
  --format table --view snapshots
```

`--match` is a case-insensitive **literal substring**; repeated values are ANDed. It is not a regular expression, so `--match '<alternative-1>|<alternative-2>'` looks for those literal `|` characters. Use `--name-regex '<alternative-1>|<alternative-2>'` for local-name alternatives or `--path-regex` for full paths. When a scope or signal query is empty, read its suggestions before widening the search.

For unambiguous filtering, `--match`/`--name-regex` apply to local signal names; `--path-match`/`--path-regex` apply to full elaborated paths; `--regex` remains a full-path alias. Scope traversal is recursive by default; use `--no-recursive` to restrict signals to the named scope.

`probe` defaults to `--radix auto`: 1-bit logic stays `0/1/X/Z`, known buses use hex, and buses with mixed `X/Z` use exact `0b...` bit strings. Every formatted event also has `value_bits` in JSON. Do not discard it.

With `--clock`, JSON output includes `waveform-observed` clock samples; in table mode, `--view snapshots` prints one selected-signal state per requested edge (or use `--view both`). Offline VCD/FST cannot prove Active/NBA/Postponed ordering. `pre-edge`, `post-active`, `post-nba`, and `postponed` therefore require simulator-time instrumentation and are rejected rather than guessed.

Use `--start/--end` for explicit windows and `--max-signals`/`--max-changes` to control evidence size. Preserve `X/Z`; never reinterpret them as zero.

4. When a known-good trace exists, locate the earliest divergence before reading downstream symptoms:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py compare good.vcd bad.vcd \
  --scope tb.dut --match state
```

Use `--align reset-deassert --align-signal <path>` or `--align clock-edge --align-signal <path>` when trace starts or reset lengths differ. Use absolute alignment only when the two runs share the same time origin.

5. Build RTL authority only when source mapping is needed:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py authority \
  --waveform bad.fst --filelist sim/files.f --top tb_top --authority-backend auto
```

Re-run `probe` with the same source/top options. `auto` selects Verilator elaboration when its JSON interface is available; only its `exact` result is compiler-elaborated, and it must use the same sources, include paths, defines, and top as the simulation. Treat `static-source-match` authority as an ownership candidate and verify it against waveform hierarchy for generate-, interface-, package-, or macro-heavy RTL. Use `--authority-backend verilator` to require elaboration or `static` to require the portable fallback. Treat `heuristic-text-match` source context only as a navigation candidate.

Pass the same `--filelist`, `--include`, `--define`, and `--parameter NAME=VALUE` inputs used for simulation, or reuse them with `--provenance-file <manifest>`. Verilator elaboration applies parameter overrides; the static backend records them but remains a `static-candidate`. Probe mappings expose the normalized tier `elaborated-exact`, `static-candidate`, or `heuristic-context` in addition to backend-specific metadata.

6. Form one causal hypothesis at a time. State what the next probe should show if it is true and what would falsify it. Narrow or extend the window only as evidence requires.

Read [references/debug-methodology.md](references/debug-methodology.md) when reasoning about sequential timing, protocols, pipelines, memories, CDC, reset, or unknown propagation.

For cocotb, run the one failing testcase with its normal waveform option, then pass the emitted path explicitly: `... inspect --waveform <path-written-by-failing-run> --json`. Keep the `results.xml` testcase name alongside the probe notes; it establishes failure provenance but does not prove that an older nearby waveform belongs to that result.

When handing off an investigation, write a small report rather than recounting a terminal dump:

```bash
python .codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py probe \
  --waveform <waveform-from-failing-run> --start <start> --end <end> \
  --report build/wave-debug/evidence.md \
  --inference '<interpretation>' --hypothesis '<falsifiable claim>'
```

The report keeps Observed evidence separate from user-supplied Inferred and Hypothesis statements.

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
