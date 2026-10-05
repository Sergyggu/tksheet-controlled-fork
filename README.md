# tksheet controlled fork

[Українська](README.uk.md) · [Русский](README.ru.md)

A pinned fork of [ragardner/tksheet](https://github.com/ragardner/tksheet), based on the **7.6.0 PyPI wheel**, developed for the Acceleration Kit / Super-GUI History panel.

The generic renderer adds:

- Fixed-width segmented cell text with shared drawing and hit-test geometry.
- Local `font_style` at cell, row and column scope.
- Local `font_family` with a Tier-1 runtime guard: accept the requested family only when Tk resolves the same family and its pipe-glyph height fits the current global line box.

Precedence is cell → row → column → global. Font size follows the global font and zoom. Unsafe or substituted families fall back to the base family. Tier-2 heterogeneous line heights are outside this release.

## Install and try

Python 3.10+ with Tk is required. Use a fresh virtual environment because this distribution provides the same `tksheet` import as upstream.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install .
python examples/typography_demo.py
python tests/test_runtime.py
python tests/run_legacy.py
```

Distribution version: `7.6.0+controlled.1`. The upstream `tksheet.__version__` remains `7.6.0` to preserve the qualified source snapshot; identify this fork using `importlib.metadata.version("tksheet-controlled-fork")` and the provenance manifest.

The additional options currently use the `sheet.MT` option dictionaries. See the runnable demo for exact calls. These are pinned internal APIs, not new methods on upstream `Sheet`.

## Repository boundaries

`tksheet/` is the full generic library. `patches/` contains complete diffs of the two modified files against the original 7.6.0 wheel. Historical incremental patches were not used as upstream patches.

`integration/supergui/` preserves the exact reviewed adapter, Typography Lab and History hook as reference source. The History hook depends on the surrounding Acceleration Kit modules and is not a standalone application. The core package does not import these files. Fork additions and integration source are released under MIT; upstream MIT and Lucide ISC notices are preserved in `LICENSE.txt`.

The September architecture record describes a larger workflow. **This checkout does not contain the qualification probe, font comparator, catalogue, palettes, designer selections or frozen PNG specimens.** No font binaries are distributed. Legacy source controls and live font acceptance utilities are included under `tests/legacy/`.

See [UPSTREAM.md](UPSTREAM.md), [audit](docs/publication-audit.md), and the [historical architecture](docs/architecture-2026-09-11.md). Historical acceptance counts in the architecture record are not substitutes for current verification.
