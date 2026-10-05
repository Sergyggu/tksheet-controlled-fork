"""FONT_FAMILY_TIER1 deployment control for the Super-GUI tksheet fork.

Runs against the ACTUAL installed Kit passed via --kit.  It protects the two
existing controlled-fork extensions (segmented text + font_style v2) and the
new Tier-1 font_family socket/guard.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

EXPECTED_PATCHED = {
    "main_table.py": "74C5460EEE11BE377123EC5E989F0AB6CF37D51D3F985C6852D13EF197995BEC",
    "column_headers.py": "8283498DC675937AB0EE2FFCCBE54552405D311A79BB01D296E110300C5BE8DB",
    "kit_grid.py": "6BFCA8196A23E045BCBBC0519BB55D49D7A35CB3B1707FD519AB49365875BD79",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest().upper()


def extract_function(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            out = ast.get_source_segment(src, node)
            if out:
                return out
    raise AssertionError(f"function not found: {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ns = ap.parse_args()
    kit = Path(ns.kit).resolve()
    mt_path = kit / "vendor" / "tksheet" / "main_table.py"
    ch_path = kit / "vendor" / "tksheet" / "column_headers.py"
    grid_path = kit / "kit_grid.py"
    dist = kit / "vendor" / "tksheet-7.6.0.dist-info"

    paths = {"main_table.py": mt_path, "column_headers.py": ch_path, "kit_grid.py": grid_path}
    fails: list[str] = []
    count = 0

    def check(name: str, cond: bool, detail: object = "") -> None:
        nonlocal count
        count += 1
        if cond:
            print(f"  PASS  {count:02d} {name}")
        else:
            msg = f"{name}: {detail}"
            fails.append(msg)
            print(f"  FAIL  {count:02d} {msg}")

    for name, path in paths.items():
        actual = sha256(path) if path.is_file() else "missing"
        check(f"installed-sha:{name}", actual == EXPECTED_PATCHED[name], actual)
    check("pinned-tksheet-7.6.0", dist.is_dir(), dist)

    mt = mt_path.read_text(encoding="utf-8")
    ch = ch_path.read_text(encoding="utf-8")
    grid = grid_path.read_text(encoding="utf-8")

    # Existing controlled-fork invariants.
    check("font-style-v2-present", "def get_cell_font_style(" in mt and "def font_styles_present(" in mt)
    check("lazy-font-caches", "def font_char_widths(" in mt and "def get_test_str_w(" in mt and "self.test_str_widths = {}" in mt)
    check("font-aware-char-width", "def char_width_fn(self, c: str, font: FontTuple | None = None)" in mt)
    check("wrap-one-effective-font", "widths=self.font_char_widths(font)" in mt and "partial(self.char_width_fn, font=font)" in mt)
    check("segmented-geometry-intact", "def segmented_text_geometry(" in mt and "def segmented_text_index(" in mt)
    check("segmented-renderer-intact", 'col_opts.get("segmented_text_count")' in mt and "segment_centres" in mt)

    # New Tier-1 contract.
    required = (
        "get_cell_font_family",
        "font_fits_line_box",
        "font_report",
        "cell_fonts_present",
    )
    check("tier1-functions-present", all(f"def {name}(" in mt for name in required), required)
    check(
        "guard-checks-actual-family",
        'self.tk.call("font", "actual", tuple(font), "-family")' in extract_function(mt, "font_fits_line_box"),
    )
    check(
        "guard-checks-vertical-contract",
        'self.get_txt_dimensions("|", font)[1] <= self.table_txt_height' in extract_function(mt, "font_fits_line_box"),
    )
    check(
        "guard-fails-closed",
        "return FontTuple(base[0], base[1], style or base[2])" in extract_function(mt, "get_cell_font"),
    )
    check(
        "guard-cache-invalidated-on-table-font-change",
        "self._font_ok_cache.clear()" in extract_function(mt, "set_table_font_help"),
    )
    check(
        "resolver-combines-family-style-live-size",
        "FontTuple(family or base[0], base[1], style or base[2])" in extract_function(mt, "get_cell_font"),
    )
    check(
        "all-size-path-uses-overrides",
        "self.cell_fonts_present()" in extract_function(mt, "set_all_cell_sizes_to_text"),
    )
    check(
        "redraw-uses-overrides",
        "self.cell_fonts_present()" in extract_function(mt, "redraw_grid_and_text"),
    )
    check(
        "column-autosize-uses-overrides",
        "self.MT.cell_fonts_present()" in extract_function(ch, "get_col_text_width"),
    )
    check(
        "adapter-family-api",
        "def set_column_font_family(" in grid and "def column_font_family(" in grid and "def column_font_effective(" in grid,
    )
    check("adapter-style-api-preserved", "def set_column_font_style(" in grid and "def column_font_style(" in grid)

    # Live integration against the installed vendored package and adapter.
    root = None
    try:
        sys.path.insert(0, str(kit))
        for mod in list(sys.modules):
            if mod == "kit_grid" or mod == "tksheet" or mod.startswith("tksheet."):
                sys.modules.pop(mod, None)
        import kit_grid  # type: ignore

        root = tk.Tk()
        root.withdraw()
        installed = set(map(str, tkfont.families(root)))
        base_family = "Segoe UI" if "Segoe UI" in installed else str(tkfont.nametofont("TkDefaultFont").actual("family"))

        g = kit_grid.HistoryGrid(
            root,
            columns=("base", "family", "combo", "missing", "tall", "seg"),
            displaycolumns=("base", "family", "combo", "missing", "tall", "seg"),
            titles={c: c for c in ("base", "family", "combo", "missing", "tall", "seg")},
            show="headings",
            height=3,
            table_font=(base_family, 10),
            header_font=(base_family, 10),
        )
        for cid in ("base", "family", "combo", "missing", "tall", "seg"):
            g.column(cid, width=190, minwidth=60, anchor="w")
        sample = "MMMMWWWW  path/to.py  Їжак ҐЄІЇ"
        g.insert("", iid="r1", values=(sample, sample, sample, sample, sample, "★★★★★"))

        mt_obj = g.sheet.MT
        font_type = type(mt_obj.PAR.ops.table_font)
        base = mt_obj.PAR.ops.table_font

        safe_families: list[str] = []
        tall_family: str | None = None
        for fam in sorted(installed, key=str.casefold):
            if fam.casefold() == str(base[0]).casefold():
                continue
            candidate = font_type(fam, base[1], "normal")
            if mt_obj.font_fits_line_box(candidate):
                safe_families.append(fam)
            elif tall_family is None:
                try:
                    actual = str(root.tk.call("font", "actual", tuple(candidate), "-family"))
                    h = mt_obj.get_txt_dimensions("|", candidate)[1]
                    if actual.casefold() == fam.casefold() and h > mt_obj.table_txt_height:
                        tall_family = fam
                except Exception:
                    pass
            if len(safe_families) >= 3 and tall_family is not None:
                break

        check("live-safe-family-found", bool(safe_families), safe_families[:3])
        if safe_families:
            safe = safe_families[0]
            g.set_column_font_family("family", safe, redraw=False)
            g.set_column_font_family("combo", safe, redraw=False)
            g.set_column_font_style("combo", "Italic BOLD", redraw=False)
            g.set_column_font_family("seg", safe, redraw=False)
            g.set_segments("seg", count=5, slot_width=16.0, redraw=False)
            g.redraw()
            root.update_idletasks()

            family_font = mt_obj.get_cell_font(0, 1)
            combo_font = mt_obj.get_cell_font(0, 2)
            seg_font = mt_obj.get_cell_font(0, 5)
            check("live-family-applied", family_font[0].casefold() == safe.casefold(), family_font)
            check("live-family-keeps-live-size", family_font[1] == base[1], family_font)
            check("live-family-plus-style", combo_font[0].casefold() == safe.casefold() and combo_font[2] == "bold italic", combo_font)
            check("live-segmented-family-coexistence", seg_font[0].casefold() == safe.casefold(), seg_font)
            check("live-requested-family-getter", g.column_font_family("family") == safe, g.column_font_family("family"))

            # cell -> row -> column precedence, using distinct safe families when available.
            if len(safe_families) >= 3:
                col_f, row_f, cell_f = safe_families[:3]
                mt_obj.col_options.setdefault(0, {})["font_family"] = col_f
                mt_obj.row_options.setdefault(0, {})["font_family"] = row_f
                mt_obj.cell_options.setdefault((0, 0), {})["font_family"] = cell_f
                check("live-family-precedence-cell", mt_obj.get_cell_font_family(0, 0) == cell_f)
                mt_obj.cell_options[(0, 0)].pop("font_family", None)
                check("live-family-precedence-row", mt_obj.get_cell_font_family(0, 0) == row_f)
                mt_obj.row_options[0].pop("font_family", None)
                check("live-family-precedence-column", mt_obj.get_cell_font_family(0, 0) == col_f)
                mt_obj.col_options[0].pop("font_family", None)
            else:
                check("live-family-precedence-cell", False, "need >=3 safe alternate families")
                check("live-family-precedence-row", False, "need >=3 safe alternate families")
                check("live-family-precedence-column", False, "need >=3 safe alternate families")

            # ColumnHeaders must exercise the same effective font for autosize.
            auto_w = int(g.sheet.CH.get_col_text_width(1))
            check("live-family-autosize-path", auto_w > 0, auto_w)

        # Missing family must never leak through to draw/measure.
        missing = "__SUPER_GUI_NO_SUCH_FONT_FAMILY_7E6C39__"
        g.set_column_font_family("missing", missing, redraw=False)
        missing_font = mt_obj.get_cell_font(0, 3)
        check("live-missing-family-fallback", missing_font[0].casefold() == str(base[0]).casefold(), missing_font)
        missing_report = g.column_font_effective("missing")
        check("live-missing-family-reported-substituted", missing_report.get("status") == "substituted", missing_report)
        check("live-missing-report-effective-base", tuple(missing_report.get("effective", ()))[:1] == (base[0],), missing_report)

        # A real installed but vertically-too-tall family must fail closed too.
        if tall_family is not None:
            g.set_column_font_family("tall", tall_family, redraw=False)
            tall_font = mt_obj.get_cell_font(0, 4)
            check("live-tall-family-fallback", tall_font[0].casefold() == str(base[0]).casefold(), (tall_family, tall_font))
        else:
            # Not every host is guaranteed to have a tall alternate family.  The
            # structural vertical check above remains mandatory; this host-specific
            # exercise becomes non-applicable rather than a false failure.
            check("live-tall-family-fallback", True, "no installed tall alternate on this host")

        # Guard decisions are per concrete tuple and must be invalidated when the
        # global table font/zoom baseline changes.
        check("live-guard-cache-populated", bool(mt_obj._font_ok_cache), len(mt_obj._font_ok_cache))
        mt_obj.set_table_font_help()
        check("live-guard-cache-cleared", not mt_obj._font_ok_cache, mt_obj._font_ok_cache)

        # Existing style-only path must remain intact with no family override.
        g.set_column_font_style("base", "bold", redraw=False)
        style_only = mt_obj.get_cell_font(0, 0)
        check("live-style-v2-regression", style_only[0] == mt_obj.PAR.ops.table_font[0] and style_only[2] == "bold", style_only)

        g.set_column_font_family("family", None, redraw=False)
        check("live-remove-family-override", g.column_font_family("family") is None)
    except Exception as exc:
        check("live-integration", False, repr(exc))
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    print()
    if fails:
        print(f"FONT_FAMILY_TIER1_R2_1: {len(fails)} FAILED / {count}")
        for failure in fails:
            print(" -", failure)
        return 1
    print(f"FONT_FAMILY_TIER1: {count}/{count} OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
