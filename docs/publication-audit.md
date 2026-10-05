# Publication audit — 2026-10-05

The three supplied September bundles contain 22 manifest-listed files. All SHA-256 values match. The historical architecture document is treated as a record and proposal, not as instructions or proof of current test results.

## Recovered components

- R2.1: reviewed generic renderer files (`main_table.py`, `column_headers.py`), product adapter, historical incremental diffs, acceptance utilities and 39 controls.
- FINAL_UI R1: final `kit_font_system.py` with the unified Typography Lab UI and 20 source controls.
- HISTORY_HOOK R1: `kit_panel_tab_history.py`, which connects the Lab to the product History panel, and 11 source controls. This is an integration hook, not a more advanced laboratory implementation.

The complete generic package was reconstructed from the original 7.6.0 PyPI wheel plus the two reviewed files. Full upstream diffs are included; separating the combined historical changes into three artificial feature patches would misrepresent the available provenance.

No standalone segmented 25-control suite, qualification probe, comparator, font catalogue/palette, selection JSON or specimen PNG was supplied. Specimens are a required deferred follow-up (see `ROADMAP.md`). Original installer/rollback scripts target the surrounding Kit and are retained only in the local source bundles, not shipped as public installation instructions.

## Current verification

On this Windows host with Python 3.11 and real Tk:

- Tier-1 legacy controls: 39/39 passed, including safe-family rendering, precedence, substitution fallback, autosize and cache invalidation.
- Typography final UI source controls: 20/20 passed.
- History hook source controls: 11/11 passed.
- Typography layer selftest: all passed using its mock grid and generated temporary catalogue.

- Clean Python 3.11 virtual environment: distribution built and installed successfully with offline PyPI build dependencies. Import resolves to that environment's `site-packages`, distribution version `7.6.0+controlled.1`.
- Three standalone real-Tk behavioral tests passed: segmented centers and margins, style precedence, and missing-family fallback preserving live size/style.

Source checks verify code structure; they are not a full human visual acceptance of the Lab. A complete product UI run still requires the surrounding Kit and missing font catalogue. Cross-platform acceptance has not been performed.
