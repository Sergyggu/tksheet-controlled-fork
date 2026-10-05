"""Visual acceptance for the installed Tier-1 font-family socket.

Read-only/throwaway: changes no product files or DB state.
Uses GRID only for direct children of root (no pack/grid collision).
"""
from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--family", default="Consolas")
    ns = ap.parse_args()
    kit = Path(ns.kit).resolve()
    sys.path.insert(0, str(kit))
    import kit_grid  # type: ignore

    root = tk.Tk()
    root.title("tksheet Tier-1 font_family acceptance R2")
    root.geometry("1320x440")
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(1, weight=1)

    top = tk.Frame(root, padx=10, pady=8)
    top.grid(row=0, column=0, sticky="ew")
    status = tk.Label(top, anchor="w", justify="left")
    status.pack(fill="x")

    cols = ("base", "family", "combo", "seg")
    titles = {
        "base": "BASE",
        "family": f"REQUESTED: {ns.family}",
        "combo": f"{ns.family} + BOLD ITALIC",
        "seg": f"SEGMENTED + {ns.family}",
    }
    g = kit_grid.HistoryGrid(
        root,
        columns=cols,
        displaycolumns=cols,
        titles=titles,
        show="headings",
        height=8,
        table_font=("Segoe UI", 10),
        header_font=("Segoe UI Semibold", 10),
        row_height=30,
    )
    for cid in cols:
        g.column(cid, width=300 if cid != "seg" else 180, minwidth=100, anchor="w")

    rows = [
        "path/to/file.py:1193  {'k':[1,2,3]}  --flag=42",
        "Український: Ґґ Єє Іі Її  Їжак приїхав",
        "Latin: Il1|0O  ABC xyz  0123456789",
        "Wrap probe: alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
    ]
    for n, text in enumerate(rows, 1):
        g.insert("", iid=f"r{n}", values=(text, text, text, "★★★★★" if n % 2 else "★★★☆☆"))

    g.set_column_font_family("family", ns.family, redraw=False)
    g.set_column_font_family("combo", ns.family, redraw=False)
    g.set_column_font_style("combo", "bold italic", redraw=False)
    g.set_column_font_family("seg", ns.family, redraw=False)
    g.set_segments("seg", count=5, slot_width=16.0, redraw=False)
    g.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
    g.redraw()
    root.update_idletasks()

    rep = g.column_font_effective("family")
    combo_rep = g.column_font_effective("combo")
    effective = g.sheet.MT.get_cell_font(0, 1)
    combo = g.sheet.MT.get_cell_font(0, 2)
    status.config(
        text=(
            f"requested={g.column_font_family('family')!r}    effective={tuple(effective)!r}\n"
            f"status={rep.get('status')}    resolved_as={rep.get('resolved_as')!r}    "
            f"pipe={rep.get('pipe_height')}px / line_box={rep.get('line_box')}px    "
            f"overshoot={rep.get('overshoot')}\n"
            f"combo={tuple(combo)!r}    combo_status={combo_rep.get('status')}\n"
            f"reason: {rep.get('reason')}\n"
            "Expected: when ACTIVE, FAMILY differs visibly from BASE; FALLBACK/SUBSTITUTED must use base family. "
            "Segmented slot geometry must remain fixed."
        )
    )
    print(status.cget("text"))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
