"""Super-GUI typography layer  (v2.3 FINAL-UI CANDIDATE).

Sits between the designer's font catalogue and the tksheet Tier-1 socket.
Nothing here knows how fonts are measured or drawn; nothing in the vendored
fork knows that Consolas exists.  Tier 1 is locked - this file never touches
vendor/tksheet.

    font_catalog.json    the workshop  - every candidate, with its evidence
    font_palette.json    the decision  - role -> family, for this product
    FontSystem           the panel     - the only place a family name lives
    TypographyLab        live fitting  - swap a face and see it, no restart

Two rules this file exists to keep:

1. The catalogue COPIES evidence, it never re-derives it.  The engine verdict
   comes from the qualification CSV, the design band comes from the designer's
   own selection files with their own thresholds.  A third formula invented
   here would silently disagree with both.

2. Headroom is not portability.  A face with 2px to spare on this host may
   still fall back on another, because a different DPI moves the base line box
   as well as the candidate.  Fields are named for what was measured
   (reference_contract_headroom, reference_normal_headroom, policy_worst_overshoot), never for a guarantee that
   was not.  The runtime guard in MainTable remains the only authority.

Build the catalogue from what the qualification already produced:

    python kit_font_system.py build \\
        --csv  _spikes/.../font_qualification_report.csv \\
        --picks "chosen_fonts*.json" \\
        --registry _spikes/.../font_registry.json \\
        --out  font-catalog/font_catalog.json

Check the layer without a grid:

    python kit_font_system.py selftest

Then, from the History tab:

    from kit_font_system import FontSystem
    fonts = FontSystem.load("font-catalog/font_catalog.json", "font_palette.json")
    fonts.apply(self.tree)
    fonts.open_lab(self.tree, master=top)
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime
import glob
import json
import re
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


def _pick(row: dict, *names: str, default: str = "") -> str:
    for n in names:
        if n in row and row[n] != "":
            return row[n]
    return default


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _by_size(text: str) -> dict:
    """Parse a 'size:value size:value' evidence column into {int: int}."""
    out = {}
    for part in str(text).replace(",", " ").split():
        if ":" in part:
            k, _, v = part.partition(":")
            try:
                out[int(k)] = int(v)
            except ValueError:
                continue
    return out


def _sizes(text: str) -> list[int]:
    out = []
    for part in str(text).replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            continue
    return sorted(out)


def _families_index(raw) -> dict[str, dict]:
    """Normalize catalogue storage to an exact-case in-memory index.

    v2.2 stores families as a JSON ARRAY of records, not an object keyed by
    family name.  This avoids case-insensitive JSON consumers (notably Windows
    PowerShell 5.1 ConvertFrom-Json) rejecting legitimate names such as
    "Source Sans Pro SemiBold" and "Source Sans Pro Semibold".

    Older v2/v2.1 object-shaped catalogues remain readable.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fam = rec.get("family")
        if not isinstance(fam, str) or not fam:
            continue
        if fam in out:
            raise ValueError(f"duplicate exact family in catalogue: {fam!r}")
        item = dict(rec)
        item.pop("family", None)
        out[fam] = item
    return out


# ---------------------------------------------------------------- catalogue


def build_catalog(csv_path: Path, pick_paths: list[Path], out: Path,
                  registry_path: Path | None = None, work_size: int | None = None) -> dict:
    """Fold the qualification CSV and the designer selections into one file.

    Per-size safety evidence is preserved without collapsing it to safe_range.
    safe_range is only the longest CONTIGUOUS run, so a face safe at
    11,13,15,16,19,20 may still show 15..16 as its longest run.  The
    canonical CSV row is also retained verbatim under qualification_row.
    """
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))

    base_font, base_size, host = None, work_size, None
    probe_schema, policy_window = None, None
    if registry_path and registry_path.exists():
        try:
            reg = json.loads(registry_path.read_text(encoding="utf-8"))
            base_font = reg.get("base_font")
            # Canonical qualification v3 calls this work_size, not base_size.
            base_size = base_size or reg.get("work_size") or reg.get("base_size")
            host = reg.get("host")
            probe_schema = reg.get("schema")
            policy_window = reg.get("policy_window")
        except Exception as exc:
            print(f"  registry unreadable ({exc}); continuing without it")
    base_size = base_size or 10

    # designer selections: band and thresholds are THEIRS, copied not recomputed
    picks: dict[str, dict] = {}
    thresholds: dict = {}
    for p in pick_paths:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  skip {p.name}: {exc}")
            continue
        incoming_thresholds = doc.get("band_thresholds") or {}
        if incoming_thresholds:
            if thresholds and incoming_thresholds != thresholds:
                raise ValueError(
                    f"conflicting band_thresholds: {p} has {incoming_thresholds}, "
                    f"previous selections used {thresholds}"
                )
            thresholds = incoming_thresholds
        for entry in doc.get("chosen", []):
            fam = entry.get("family")
            if not fam:
                continue
            rec = picks.setdefault(
                fam,
                {"sources": [], "design_band": None, "designer_notes": {}, "selection_contexts": []},
            )
            rec["sources"].append(p.stem)
            band = entry.get("design_band")
            if band:
                if rec["design_band"] and rec["design_band"] != band:
                    raise ValueError(
                        f"conflicting design_band for {fam!r}: "
                        f"{rec['design_band']!r} vs {band!r} in {p}"
                    )
                rec["design_band"] = band
            rec["selection_contexts"].append({
                "source": p.stem,
                "size_reviewed": doc.get("size_reviewed"),
                "style_reviewed": doc.get("style_reviewed"),
                "specimen_preset": doc.get("specimen_preset"),
            })
            for k in ("current_size_status", "engine_verdict"):
                if entry.get(k):
                    rec["designer_notes"][k] = entry[k]

    families = {}
    for r in rows:
        fam = _pick(r, "family")
        if not fam:
            continue
        deltas = _by_size(_pick(r, "normal_deltas", "deltas"))
        contract = _by_size(_pick(r, "contract_overshoots")) or deltas
        worst = int(_num(_pick(r, "contract_max_overshoot", "max_overshoot",
                               "normal_max_overshoot", default="0")))
        contract_at_work = contract.get(base_size, deltas.get(base_size))
        normal_at_work = deltas.get(base_size)
        lo = _pick(r, "safe_range_lo", "range_lo")
        hi = _pick(r, "safe_range_hi", "range_hi")
        rec = picks.get(fam, {})
        families[fam] = {
            # --- engine evidence, copied verbatim -------------------------
            "engine_verdict": _pick(r, "verdict"),
            "safe_count": int(_num(_pick(r, "safe_count", "range_len"))),
            "total_sizes": int(_num(_pick(r, "total_sizes"), len(deltas) or 0)),
            "safe_range": [int(_num(lo)), int(_num(hi))] if lo and hi else None,
            "safe_range_note": "longest CONTIGUOUS run only - see contract_by_size",
            "unsafe_sizes": _sizes(_pick(r, "unsafe_sizes")),
            "contract_by_size": {str(k): v for k, v in sorted(contract.items())},
            "policy_worst_overshoot": worst,
            # --- measured at the working size, on THIS host ---------------
            # contract = worst of the allowed local styles at this size;
            # normal = the normal-style measurement specifically.
            "reference_contract_headroom": (
                None if contract_at_work is None else -contract_at_work
            ),
            "reference_normal_headroom": (
                None if normal_at_work is None else -normal_at_work
            ),
            "reference_size": base_size,
            "mono": _pick(r, "mono") in ("1", "True", "true", "Y"),
            "density": round(_num(_pick(r, "width_ratio"), 0.0), 4),
            "cyrillic_hint": _pick(r, "cyrillic_plausible") == "1",
            "style_fail_at": _sizes(_pick(r, "style_fail_at")),
            # --- designer decision, copied verbatim -----------------------
            "design_band": rec.get("design_band"),
            "designer_picked": sorted(set(rec.get("sources", []))) or None,
            "designer_notes": rec.get("designer_notes") or None,
            "selection_contexts": rec.get("selection_contexts") or None,
            # Preserve the canonical source row verbatim as provenance.
            "qualification_row": dict(r),
        }

    family_records = [{"family": fam, **rec} for fam, rec in families.items()]
    doc = {
        "schema": "super-gui.font-catalog.v2.2",
        "source_csv": str(csv_path),
        "base_font": base_font,
        "reference_size": base_size,
        "source_probe_schema": probe_schema,
        "policy_window": policy_window,
        "host": host,
        "designer_band_thresholds": thresholds or None,
        "note": (
            "Evidence copied from the qualification probe, not re-derived. "
            "engine_verdict is the probe's; design_band is the designer's, under "
            "designer_band_thresholds. reference_contract_headroom is the worst-style "
            "headroom at reference_size; reference_normal_headroom is normal-style headroom. "
            "Both are measurements ON THIS HOST, not portability guarantees: another DPI "
            "moves the base line box too. Families are stored as an ARRAY (not object keys) "
            "so case-insensitive JSON consumers can preserve distinct Tk family names. The "
            "runtime guard in MainTable is the only authority."
        ),
        "family_count": len(family_records),
        "families": family_records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)

    picked = [f for f, v in families.items() if v["designer_picked"]]
    print(f"catalogue: {len(families)} families, {len(picked)} designer-picked "
          f"(reference size {base_size})")

    # A selection with no evidence row is the tell-tale of a partial CSV -
    # usually one produced with --only-mono while the picks were made from the
    # full report.  Say so rather than silently dropping the face.
    orphans = sorted(set(picks) - set(families))
    doc["complete_for_designer_picks"] = not bool(orphans)
    if orphans:
        note = (f"{len(orphans)} selected families have no row in this CSV; "
                f"rebuild against the full report (no --only-mono)")
        print(f"  WARNING: {note}")
        print("  " + ", ".join(orphans[:8]) + (" ..." if len(orphans) > 8 else ""))
        doc["coverage_gap"] = {"missing_evidence": orphans, "note": note}

    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return doc



PALETTE_SCHEMA = "super-gui.font-palette.v1"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _empty_palette() -> dict:
    return {"schema": PALETTE_SCHEMA, "roles": {}, "columns": {}}


def _valid_role_name(role: str) -> bool:
    return bool(ROLE_NAME_RE.fullmatch(str(role or "")))


# -------------------------------------------------------------- font system


class FontSystem:
    """role -> family, and the only place in the product a face is named."""

    def __init__(self, catalog: dict, palette: dict, palette_path: Path | None = None):
        self.catalog = catalog
        self.families: dict[str, dict] = _families_index(catalog.get("families", {}))
        self.palette: dict = palette
        self.palette_path = palette_path

    @classmethod
    def load(cls, catalog_path, palette_path) -> "FontSystem":
        cp, pp = Path(catalog_path), Path(palette_path)
        catalog = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {"families": {}}
        raw_palette = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else _empty_palette()
        obj = cls(catalog, _empty_palette(), pp)
        obj.palette = obj.validate_palette(raw_palette)
        return obj

    def validate_palette(self, palette: dict) -> dict:
        """Validate the product-layer contract without touching the grid."""
        if not isinstance(palette, dict):
            raise ValueError("palette root must be a JSON object")
        roles = palette.get("roles", {})
        columns = palette.get("columns", {})
        if not isinstance(roles, dict) or not isinstance(columns, dict):
            raise ValueError("palette roles and columns must be JSON objects")

        clean_roles: dict[str, str] = {}
        for role, family in roles.items():
            role = str(role)
            if not _valid_role_name(role):
                raise ValueError(
                    f"invalid role {role!r}; use lowercase_ascii_snake_case "
                    "(letter first, max 40 chars)"
                )
            if family in (None, ""):
                continue
            family = str(family)
            if family not in self.families:
                raise ValueError(f"palette role {role!r} names unknown family {family!r}")
            clean_roles[role] = family

        clean_columns: dict[str, str] = {}
        for column_id, role in columns.items():
            column_id = str(column_id)
            role = str(role)
            if not column_id:
                raise ValueError("palette contains an empty column id")
            if not _valid_role_name(role):
                raise ValueError(
                    f"column {column_id!r} has invalid role {role!r}; "
                    "use lowercase_ascii_snake_case"
                )
            clean_columns[column_id] = role

        return {
            "schema": PALETTE_SCHEMA,
            "roles": clean_roles,
            "columns": clean_columns,
        }

    def read_palette(self, path) -> dict:
        p = Path(path)
        return self.validate_palette(json.loads(p.read_text(encoding="utf-8")))

    def replace_palette(self, palette: dict) -> None:
        self.palette = self.validate_palette(copy.deepcopy(palette))

    # -- palette reads -----------------------------------------------------
    def roles(self) -> dict:
        return self.palette.setdefault("roles", {})

    def columns(self) -> dict:
        return self.palette.setdefault("columns", {})

    def family_for(self, role: str | None) -> str | None:
        return self.roles().get(role) if role else None

    def role_of_column(self, column_id: str) -> str | None:
        return self.columns().get(column_id)

    def columns_with_role(self, role: str) -> list[str]:
        return [c for c, r in self.columns().items() if r == role]

    # -- palette writes ----------------------------------------------------
    def assign_column(self, column_id: str, role: str | None) -> None:
        cols = self.columns()
        if role:
            cols[column_id] = role
        else:
            cols.pop(column_id, None)

    def set_role(self, role: str, family: str | None, grid=None,
                 *, redraw: bool = True) -> list[str]:
        """Point a role at a family and push it to EVERY column holding it.

        A role that applied to only the column you happened to click would
        make the live grid disagree with the palette the moment you saved and
        reloaded - two columns share the role, one shows the new face, both
        get it after restart.  Returns the columns that changed.
        """
        roles = self.roles()
        if family is None:
            roles.pop(role, None)
        else:
            roles[role] = family
        touched = self.columns_with_role(role)
        if grid is not None:
            for column_id in touched:
                grid.set_column_font_family(column_id, family, redraw=False)
            if redraw and touched:
                grid.sheet.redraw()
        return touched

    # -- application -------------------------------------------------------
    def apply(self, grid, *, redraw: bool = True) -> dict:
        """Replace live family overrides with the whole palette.

        FontSystem owns product-level family overrides.  Clearing first prevents a
        column removed from the palette from retaining a stale live family until the
        next process restart.
        """
        for column_id in getattr(grid, "_ids", list(self.columns())):
            grid.set_column_font_family(column_id, None, redraw=False)
        for column_id, role in self.columns().items():
            grid.set_column_font_family(column_id, self.family_for(role), redraw=False)
        if redraw:
            grid.sheet.redraw()
        return {c: grid.column_font_effective(c) for c in self.columns()}

    def clear_live(self, grid, *, redraw: bool = True) -> None:
        """Drop overrides from the grid WITHOUT touching the palette."""
        for column_id in getattr(grid, "_ids", list(self.columns())):
            grid.set_column_font_family(column_id, None, redraw=False)
        if redraw:
            grid.sheet.redraw()

    def save(self, path=None) -> Path:
        p = Path(path or self.palette_path or "font_palette.json")
        clean = self.validate_palette(self.palette)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        self.palette = clean
        return p

    # -- candidate lists ---------------------------------------------------
    def candidates(self, *, only_picked: bool = True, mono: bool | None = None) -> list[str]:
        out = []
        for fam, v in self.families.items():
            if only_picked and not v.get("designer_picked"):
                continue
            if mono is not None and bool(v.get("mono")) is not mono:
                continue
            out.append(fam)
        order = {"A": 0, "B": 1, "C": 2, "D": 3, None: 4}
        return sorted(out, key=lambda f: (order.get(self.families[f].get("design_band"), 4),
                                          self.families[f].get("density") or 9, f))

    def summary(self, family: str) -> str:
        v = self.families.get(family)
        if not v:
            return "not in catalogue"
        bits = []
        if v.get("design_band"):
            bits.append(f"band {v['design_band']}")
        if v.get("engine_verdict"):
            bits.append(v["engine_verdict"])
        chr_ = v.get("reference_contract_headroom")
        nhr = v.get("reference_normal_headroom")
        if chr_ is not None:
            bits.append(f"contract headroom {chr_:+d}px @{v.get('reference_size')}")
        if nhr is not None and nhr != chr_:
            bits.append(f"normal {nhr:+d}px")
        if v.get("density"):
            bits.append(f"density {v['density']}")
        if v.get("unsafe_sizes"):
            bits.append(f"unsafe at {','.join(map(str, v['unsafe_sizes']))}")
        return "  ".join(bits)

    def open_lab(self, grid, master=None) -> "TypographyLab":
        return TypographyLab(self, grid, master)


# ------------------------------------------------------------ live fitting


class TypographyLab:
    """One WYSIWYG editor for exploration, checkpoints and final palette commit.

    There are no stage-1/stage-2 windows.  A family chosen on a column with no
    role is a preview-only experiment.  Assigning a semantic role turns that
    experiment into palette state.  The same window can load/save checkpoints,
    compare against base, and finally commit the canonical font_palette.json.
    """

    def __init__(self, system: FontSystem, grid, master=None):
        self.sys = system
        self.grid = grid
        self.top = tk.Toplevel(master) if master is not None else tk.Toplevel()
        self.top.title("Typography Lab — WYSIWYG")
        self.top.protocol("WM_DELETE_WINDOW", self._window_close)
        try:
            self.top.transient(master.winfo_toplevel() if master is not None else None)
        except Exception:
            pass
        self.top.minsize(900, 520)
        self.top.geometry("1240x760")

        self.rows: dict[str, dict] = {}
        self._opening_palette = copy.deepcopy(self.sys.palette)
        self._preview_columns: set[str] = set()
        self._base_preview_active = False
        self._working_source = (
            self.sys.palette_path.name
            if self.sys.palette_path and Path(self.sys.palette_path).exists()
            else "new palette"
        )
        self._last_checkpoint: Path | None = None
        self._show_internal_var = tk.BooleanVar(value=False)

        self._build()
        self._sync_controls_from_palette()
        self.refresh()
        self._update_session_banner()

    # -- catalogue / row helpers ------------------------------------------
    def _families(self) -> list[str]:
        picked = self.sys.candidates(only_picked=True)
        return ["(base)"] + (picked or self.sys.candidates(only_picked=False))

    def _role_values(self) -> list[str]:
        roles = set(self.sys.roles())
        roles.update(
            str(r["role"].get()).strip()
            for r in self.rows.values()
            if str(r["role"].get()).strip()
        )
        return sorted(r for r in roles if _valid_role_name(r))

    def _title_for(self, column_id: str) -> str:
        title = str(getattr(self.grid, "_titles", {}).get(column_id, "") or "").strip()
        return title

    def _is_internal(self, column_id: str) -> bool:
        return not bool(self._title_for(column_id))

    def _display_title(self, column_id: str) -> str:
        title = self._title_for(column_id)
        return title if title else f"[{column_id}]"

    # -- UI ---------------------------------------------------------------
    def _build(self) -> None:
        # Session/state controls: one panel owns both exploratory and final work.
        state = ttk.Frame(self.top)
        state.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Button(state, text="Load palette…", command=self._load_palette).pack(side="left")
        ttk.Button(state, text="Save checkpoint…", command=self._save_checkpoint).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(state, text="Revert saved", command=self._revert_saved).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(state, text="Base preview", command=self._base_preview).pack(
            side="left", padx=(14, 0)
        )
        ttk.Button(state, text="Apply working", command=self._apply_working).pack(
            side="left", padx=(5, 0)
        )
        ttk.Button(state, text="Refresh status", command=self.refresh).pack(
            side="left", padx=(5, 0)
        )

        self.session_label = ttk.Label(state, text="", anchor="e")
        self.session_label.pack(side="right", padx=(12, 0))

        hint = ttk.Frame(self.top)
        hint.pack(fill="x", padx=10, pady=(0, 5))
        ttk.Label(
            hint,
            text="role = stable semantic token (lowercase_ascii_snake_case); "
                 "empty role = preview only",
            anchor="w",
        ).pack(side="left")
        ttk.Checkbutton(
            hint,
            text="Show internal columns",
            variable=self._show_internal_var,
            command=self._refresh_row_visibility,
        ).pack(side="right")

        # Column headers.
        head = ttk.Frame(self.top)
        head.pack(fill="x", padx=10, pady=(0, 2))
        for i, (txt, w) in enumerate((("column", 21), ("role", 18), ("family", 30))):
            ttk.Label(head, text=txt, width=w, anchor="w").grid(row=0, column=i, sticky="w")
        ttk.Label(head, text="engine / catalogue evidence", anchor="w").grid(
            row=0, column=3, sticky="w"
        )

        # Scrollable row matrix.  Hidden structural criterion slots no longer make
        # the Lab taller than the screen; they are available through the checkbox.
        holder = ttk.Frame(self.top)
        holder.pack(fill="both", expand=True, padx=10)

        bg = self.top.cget("bg")
        self._canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0, bg=bg)
        vscroll = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        hscroll = ttk.Scrollbar(holder, orient="horizontal", command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self._canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)

        self._body = tk.Frame(self._canvas, bg=bg)
        self._body_window = self._canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._body.bind("<Configure>", self._body_configured)
        self._canvas.bind("<Configure>", self._canvas_configured)
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

        family_values = self._families()
        for i, column_id in enumerate(getattr(self.grid, "_ids", ())):
            widgets: list[tk.Widget] = []

            title = tk.Label(
                self._body, text=self._display_title(column_id)[:24],
                width=21, anchor="w", bg=bg,
            )
            title.grid(row=i, column=0, sticky="w")
            widgets.append(title)

            role_var = tk.StringVar(value="")
            role_box = ttk.Combobox(
                self._body, width=18, textvariable=role_var,
                values=(), state="normal",
            )
            role_box.grid(row=i, column=1, padx=(0, 6), sticky="ew")
            role_box.bind("<FocusOut>", lambda _e, c=column_id: self._commit_role(c))
            role_box.bind("<Return>", lambda _e, c=column_id: self._commit_role(c))
            role_box.bind("<<ComboboxSelected>>", lambda _e, c=column_id: self._commit_role(c))
            widgets.append(role_box)

            fam_var = tk.StringVar(value="(base)")
            family_box = ttk.Combobox(
                self._body, width=29, textvariable=fam_var,
                values=family_values, state="readonly",
            )
            family_box.grid(row=i, column=2, padx=(0, 8), sticky="ew")
            family_box.bind("<<ComboboxSelected>>", lambda _e, c=column_id: self._choose(c))
            widgets.append(family_box)

            status = tk.Label(self._body, text="", anchor="w", bg=bg)
            status.grid(row=i, column=3, sticky="w")
            widgets.append(status)

            self.rows[column_id] = {
                "role": role_var,
                "role_box": role_box,
                "family": fam_var,
                "family_box": family_box,
                "status": status,
                "widgets": widgets,
                "internal": self._is_internal(column_id),
            }

        self._body.grid_columnconfigure(3, weight=1)
        self._sync_role_choices()
        self._refresh_row_visibility()

        # Bottom status + transactional session actions.
        self.note = ttk.Label(self.top, text="", anchor="w")
        self.note.pack(fill="x", padx=10, pady=(6, 2))

        actions = ttk.Frame(self.top)
        actions.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(
            actions,
            text=("TALL/substituted families fall back to base; preview-only columns are "
                  "never written to the palette."),
            anchor="w",
        ).pack(side="left")

        ttk.Button(actions, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(actions, text="Save & Close", command=self._save_and_close).pack(
            side="right", padx=(0, 6)
        )

    def _body_configured(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _canvas_configured(self, event) -> None:
        # Let the row matrix use the viewport width while keeping horizontal
        # scrolling available if long evidence text needs more.
        try:
            req = self._body.winfo_reqwidth()
            self._canvas.itemconfigure(self._body_window, width=max(event.width, req))
        except Exception:
            pass

    def _bind_mousewheel(self, _event=None) -> None:
        self._canvas.bind_all("<MouseWheel>", self._mousewheel, add="+")

    def _unbind_mousewheel(self, _event=None) -> None:
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

    def _mousewheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _refresh_row_visibility(self) -> None:
        show_internal = bool(self._show_internal_var.get())
        for row in self.rows.values():
            visible = show_internal or not row["internal"]
            for widget in row["widgets"]:
                if visible:
                    widget.grid()
                else:
                    widget.grid_remove()
        self.top.after_idle(self._body_configured)

    # -- session state -----------------------------------------------------
    def _palette_dirty(self) -> bool:
        return self.sys.palette != self._opening_palette

    def _has_session_changes(self) -> bool:
        return (
            self._palette_dirty()
            or bool(self._preview_columns)
            or bool(self._base_preview_active)
        )

    def _update_session_banner(self) -> None:
        canonical = (
            str(self.sys.palette_path.name)
            if self.sys.palette_path is not None else "font_palette.json"
        )
        flags = []
        if self._palette_dirty():
            flags.append("modified")
        if self._preview_columns:
            flags.append(f"preview-only {len(self._preview_columns)}")
        if self._base_preview_active:
            flags.append("base preview")
        state = ", ".join(flags) if flags else "clean"
        self.session_label.config(
            text=f"canonical: {canonical}  |  source: {self._working_source}  |  {state}"
        )

    def _set_note(self, text: str) -> None:
        self.note.config(text=text)
        self._update_session_banner()

    def _sync_role_choices(self) -> None:
        values = self._role_values()
        for row in self.rows.values():
            row["role_box"].configure(values=values)

    def _sync_controls_from_palette(self) -> None:
        for column_id, row in self.rows.items():
            role = self.sys.role_of_column(column_id)
            row["role"].set(role or "")
            row["family"].set(self.sys.family_for(role) or "(base)")
        self._sync_role_choices()

    def _flush_role_entries(self) -> bool:
        # Buttons normally trigger FocusOut first, but finalization must not rely
        # on focus-manager ordering.
        for column_id, row in self.rows.items():
            proposed = row["role"].get().strip()
            current = self.sys.role_of_column(column_id) or ""
            if proposed != current and not self._commit_role(column_id, show_error=True):
                return False
        return True

    # -- role / family editing --------------------------------------------
    def _commit_role(self, column_id: str, *, show_error: bool = True) -> bool:
        row = self.rows[column_id]
        role = row["role"].get().strip()
        previous = self.sys.role_of_column(column_id) or ""

        if role and not _valid_role_name(role):
            row["role"].set(previous)
            if show_error:
                messagebox.showerror(
                    "Invalid typography role",
                    "Use lowercase_ascii_snake_case.\n\n"
                    "Examples: metadata, long_text, numeric, annotation, rating",
                    parent=self.top,
                )
            self._set_note(f"{column_id}: invalid role rejected")
            return False

        if role == previous:
            return True

        self.sys.assign_column(column_id, role or None)
        self._preview_columns.discard(column_id)
        self._base_preview_active = False

        family = self.sys.family_for(role) if role else None
        row["family"].set(family or "(base)")
        self.grid.set_column_font_family(column_id, family, redraw=True)

        self._sync_role_choices()
        self.refresh()
        self._set_note(
            f"{column_id}: role -> {role or '(none)'}"
        )
        return True

    def _choose(self, column_id: str) -> None:
        if not self._commit_role(column_id):
            return

        row = self.rows[column_id]
        fam = row["family"].get()
        family = None if fam == "(base)" else fam
        role = row["role"].get().strip()

        self._base_preview_active = False
        if not role:
            self.grid.set_column_font_family(column_id, family, redraw=True)
            if family is None:
                self._preview_columns.discard(column_id)
            else:
                self._preview_columns.add(column_id)
            self._set_note(f"{column_id}: preview only, no role assigned")
        else:
            self.sys.assign_column(column_id, role)
            touched = self.sys.set_role(role, family, self.grid)
            for c in touched:
                self._preview_columns.discard(c)
                if c in self.rows:
                    self.rows[c]["family"].set(fam)
            self._set_note(
                f"role '{role}' -> {fam}  ({len(touched)} column(s))"
            )
        self.refresh()

    # -- engine status ----------------------------------------------------
    def refresh(self) -> None:
        """Re-read the runtime guard for every column. Verdicts change with zoom."""
        for column_id, row in self.rows.items():
            rep = self.grid.column_font_effective(column_id)
            st = rep.get("status", "?")
            colour = {
                "active": "#186a18",
                "fallback": "#a05000",
                "substituted": "#a05000",
                "error": "#a00000",
                "none": "#888",
            }.get(st, "#555")
            extra = ""
            if "pipe_height" in rep:
                extra = (
                    f"  {rep['pipe_height']}/{rep['line_box']}px"
                    f"  over={rep['overshoot']:+d}"
                )
            fam = row["family"].get()
            if fam and fam != "(base)":
                cat = self.sys.summary(fam)
                if cat and cat != "not in catalogue":
                    extra += f"   [{cat}]"
            row["status"].config(text=f"{st}{extra}", fg=colour)
        self._update_session_banner()

    # -- working-state controls -------------------------------------------
    def _apply_working(self) -> None:
        if not self._flush_role_entries():
            return
        self.sys.apply(self.grid)
        self._preview_columns.clear()
        self._base_preview_active = False
        self._sync_controls_from_palette()
        self.refresh()
        self._set_note("working palette applied; preview-only overrides cleared")

    def _base_preview(self) -> None:
        self.sys.clear_live(self.grid)
        self._preview_columns.clear()
        self._base_preview_active = True
        self.refresh()
        self._set_note("base preview — working palette kept in memory")

    def _canonical_palette(self) -> dict:
        if self.sys.palette_path and Path(self.sys.palette_path).exists():
            return self.sys.read_palette(self.sys.palette_path)
        return _empty_palette()

    def _revert_saved(self) -> None:
        try:
            self.sys.replace_palette(self._canonical_palette())
            self.sys.apply(self.grid)
        except Exception as exc:
            messagebox.showerror("Revert saved palette", str(exc), parent=self.top)
            return
        self._preview_columns.clear()
        self._base_preview_active = False
        self._working_source = (
            self.sys.palette_path.name if self.sys.palette_path else "font_palette.json"
        )
        self._sync_controls_from_palette()
        self.refresh()
        self._set_note("reverted to canonical saved palette")

    def _palette_dialog_dir(self) -> Path:
        base = (
            Path(self.sys.palette_path).parent
            if self.sys.palette_path is not None else Path.cwd()
        )
        checkpoints = base / "palettes"
        checkpoints.mkdir(parents=True, exist_ok=True)
        return checkpoints

    def _load_palette(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.top,
            title="Load typography palette",
            initialdir=str(self._palette_dialog_dir()),
            filetypes=(("Typography palette", "*.json"), ("JSON", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            self.sys.replace_palette(self.sys.read_palette(path))
            self.sys.apply(self.grid)
        except Exception as exc:
            messagebox.showerror("Load typography palette", str(exc), parent=self.top)
            return
        self._preview_columns.clear()
        self._base_preview_active = False
        self._working_source = Path(path).name
        self._sync_controls_from_palette()
        self.refresh()
        self._set_note(f"loaded {Path(path).name} into working state")

    def _save_checkpoint(self) -> None:
        if not self._flush_role_entries():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            parent=self.top,
            title="Save typography checkpoint",
            initialdir=str(self._palette_dialog_dir()),
            initialfile=f"font_palette_{stamp}.json",
            defaultextension=".json",
            filetypes=(("Typography palette", "*.json"), ("JSON", "*.json")),
        )
        if not path:
            return
        try:
            saved = self.sys.save(path)
        except Exception as exc:
            messagebox.showerror("Save typography checkpoint", str(exc), parent=self.top)
            return
        self._last_checkpoint = saved
        suffix = (
            f"; {len(self._preview_columns)} preview-only column(s) were not included"
            if self._preview_columns else ""
        )
        self._set_note(f"checkpoint saved: {saved.name}{suffix}")

    # -- finalization / close semantics -----------------------------------
    def _save_and_close(self) -> bool:
        if not self._flush_role_entries():
            return False

        if self._preview_columns:
            names = ", ".join(sorted(self._preview_columns))
            if not messagebox.askyesno(
                "Preview-only changes",
                "These columns have live font previews but no typography role, "
                "so they cannot be saved in the palette:\n\n"
                f"{names}\n\n"
                "Save the role-based palette and discard those previews?",
                parent=self.top,
            ):
                return False

        try:
            # Final live state must exactly match what is written to disk.
            self.sys.apply(self.grid)
            saved = self.sys.save()
        except Exception as exc:
            messagebox.showerror("Save typography palette", str(exc), parent=self.top)
            return False

        self._preview_columns.clear()
        self._base_preview_active = False
        self._opening_palette = copy.deepcopy(self.sys.palette)
        self._working_source = saved.name
        self.top.destroy()
        return True

    def _cancel(self) -> None:
        # Transactional cancel: restore exactly the palette that was active when
        # this Lab session opened; checkpoint files already written remain files.
        self.sys.replace_palette(copy.deepcopy(self._opening_palette))
        self.sys.apply(self.grid)
        self._preview_columns.clear()
        self._base_preview_active = False
        self.top.destroy()

    def _window_close(self) -> None:
        if not self._has_session_changes():
            self.top.destroy()
            return
        answer = messagebox.askyesnocancel(
            "Close Typography Lab",
            "Save the working typography palette before closing?\n\n"
            "Yes = Save & Close\n"
            "No = discard this session and restore its opening state\n"
            "Cancel = keep editing",
            parent=self.top,
        )
        if answer is True:
            self._save_and_close()
        elif answer is False:
            self._cancel()


# ------------------------------------------------------------------ selftest


class _MockSheet:
    def __init__(self):
        self.redraws = 0

    def redraw(self):
        self.redraws += 1


class _MockGrid:
    """Stands in for KitGrid: same surface the layer uses, nothing more."""

    TALL = ("Nunito", "Source Code Pro", "Sitka")

    def __init__(self):
        self._ids = ["run", "model", "details", "notes"]
        self._titles = {c: c.title() for c in self._ids}
        self.sheet = _MockSheet()
        self.fams: dict[str, str | None] = {}

    def set_column_font_family(self, column_id, family, redraw=False):
        self.fams[column_id] = family
        if redraw:
            self.sheet.redraw()

    def column_font_effective(self, column_id):
        f = self.fams.get(column_id)
        if not f:
            return {"status": "none", "reason": "no override"}
        tall = f.startswith(self.TALL)
        return {"status": "fallback" if tall else "active",
                "pipe_height": 18 if tall else 15, "line_box": 17,
                "overshoot": 1 if tall else -2, "reason": "mock"}


def selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    catalog = {"families": {
        "Consolas": {"design_band": "A", "engine_verdict": "SAFE", "mono": True,
                     "density": 1.1729, "reference_contract_headroom": 2,
                     "reference_normal_headroom": 2, "reference_size": 10,
                     "policy_worst_overshoot": 0, "unsafe_sizes": [],
                     "designer_picked": ["mono"]},
        "Cascadia Mono": {"design_band": "B", "engine_verdict": "SAFE-RANGE", "mono": True,
                          "density": 1.3405, "reference_contract_headroom": 0,
                          "reference_normal_headroom": 0, "reference_size": 10,
                          "policy_worst_overshoot": 2, "unsafe_sizes": [8, 9],
                          "designer_picked": ["mono"]},
        "Nunito Black": {"design_band": "C", "engine_verdict": "REJECTED-TALLER", "mono": False,
                         "density": 1.1165, "reference_contract_headroom": -1,
                         "reference_normal_headroom": -1, "reference_size": 10,
                         "policy_worst_overshoot": 2, "unsafe_sizes": [7, 8, 9, 10],
                         "designer_picked": ["diz-003"]},
    }}

    with tempfile.TemporaryDirectory(prefix="super-gui-font-selftest-") as td:
        td = Path(td)
        palette_path = td / "palette.json"
        catalog_path = td / "catalog.json"

        sysf = FontSystem(catalog, {"roles": {}, "columns": {}}, palette_path)
        grid = _MockGrid()

        print("-- catalogue is not re-derived")
        check("bands are the designer's, not recomputed",
              sysf.families["Nunito Black"]["design_band"] == "C")
        check("worst-case and reference headroom are separate fields",
              sysf.families["Consolas"]["policy_worst_overshoot"] == 0
              and sysf.families["Consolas"]["reference_contract_headroom"] == 2)
        check("no ship_safe verdict is invented",
              all("ship_safe" not in v for v in sysf.families.values()))
        check("per-size evidence survives",
              sysf.families["Cascadia Mono"]["unsafe_sizes"] == [8, 9])

        print("\n-- role changes propagate atomically")
        sysf.assign_column("details", "code")
        sysf.assign_column("notes", "code")
        touched = sysf.set_role("code", "Consolas", grid)
        check("both columns of the role got the family", sorted(touched) == ["details", "notes"])
        check("live matches palette after role change",
              grid.fams["details"] == grid.fams["notes"] == "Consolas")
        before = grid.sheet.redraws
        sysf.set_role("code", "Cascadia Mono", grid)
        check("one redraw per role change", grid.sheet.redraws == before + 1)
        check("live follows the role, not the clicked column",
              grid.fams["details"] == grid.fams["notes"] == "Cascadia Mono")

        print("\n-- save / reload round trip")
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        sysf.save()
        reloaded = FontSystem.load(catalog_path, palette_path)
        grid2 = _MockGrid()
        status = reloaded.apply(grid2)
        check("reloaded palette reproduces the live grid",
              grid2.fams["details"] == grid2.fams["notes"] == "Cascadia Mono")
        check("apply reports engine status per column",
              status["details"]["status"] == "active", status.get("details"))

        # A full palette application must not leave stale family overrides on a
        # column that is no longer mapped by the palette.
        grid2.fams["model"] = "Consolas"
        reloaded.apply(grid2)
        check("apply clears stale unassigned overrides", grid2.fams.get("model") is None)

        print("\n-- fallback stays visible, palette stays intact")
        sysf.assign_column("model", "meta")
        sysf.set_role("meta", "Nunito Black", grid)
        rep = grid.column_font_effective("model")
        check("tall face reports fallback", rep["status"] == "fallback", rep)
        check("palette still records what was asked for",
              sysf.family_for("meta") == "Nunito Black")

        sysf.clear_live(grid)
        check("base preview clears the grid", all(v is None for v in grid.fams.values()))
        check("base preview keeps the palette", sysf.family_for("code") == "Cascadia Mono")

        print("\n-- palette contract")
        try:
            sysf.validate_palette({
                "schema": PALETTE_SCHEMA,
                "roles": {"Bad Role": "Consolas"},
                "columns": {"details": "Bad Role"},
            })
            bad_role_rejected = False
        except ValueError:
            bad_role_rejected = True
        check("invalid role names are rejected", bad_role_rejected)

        try:
            sysf.validate_palette({
                "schema": PALETTE_SCHEMA,
                "roles": {"code": "__NO_SUCH_FONT__"},
                "columns": {"details": "code"},
            })
            unknown_family_rejected = False
        except ValueError:
            unknown_family_rejected = True
        check("unknown families are rejected", unknown_family_rejected)

        clean = sysf.validate_palette({
            "schema": PALETTE_SCHEMA,
            "roles": {"code": "Consolas"},
            "columns": {"details": "code"},
        })
        check("valid semantic role palette survives validation",
              clean["roles"] == {"code": "Consolas"}
              and clean["columns"] == {"details": "code"})

        print("\n-- candidate ordering")
        order = sysf.candidates()
        check("band first, density second", order[0] == "Consolas", order)

        print("\n-- builder provenance")
        csv_path = td / "probe.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "family", "mono", "safe_count", "total_sizes", "safe_range_lo",
                "safe_range_hi", "contract_max_overshoot", "unsafe_sizes",
                "style_fail_at", "width_ratio", "cyrillic_plausible",
                "normal_deltas", "contract_overshoots", "verdict",
            ])
            w.writeheader()
            w.writerow({
                "family": "Demo", "mono": 0, "safe_count": 2, "total_sizes": 3,
                "safe_range_lo": 12, "safe_range_hi": 13,
                "contract_max_overshoot": 1, "unsafe_sizes": "11",
                "style_fail_at": "11", "width_ratio": 1.0, "cyrillic_plausible": 1,
                "normal_deltas": "11:+1 12:-2 13:-3",
                "contract_overshoots": "11:+1 12:-1 13:-2", "verdict": "SAFE-RANGE",
            })
        reg_path = td / "registry.json"
        reg_path.write_text(json.dumps({
            "schema": "super-gui.font-family-qualification.v3",
            "base_font": "Segoe UI", "work_size": 13, "policy_window": [11, 13],
            "host": {"tk_patchlevel": "8.6.12"},
        }), encoding="utf-8")
        pick_path = td / "chosen.json"
        pick_path.write_text(json.dumps({
            "band_thresholds": {"C_max_overshoot_px": 2, "D_max_overshoot_px": 4},
            "size_reviewed": 13, "style_reviewed": "normal", "specimen_preset": "code",
            "chosen": [{"family": "Demo", "design_band": "B",
                        "engine_verdict": "SAFE-RANGE"}],
        }), encoding="utf-8")
        built = build_catalog(csv_path, [pick_path], td / "built.json", reg_path)
        built_index = _families_index(built["families"])
        demo = built_index["Demo"]
        check("builder reads canonical registry work_size",
              built["reference_size"] == 13, built.get("reference_size"))
        check("builder keeps normal and contract headroom distinct",
              demo["reference_normal_headroom"] == 3
              and demo["reference_contract_headroom"] == 2, demo)
        check("builder retains raw qualification row",
              demo["qualification_row"]["contract_overshoots"] == "11:+1 12:-1 13:-2")
        check("complete picks are marked complete",
              built["complete_for_designer_picks"] is True)
        check("v2.2 stores families as JSON array",
              isinstance(built["families"], list) and built["schema"].endswith("v2.2"))

        case_catalog = {
            "schema": "super-gui.font-catalog.v2.2",
            "families": [
                {"family": "Source Sans Pro SemiBold", "engine_verdict": "SAFE"},
                {"family": "Source Sans Pro Semibold", "engine_verdict": "SAFE-RANGE"},
            ],
        }
        case_path = td / "case_catalog.json"
        case_path.write_text(json.dumps(case_catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        case_loaded = FontSystem.load(case_path, td / "none_palette.json")
        check("case-distinct family names survive catalogue load",
              set(case_loaded.families) == {
                  "Source Sans Pro SemiBold", "Source Sans Pro Semibold"
              })

    print()
    if fails:
        print(f"TYPOGRAPHY LAYER: {len(fails)} FAILED -> {fails}")
        return 1
    print("TYPOGRAPHY LAYER: all passed")
    return 0


# ---------------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser(description="Super-GUI typography layer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="fold qualification CSV + designer picks into a catalogue")
    b.add_argument("--csv", required=True)
    b.add_argument("--picks", nargs="*", default=[])
    b.add_argument("--registry", default=None)
    b.add_argument("--work-size", type=int, default=None)
    b.add_argument("--out", default="font-catalog/font_catalog.json")
    b.add_argument("--allow-missing-evidence", action="store_true",
                   help="write an incomplete catalogue and still return success")
    sub.add_parser("selftest", help="check the layer against a mock grid")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()
    picks: list[Path] = []
    for pat in args.picks:
        picks.extend(Path(p) for p in glob.glob(pat))
    doc = build_catalog(Path(args.csv), picks, Path(args.out),
                        Path(args.registry) if args.registry else None, args.work_size)
    if doc.get("coverage_gap") and not args.allow_missing_evidence:
        print("ERROR: catalogue is incomplete for designer picks; "
              "use the full qualification CSV or --allow-missing-evidence")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
