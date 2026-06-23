# systemverilog-waveform-debug-skill

[![Smoke test](https://github.com/L1Ban404/systemverilog-waveform-debug-skill/actions/workflows/smoke.yml/badge.svg)](https://github.com/L1Ban404/systemverilog-waveform-debug-skill/actions/workflows/smoke.yml)

A Codex skill and portable CLI for evidence-driven Verilog/SystemVerilog debugging from VCD or FST waveforms.

Version 0.4 turns waveform analysis into an iterative investigation: discover hierarchy and signals, query compact windows, compare good and bad traces, map activity to RTL ownership, test hypotheses, and close the loop with an authorized RTL fix and regression.

## Capabilities

- Pure Python 3.10+ streaming VCD metadata and change queries
- FST through compatible `pywellen` or cached `fst2vcd` conversion
- Waveform-only scope, signal, point, and bounded-window queries
- Physical time units and clock-edge samples
- Good/bad trace first-divergence analysis
- Verilog/SystemVerilog discovery plus `.f/.flist`, include, define, and exclude inputs
- RTL hierarchy authority and source-navigation context
- Bounded JSON evidence designed for an LLM context window

The adapter is simulator- and architecture-independent.

## Install as a project skill

```bash
git submodule add https://github.com/L1Ban404/systemverilog-waveform-debug-skill.git \
  .codex/skills/systemverilog-waveform-debug-skill
```

SSH works as well:

```bash
git submodule add git@github.com:L1Ban404/systemverilog-waveform-debug-skill.git \
  .codex/skills/systemverilog-waveform-debug-skill
```

## Quick start

```bash
CLI=.codex/skills/systemverilog-waveform-debug-skill/scripts/wave_debug.py

python "$CLI" doctor
python "$CLI" inspect --json
python "$CLI" scopes --json
python "$CLI" signals --scope tb.dut --match valid --json
python "$CLI" probe --around 420ns --radius 30ns \
  --scope tb.dut --signal tb.dut.clk --clock tb.dut.clk
```

Map selected activity back to RTL:

```bash
python "$CLI" authority --waveform build/fail.fst \
  --filelist sim/files.f --top tb_top --authority-backend auto
python "$CLI" probe --waveform build/fail.fst --around 420ns --radius 20ns \
  --scope tb_top.dut --match state --filelist sim/files.f --top tb_top
```

Compare traces:

```bash
python "$CLI" compare passing.vcd failing.vcd --scope tb.dut
```

Run `python "$CLI" <command> --help` for all options. Times accept integer waveform ticks or physical values such as `42ns` and `1.5us`.

## Backends

VCD requires only Python 3.10 or newer. FST uses the first available path:

1. the bundled BSD-3-Clause `pywellen` binary on compatible CPython 3.12 x86-64 Linux systems;
2. a compatible installed `pywellen` on other platforms;
3. `fst2vcd`, commonly provided by GTKWave or OSS CAD Suite.

`doctor --json` reports backend provenance, runtime ABI, capabilities, and remediation. RTL authority defaults to `auto`: it uses Verilator's elaborated JSON when the installed Verilator supports `--json-only`, labeling the result `exact` with high confidence. Use `--authority-backend static` for the internal no-dependency fallback; it is labeled `static-source-match` and is useful for ownership candidates, but not equivalent to compiler elaboration for complex generate, interface, package, or preprocessor-heavy designs. An explicit `--authority-backend verilator` never silently falls back.

### Recommended: install Verilator locally

For parameterized, macro-heavy, or generate-heavy RTL, install a recent local Verilator so `authority --authority-backend auto` can produce compiler-elaborated `exact` mappings. Verify that the installed version exposes the required JSON interface:

```bash
verilator --version
verilator --help | grep -- --json-only
python "$CLI" doctor --json
```

On Debian/Ubuntu, `sudo apt install verilator` is a convenient starting point; use a newer upstream or OSS CAD Suite build when the packaged version does not list `--json-only`. The tool continues to work without Verilator through `--authority-backend static`.

The bundled `pywellen` component is distributed under BSD-3-Clause; see `third_party/pywellen/LICENSE`.

Authority JSON and SQLite metadata use the same `0.4` schema version. The JSON contract is published in `schemas/authority.schema.json`; consumers should reject unsupported versions instead of guessing field semantics. Authority files and cache metadata are written atomically, while cache identity includes the selected backend, sources, include paths, and definitions.

## Development

```bash
python -m unittest discover -s tests -p 'test_*.py'
python tests/test_smoke.py
python -m py_compile scripts/wave_debug.py scripts/wave_debug_lib/*.py
python -m pip install -r requirements-dev.txt
python tests/validate_skill.py
```

CI checks the portable VCD path on Python 3.10–3.13 and exercises both direct and converted FST paths on Python 3.12.

## License

Apache-2.0. See [LICENSE](LICENSE).
