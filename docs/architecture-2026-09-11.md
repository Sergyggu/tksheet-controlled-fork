# tksheet Controlled Fork for Super-GUI
## Fixed interaction geometry, local typography, runtime font qualification, and a WYSIWYG design pipeline

> **Status:** engineering/architecture record after successful live integration in Super-GUI History  
> **Upstream base:** `tksheet 7.6.0`  
> **Project context:** Legacy GPU Kit / Acceleration Kit for InvokeAI — Super-GUI  
> **Date:** 2026-09-11

---

## 1. Why this fork exists

The fork did not begin as an attempt to redesign `tksheet`.

It began with a practical product problem in the Super-GUI History tab: a dense inference journal needed richer interaction and typography than a stock table widget could provide, while preserving the properties that made the table useful in the first place:

- one logical database field must remain one logical table column;
- user actions must hit the exact thing they appear to hit;
- column widths, wrapping, autosize and zoom must remain coherent;
- the table must still behave as a table rather than becoming a collection of custom mini-widgets;
- product-specific concepts must not leak into the generic renderer;
- every renderer modification must be reversible, testable and version-pinned.

The result is a deliberately narrow **controlled fork** of `tksheet 7.6.0`, plus a separate typography/design toolchain layered on top of it.

The fork adds three generic capabilities:

1. **fixed-width segmented text** for deterministic multi-slot controls inside one logical cell;
2. **local `font_style` resolution** at cell / row / column scope;
3. **Tier-1 local `font_family` resolution** with a fail-closed runtime height guard.

The Super-GUI then adds a fourth layer **outside the fork**:

4. a font qualification + catalogue + palette + **Typography Lab** pipeline for live WYSIWYG fitting on the real product grid.

This separation is the central architectural decision of the work.

---

## 2. What was wrong with the obvious approaches

### 2.1 Rating glyphs are not geometry

The History evaluation area uses five symbols such as:

```text
★★★★★
★★★☆☆
▲▲△△△
```

The first instinct is to render one Unicode string and infer which symbol was clicked from text metrics.

That turned out to be the wrong abstraction.

Glyph ink boxes, advances, kerning, font fallback and platform text behaviour are presentation details. A rating ordinal is interaction geometry. If the click target for slot 3 depends on the width of glyphs 1 and 2, the UI can visually drift away from its own data model.

The successful design therefore stopped measuring the symbols for interaction purposes.

### 2.2 A local font is not only a drawing concern

`tksheet` already knows how to draw a global Tk font tuple. The difficult part of local typography was not calling `Canvas.create_text(..., font=...)`.

A different local font affects:

- text measurement;
- wrapping;
- autosize;
- row-height calculation;
- zoom;
- caches;
- headers that measure body cells.

So the invariant had to be stronger than “the requested font is visible”:

> **measure font == wrap font == draw font**

### 2.3 Font family introduces a vertical-geometry boundary

`tksheet` derives its table text height from the Canvas bounding box of the pipe glyph (`|`) in the global table font.

That means a locally selected family can be horizontally correct and still be vertically unsafe.

A shorter family is harmless: the global line box has extra room.

A taller family can clip or invalidate row-height assumptions.

That gave us a practical boundary:

```text
Tier 1:
    pipe(candidate, current_size, effective_style)
    <= live global table line box

Tier 2:
    candidate needs a taller local line box
    and the product still wants heterogeneous per-cell vertical geometry
```

Tier 2 was intentionally **not** implemented. The product had enough good Tier-1 families that paying the architectural cost of heterogeneous local line heights was unnecessary.

---

## 3. Extension #1 — fixed-width segmented text

### Goal

Keep one logical tksheet cell and one logical History criterion column, while rendering several independently hittable presentation slots.

### Core geometry

For a cell with left/right edges, `count` slots and a fixed `slot_width`:

```text
run_width = count * slot_width
run_left  = cell_left + (cell_width - run_width) / 2

slot N center =
    run_left + (N + 0.5) * slot_width
```

The fork exposes shared geometry helpers equivalent to:

```python
segmented_text_geometry(left, right, count, slot_width)
segmented_text_index(left, right, x, count, slot_width)
```

The renderer and hit tester use the **same geometry**.

### Important invariant

> **symbol position N = cell geometry + N**

It is never:

```text
width(glyph 0)
+ width(glyph 1)
+ ...
```

and never inferred from `Font.measure()`, glyph bbox or `find_closest()`.

### What this buys us

- five visible rating slots remain five deterministic hit targets;
- empty side margins are not rating targets;
- changing the table font does not change slot ordinal geometry;
- a different Unicode glyph cannot move neighbouring slots;
- the criterion remains one logical column;
- the History database still stores only the semantic integer rating.

For the Super-GUI rating control the product geometry is:

```text
criterion column width : 108 px
slot width             : 16 px
slot count             : 5
segmented run width    : 80 px
```

The generic renderer itself does **not** know what a rating, star, triangle, quality or issue is.

---

## 4. Extension #2 — local `font_style`

The next requirement was per-column / per-row / per-cell emphasis without duplicating tables or replacing cells with widgets.

### Resolution order

The local style follows the existing options model:

```text
cell
 ↓
row
 ↓
column
 ↓
global table font
```

Supported local style values are intentionally narrow:

```text
normal
bold
italic
bold italic
```

`None` removes the local override.

### What remains inherited

The style extension changes style only.

Family and size continue to come from the current global table font unless another qualified layer overrides family.

This matters for zoom: the local style does not freeze the original size.

### Measurement path

Every place that must understand body text resolves the same effective font:

```text
effective font
   ├─ measurement
   ├─ char-width cache
   ├─ wrap
   ├─ Canvas draw
   └─ body-cell autosize
```

Font-sensitive caches are keyed by the full `FontTuple`, not by one global font.

### Semantic detail

A local `"normal"` is meaningful. It means:

> force normal style here even if the global base is bold.

It is not treated as “no override”.

---

## 5. Extension #3 — Tier-1 local `font_family`

Once local style worked, the next question was whether the History grid could use genuinely different typefaces by column without rewriting the vertical layout engine.

The answer became **yes, conditionally and safely**.

### Resolution order

Family uses the same generic precedence:

```text
cell family
    ↓
row family
    ↓
column family
    ↓
global table family
```

Style is resolved independently.

Size remains live/global, so zoom still controls the active size.

The effective tuple is therefore conceptually:

```text
(effective_family, live_global_size, effective_style)
```

### Runtime authority

A static font catalogue is useful evidence, but it can never be the final authority because:

- installed fonts differ by host;
- Tk substitution differs by host;
- DPI / Tk scaling changes measurements;
- zoom can reach tuples that were not precomputed.

The final decision therefore happens **at render time** for the actual effective tuple.

The Tier-1 rule is:

```text
pipe(candidate_family, current_size, effective_style)
    <=
live table line box
```

### Fail-closed behaviour

If the requested family:

- resolves to another family;
- exceeds the live line box;
- cannot be measured safely;

the renderer falls back to the **base family** while preserving the intended live size/style where possible.

The product can therefore ask for a family without being allowed to break row geometry.

### Diagnostic states

The adapter exposes effective-state diagnostics such as:

```text
none
active
fallback
substituted
error
```

with measurements such as:

```text
pipe_height
line_box
overshoot
requested family
resolved family
reason
```

This is crucial for tooling: a font that visually “did not change” can be identified as an intentional runtime fallback rather than a broken setter.

---

## 6. Why Tier 2 is intentionally frozen

Tier 2 would mean allowing local fonts to demand different line heights inside the same table.

That is a qualitatively different problem.

It would affect:

- row-height ownership;
- multiline wrapping;
- autosize;
- vertical centring;
- row/index coordination;
- zoom;
- editor geometry;
- potentially every row containing a tall local font.

The Tier-1 runtime guard makes this unnecessary for the current product.

The rule is simple:

```text
fits existing line box -> use it
does not fit           -> fall back
```

The user still gets a broad typography palette without turning the table into a heterogeneous layout engine.

---

## 7. The font qualification pipeline

The fork does not hard-code a list of “good fonts”.

Instead, a standalone qualification probe measures the **same Tk/Canvas model that the renderer uses**.

### Measurement model

The probe reproduces the relevant runtime primitives, including:

- Canvas text bbox;
- pipe-glyph height;
- font-aware character width behaviour;
- style variants;
- Tk family resolution;
- width density;
- accumulated text-width drift.

The qualification criterion evolved during the investigation.

The important correction was:

```text
candidate height == base height
```

was too strict.

The correct Tier-1 safety rule is:

```text
candidate height <= base height
```

A shorter font is safe. Only an overshoot is dangerous.

### Policy vs authority

The qualification registry is explicitly:

```text
EVIDENCE_ONLY_NOT_RUNTIME_AUTHORITY
```

This distinction is deliberate.

A policy scan gives us a useful engineering/design map.

The live runtime guard remains authoritative.

### Current reference scan

The current Super-GUI scan was performed against:

```text
base family     : Segoe UI
reference size  : 10
policy window   : 7..20
Tk              : 8.6.12
Tk scaling      : ~1.333
platform        : Windows 11
```

The full scan enumerated 1024 Tk-visible families.

The designer then selected **95 families** for the product-facing shortlist.

The master catalogue retains the engineering evidence for all scanned families and marks the designer-selected subset.

---

## 8. Static evidence, visual judgement and runtime truth are different things

The font workflow deliberately separates three questions.

### Question A — can the family fit?

Answered by the qualification probe.

### Question B — does the family belong in this interface?

Answered by a visual comparator / viewer and human design judgement.

### Question C — is this exact effective tuple safe *right now*?

Answered by the runtime Tier-1 guard.

That produces the pipeline:

```text
installed Tk families
        │
        ▼
qualification probe
        │
        ├─ CSV evidence
        └─ registry
        │
        ▼
designer comparator / frozen specimens
        │
        ▼
designer selections
        │
        ▼
font_catalog.json
        │
        ▼
font_palette.json
        │
        ▼
Typography Lab on the real History grid
        │
        ▼
Tier-1 runtime guard
        │
     ┌──┴─────────────┐
     ▼                ▼
  ACTIVE          FALLBACK
```

This is more useful than either a hard-coded whitelist or a purely visual font picker.

---

## 9. Designer comparator and frozen PNG specimens

The standalone Python viewer belongs in the fork/tooling package.

Its purpose is not to replace qualification.

Its purpose is to answer the visual questions that numbers cannot answer well:

- legibility at the actual UI size;
- texture/rhythm;
- density;
- code-vs-prose character;
- relationship to the base font;
- behaviour of ambiguous glyphs;
- suitability for a particular visual role.

A useful principle established during the work is:

> **Numbers decide whether a face fits. The comparator helps decide whether it belongs.**

The viewer should travel with the repository because it makes the qualification evidence explorable.

The **frozen PNG contact sheets/specimens** should travel with it for a different reason: they are instant, dependency-free reference material and preserve what a particular review set looked like at the time.

They are not runtime authority and should be labelled accordingly.

Recommended documentation language:

```text
Python comparator
    interactive design/inspection tool

Frozen PNG specimens
    fast visual reference / provenance snapshot

Qualification CSV/registry
    host-specific engineering evidence

Runtime guard
    final authority for the current effective tuple
```

---

## 10. Master catalogue

The catalogue builder joins:

- full qualification CSV;
- qualification registry / host metadata;
- designer selection JSON files.

It does **not** invent a third verdict.

The engineering verdict comes from the probe.

The design band comes from the designer selection.

### Why the catalogue stores full per-size evidence

A field such as:

```text
safe_range = 10..20
```

means only:

> longest contiguous safe run

It does **not** mean:

> these are the only safe sizes

So the catalogue also carries exact per-size evidence, including unsafe sizes and contract overshoots.

### Why `families` became a JSON array

Some real Tk family names differ only in case/word spelling, for example variants such as:

```text
Source Sans Pro SemiBold
Source Sans Pro Semibold
```

Python can distinguish them as dictionary keys, but Windows PowerShell 5.1 `ConvertFrom-Json` treats object property names case-insensitively and rejects that shape as duplicate keys.

The catalogue schema therefore stores families as an **array of records**.

The Python layer rebuilds an exact-case index in memory.

This makes the JSON portable across the tools actually used by the project.

### Current catalogue checkpoint

```text
schema             : super-gui.font-catalog.v2.2
all families       : 1024
designer-picked    : 95
reference size     : 10
policy window      : 7..20
complete for picks : true
coverage gap       : none
```

Current catalogue SHA-256:

```text
B031A7A9D7B26D740454927935F24E72C80EE73FB5F78ACBAB66C4EA968D2B12
```

---

## 11. Typography roles and palettes

Typography roles are a **Super-GUI product layer**, not a tksheet concept.

They do not replace column IDs or colour zones.

The three concepts answer different questions:

```text
column id  -> what structural/data column is this?
zone       -> what colour/information zone does it belong to?
font role  -> what typography function does it share?
```

Role names are deliberately semantic and stable.

The accepted role syntax is:

```text
lowercase_ascii_snake_case
^[a-z][a-z0-9_]{0,39}$
```

A role is not:

- the current cell value;
- a font name;
- an arbitrary temporary label;
- necessarily identical to one column ID.

### Why roles matter

Without roles the product would store:

```text
column -> family
```

With roles it can store:

```text
column -> role -> family
```

Several columns can therefore move together when the designer changes one semantic type treatment.

This role system exists only in the typography/product layer.

The vendored renderer still knows only:

```text
column X requests family Y
```

---

## 12. Typography Lab — WYSIWYG on the real Python UI

The last stage turned the font pipeline into a live design system.

`Typography Lab` operates on the real `HistoryGrid`, not on a mockup.

Changing a family immediately redraws the actual History table with:

- real data;
- real column widths;
- real colour zones;
- real segmented ratings;
- real runtime font diagnostics.

This effectively gives a Tk/Python interface a small **WYSIWYG typography editor**.

### One UI, not two stages

Exploration and finalisation happen in the same panel.

The state model is:

```text
preview-only experiment
        ↓
working role palette
        ↓
checkpoint / load / revert
        ↓
canonical Save & Close
```

### Session controls

The final UI includes:

```text
Load palette…
Save checkpoint…
Revert saved

Base preview
Apply working
Refresh status

Save & Close
Cancel
```

The window-close path is transactional when the session is dirty:

```text
Save
Discard
Cancel
```

### Preview-only mode

If a column has no role, changing its family is a live experiment only.

It is intentionally not serialised into the product palette.

This lets the designer try dozens of typefaces without turning every experiment into configuration debt.

### Checkpoints

Intermediate palettes can be saved and later loaded into the same editor.

This turns the Lab into a small design-state editor rather than a one-shot preference dialog.

---

## 13. Product layering

The final architecture is intentionally split.

```text
┌──────────────────────────────────────────────────────┐
│ Super-GUI / product                                  │
│                                                      │
│  Typography Lab                                      │
│  font_palette.json                                   │
│  font_catalog.json                                   │
│  semantic font roles                                 │
│          │                                           │
│          ▼                                           │
│  kit_font_system.py                                  │
│          │                                           │
│          ▼                                           │
│  kit_grid.py adapter                                 │
└──────────┼───────────────────────────────────────────┘
           │ generic family/style/segment requests
           ▼
┌──────────────────────────────────────────────────────┐
│ vendored tksheet 7.6.0 controlled fork              │
│                                                      │
│  fixed-width segmented text                         │
│  local font_style                                    │
│  Tier-1 local font_family + runtime guard            │
└──────────────────────────────────────────────────────┘
```

This boundary is important.

There is **no** product-specific family name, rating concept, role name or Super-GUI colour-zone concept inside the generic renderer.

---

## 14. What belongs in the standalone fork repository

A GitHub publication should distinguish the generic fork from the Super-GUI-specific design layer.

A proposed repository layout:

```text
tksheet-controlled-fork/
│
├─ README.md
├─ LICENSE / upstream-license material
├─ UPSTREAM.md
├─ CHANGELOG.md
│
├─ tksheet/                         # pinned controlled fork
│  └─ ...
│
├─ patches/
│  ├─ 01-segmented-text.patch
│  ├─ 02-font-style.patch
│  └─ 03-font-family-tier1.patch
│
├─ qualification/
│  ├─ font_qualify_final_v2.py
│  ├─ README.md
│  └─ example-evidence/
│
├─ tools/
│  ├─ font_compare_final.py         # interactive Python viewer
│  └─ ...
│
├─ specimens/
│  ├─ README.md
│  └─ *.png                         # frozen quick-reference sheets
│
├─ tests/
│  ├─ segmented-text/
│  ├─ font-style/
│  └─ font-family-tier1/
│
└─ docs/
   ├─ architecture.md
   ├─ qualification-model.md
   └─ images/
```

Before publishing, the separate GitHub review should verify:

- upstream `tksheet` licence and required notices;
- exact upstream 7.6.0 provenance/commit or release artefact;
- which files are genuinely generic and which remain Super-GUI adapters;
- whether patches or full vendored files are the preferred public distribution;
- package/module naming;
- versioning policy for future upstream ports;
- font redistribution licensing for any specimen generation inputs.

The **font files themselves should not be bundled merely because the viewer references those families** unless their redistribution licences explicitly permit it.

Frozen PNG samples are much easier to distribute safely than arbitrary font binaries, but their content/licensing should still be reviewed before public release.

---

## 15. How the Acceleration Kit should consume it

Inside the Acceleration Kit / Super-GUI, the controlled fork should remain a pinned **embedded vendored component**.

That is more precise than calling it a separate Python environment.

The Kit can carry:

```text
kit_V100_16GB/
│
├─ vendor/
│  └─ tksheet/                # exact qualified controlled-fork snapshot
│
├─ kit_grid.py                # product adapter
├─ kit_font_system.py         # product typography layer
│
└─ font-catalog/
   ├─ font_catalog.json
   ├─ font_palette.json
   ├─ designer-picks/
   └─ palettes/
```

The embedded copy should be identified by exact hashes and never silently replaced by whatever `pip` happens to provide.

An upstream `tksheet` upgrade should be treated as a **port/requalification event**, not a routine dependency bump.

---

## 16. Qualification status at the current checkpoint

### Controlled-fork production files

```text
vendor/tksheet/main_table.py
74C5460EEE11BE377123EC5E989F0AB6CF37D51D3F985C6852D13EF197995BEC

vendor/tksheet/column_headers.py
8283498DC675937AB0EE2FFCCBE54552405D311A79BB01D296E110300C5BE8DB

kit_grid.py
6BFCA8196A23E045BCBBC0519BB55D49D7A35CB3B1707FD519AB49365875BD79
```

### Product typography layer

```text
kit_font_system.py
20E98DF392AF1EA08ECA97D687956948C319132529FC0F124004B214A6B60E34
```

### History integration hook

```text
kit_panel_tab_history.py
3DA2BCE104F1B926D1EAD5EF5C7295A5BDA4BE550D64FB41E23B6CFFCA33CE9C
```

### Master font catalogue

```text
font_catalog.json
B031A7A9D7B26D740454927935F24E72C80EE73FB5F78ACBAB66C4EA968D2B12
```

### Qualification checkpoints observed during the work

```text
segmented/rating regression controls : 25/25
font-family Tier-1 controls           : 39/39
Typography History hook controls      : 11/11
Typography final UI controls          : 20/20
Typography layer selftest             : all passed
```

Human live acceptance also confirmed that local designer-selected families visibly apply on the real History grid while the runtime diagnostics remain active.

---

## 17. Engineering rules worth keeping

Several rules from this work are broader than the specific feature set.

### Geometry should be assigned, not inferred, when the interaction is ordinal

For a five-slot control, the application already knows there are five slots.

That is stronger information than anything recoverable from glyph measurements.

### A renderer option is not complete until measurement follows it

A font override that only reaches `create_text()` is a rendering demo, not a table-engine feature.

### Static qualification is evidence; runtime is authority

Fonts, DPI, Tk and zoom make static whitelists brittle.

Use precomputation to guide the user, and a live guard to protect the engine.

### Preserve product semantics outside generic infrastructure

The renderer knows about segments and fonts.

The product knows about ratings, semantic roles, colour zones and designer palettes.

### Keep source states fail-closed

Production fork updates used exact known SHA states, backups, compile checks, regression controls and rollback.

That discipline is part of the fork, not incidental tooling.

---

## 18. What this fork is not

It is not:

- a full typography/layout engine;
- a per-cell arbitrary line-height system;
- a replacement for font coverage/cmap analysis;
- a promise that a font safe on one host is safe on every host;
- a hard-coded Super-GUI skin inside `tksheet`;
- a general-purpose WYSIWYG page designer.

It is a narrow set of table-engine primitives plus a host-aware design pipeline that made a previously rigid Tk table much more usable without abandoning its table semantics.

---

## 19. The practical outcome

The project started with questions such as:

```text
How do we make a five-symbol rating click exactly where it appears?

Can one column be bold while another remains normal?

Can code-like, narrow, serif or designer faces coexist in the same table?

Can we try them without editing Python and restarting the program?

Can a designer save, reload and compare intermediate typography states?
```

At the current checkpoint the answers are:

```text
deterministic segmented hit geometry       yes
local style                                yes
local family                               yes, Tier-1 guarded
runtime fail-closed fallback               yes
font qualification evidence                yes
designer shortlist / master catalogue      yes
real-grid live WYSIWYG fitting             yes
semantic role palette                      yes
checkpoint/load/revert workflow            yes
```

That is the real reason the controlled fork is useful.

The fork did not merely make `tksheet` “more customisable”.

It created a stable boundary where **interaction geometry, text rendering, font safety and product design can evolve independently**.

---

## 20. Next repository step

The next phase should be a separate publication/review round:

1. reconstruct the exact upstream `tksheet 7.6.0` base;
2. classify every changed file as generic fork / adapter / product layer;
3. produce clean patches against upstream;
4. assemble the qualification controls and sample evidence;
5. add the Python font viewer and selected frozen PNG specimen sheets;
6. write upstream/licence notices;
7. create a reproducible install/import example outside the Acceleration Kit;
8. define a fork version, e.g. `7.6.0-supergui.1` or another neutral project name;
9. publish only after a clean-room install reproduces the tests.

The Acceleration Kit can then embed that exact tagged snapshot rather than carrying an undocumented local modification.

---

### One-line summary

> A pinned `tksheet 7.6.0` controlled fork adds deterministic segmented-cell geometry and safe local typography; a separate qualification/catalogue/palette toolchain turns those primitives into a live WYSIWYG design workflow without putting product-specific semantics into the renderer.
