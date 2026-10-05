"""History grid: the Treeview surface the tab already speaks, backed by tksheet.

Why an adapter and not a rewrite
--------------------------------
`kit_panel_tab_history.py` touches its table at 36 call sites through about
twenty `ttk.Treeview` verbs.  Re-expressing all of them inline would turn a
widget swap into a rewrite of a 2400-line file that currently carries three
green permanent controls.  This module speaks those verbs instead, so the tab
changes in a handful of places and the controls keep passing unread.

It also keeps the escape route open: `BACKEND = "treeview"` returns the tab to
the stock widget without touching the tab at all.  The Treeview path is not
deleted until the ported panel has been live-tested.

What it adds beyond Treeview
----------------------------
`set_zones()` / `repaint()` - the semantic colour zones (provenance, Group,
Comment, quality, issue, time).  Zones are a property of the COLUMN MODEL, not
of the widget: the caller hands over a mapping and the grid rebuilds every
surface from it.  A criterion column inserted later needs no special handling,
it just arrives with a zone.

Painting order matters and is fixed here: column zones first, then per-row tag
colours, then hover.  Cell-level paint wins over column-level in tksheet, so
row states are drawn cell by cell CARRYING each cell's own zone background -
a row-level highlight would drop the zone stripe under the cursor.
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Iterable, Sequence

BACKEND = "tksheet"          # "treeview" restores the stock widget

# The Kit carries its own copy of tksheet and never touches InvokeAI's
# site-packages.  The vendor directory goes on the path here, once, so that
# nothing else in the Kit has to know the library is vendored at all.
VENDOR = Path(__file__).resolve().parent / "vendor"
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

_TREE_COLUMN = "#0"
_REASON = ""


def _excel_column(index: int) -> str:
    """0-based data-column index -> tksheet span key (A, B, ..., AA...)."""
    n = int(index) + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def available() -> bool:
    """True when the ported grid can be built.  Never raises, always explains.

    A silent downgrade to ttk.Treeview is how a port appears to have been
    installed while none of it is running, so the reason for every refusal is
    recorded and `backend_report()` prints it.
    """
    global _REASON
    if BACKEND != "tksheet":
        _REASON = f"BACKEND is set to {BACKEND!r} in kit_grid.py"
        return False
    try:
        import tksheet
    except ImportError as exc:
        _REASON = (
            f"tksheet is not importable ({exc}). Expected a vendored copy at "
            f"{VENDOR}. Install it with:\n"
            f'    pip install --target="{VENDOR}" tksheet==7.6.0'
        )
        return False
    version = getattr(tksheet, "__version__", "?")
    if not hasattr(tksheet.Sheet, "highlight_columns"):
        _REASON = f"tksheet {version} is too old: no highlight_columns()"
        return False
    _REASON = f"tksheet {version} from {Path(tksheet.__file__).parent}"
    return True


def backend_reason() -> str:
    return _REASON


def backend_report() -> str:
    ok = available()
    return (f"kit_grid backend : {'tksheet (ported grid)' if ok else 'ttk.Treeview (fallback)'}\n"
            f"reason           : {_REASON}\n"
            f"module           : {Path(__file__).resolve()}")


class HistoryGrid:
    """The subset of ttk.Treeview the History tab uses, plus colour zones.

    Deliberately NOT a general-purpose wrapper: every method here exists
    because the tab calls it.  Adding a verb is cheap; guessing at ones nobody
    calls is how an adapter turns into a second widget library.
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        columns: Sequence[str],
        displaycolumns: Sequence[str],
        titles: dict[str, str],
        show: str = "headings",
        height: int = 12,
        table_font: tuple = ("Segoe UI", 10),
        header_font: tuple = ("Segoe UI Semibold", 10),
        row_height: int = 22,
        tree_width: int = 340,
    ) -> None:
        from tksheet import Sheet

        self._ids = list(columns)
        self._index = {cid: i for i, cid in enumerate(self._ids)}
        self._visible = list(displaycolumns)
        self._titles = dict(titles)
        self._commands: dict[str, Callable[[], None] | str] = {}
        self._menus: dict[str, tuple[Callable[[Any], None], bool, str]] = {}
        self._anchor: dict[str, str] = {}
        self._widths: dict[str, int] = {}
        self._minwidths: dict[str, int] = {}
        self._stretch: str | None = None
        self._tree_width = tree_width
        self._tree_min_width = 1
        self._tags: dict[str, dict[str, str]] = {}
        self._row_tags: dict[str, tuple[str, ...]] = {}
        self._zone_of: dict[str, str] = {}
        self._palette: dict[str, tuple[str, str]] = {}
        self._header_palette: dict[str, tuple[str, str]] = {}
        self._hover_row: int | None = None
        self._hover_fg: str | None = None
        self._show = show

        self.sheet = Sheet(
            parent,
            treeview=True,
            headers=[self._titles.get(cid, cid) for cid in self._ids],
            show_row_index=(show != "headings"),
            default_row_index_width=tree_width,
            font=(*table_font, "normal"),
            header_font=(*header_font, "normal"),
            default_row_height=row_height,
        )
        self.sheet.enable_bindings(
            "single_select", "column_width_resize", "double_click_column_resize",
            "arrowkeys", "copy",
        )
        # An empty sheet has no column positions yet, so column_width() would
        # raise before the first row arrives.  Treeview lets callers size its
        # columns at build time, and the tab does exactly that, so the
        # positions are created up front from the header row.
        self.sheet.headers([self._titles.get(cid, cid) for cid in self._ids],
                           reset_col_positions=True, redraw=False)
        # ttk.Treeview invokes heading(command=...) itself.  tksheet has no
        # such notion, so the adapter reproduces it: a heading command is only
        # useful if something actually calls it.
        self._header_dragged = False
        self.sheet.CH.bind("<B1-Motion>", self._heading_drag, add="+")
        self.sheet.CH.bind("<ButtonRelease-1>", self._heading_release, add="+")
        # tksheet exposes one callback for both drag-resize and double-click
        # auto-resize.  Use it as a per-column min-width gate; the library's own
        # min_column_width is global and would make narrow columns such as Pick
        # unusably wide.
        self._previous_column_width_resize_func = self.sheet.CH.column_width_resize_func
        self.sheet.CH.column_width_resize_func = self._column_width_resized
        self._apply_displaycolumns()

    # -- placement / scrolling --------------------------------------------
    def grid(self, **kwargs) -> None:
        self.sheet.grid(**kwargs)

    def yview(self, *args) -> Any:
        return self.sheet.set_yview(*args) if args else None

    def xview(self, *args) -> Any:
        return self.sheet.set_xview(*args) if args else None

    def xview_scroll(self, number: int, what: str) -> None:
        try:
            self.sheet.MT.xview_scroll(number, what)
            self.sheet.redraw()
        except Exception:
            pass

    def configure(self, **kwargs) -> None:
        if "displaycolumns" in kwargs:
            self._visible = list(kwargs["displaycolumns"])
            self._apply_displaycolumns()
        if "show" in kwargs:
            self._show = kwargs["show"]
            # set_options(show_row_index=...) records the option but does not
            # raise or lower the canvas; show()/hide() are what actually move
            # it, and without them a grouped view has nowhere to print the
            # group name.
            if self._show == "headings":
                self.sheet.hide("row_index")
            else:
                self.sheet.show("row_index")
                self.sheet.set_index_width(self._tree_width, redraw=False)
            self.repaint()

    def cget(self, key: str) -> Any:
        if key == "displaycolumns":
            return tuple(self._visible)
        if key == "show":
            return self._show
        return None

    def _apply_displaycolumns(self) -> None:
        hidden = [c for c in self._ids if c not in self._visible]
        try:
            self.sheet.display_columns(
                columns=[self._index[c] for c in self._ids if c in self._visible],
                all_columns_displayed=not hidden,
                redraw=False,
            )
        except Exception:
            pass
        # Widths are addressed by DISPLAYED position in tksheet, so hiding a
        # column shifts every width to its right.  Treeview hides nothing from
        # its own width bookkeeping, and the tab relies on that, so the stored
        # widths are simply re-applied against the new layout.
        for column_id, width in self._widths.items():
            self._apply_width(column_id, width)
        self.repaint()

    def current_widths(self) -> dict[str, int]:
        """Widths as they stand now, including any the user dragged."""
        shown = [c for c in self._ids if c in self._visible]
        out = dict(self._widths)
        try:
            live = self.sheet.get_column_widths()
        except Exception:
            return out
        for i, column_id in enumerate(shown):
            if i < len(live):
                out[column_id] = int(live[i])
        return out

    def set_widths(self, widths: dict[str, int]) -> None:
        for column_id, width in widths.items():
            if column_id in self._index:
                wanted = self._clamped_width(column_id, int(width))
                self._widths[column_id] = wanted
                self._apply_width(column_id, wanted)
        self.sheet.redraw()

    def _clamped_width(self, column_id: str, width: int) -> int:
        return max(int(width), int(self._minwidths.get(column_id, 1)))

    def _column_width_resized(self, event_data) -> None:
        """Enforce Treeview-style per-column minwidth after a user resize.

        tksheet 7.6.0 only has one global min_column_width.  History needs tiny
        utility columns and 108px criterion controls at the same time, so the
        adapter clamps only the displayed column that tksheet reports as resized.
        This callback runs for drag resize and double-click auto-resize.
        """
        changed = False
        shown = [c for c in self._ids if c in self._visible]
        try:
            resized = dict(event_data.get("resized_columns", {}))
        except Exception:
            resized = {}
        for displayed, sizes in resized.items():
            try:
                i = int(displayed)
                column_id = shown[i]
                live = int(sizes.get("new_size"))
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            floor = int(self._minwidths.get(column_id, 1))
            wanted = max(live, floor)
            self._widths[column_id] = wanted
            if wanted != live:
                try:
                    self.sheet.column_width(i, wanted, redraw=False)
                    changed = True
                except Exception:
                    pass
        if changed:
            try:
                self.sheet.redraw()
            except Exception:
                pass
        previous = self._previous_column_width_resize_func
        if callable(previous):
            previous(event_data)

    def _displayed_index(self, column_id: str) -> int | None:
        if column_id not in self._visible:
            return None
        shown = [c for c in self._ids if c in self._visible]
        try:
            return shown.index(column_id)
        except ValueError:
            return None

    def _apply_width(self, column_id: str, width: int) -> None:
        i = self._displayed_index(column_id)
        if i is None:
            return                          # hidden: remembered, not drawn
        wanted = self._clamped_width(column_id, width)
        try:
            self.sheet.column_width(i, wanted, redraw=False)
        except Exception:
            pass

    def _apply_anchor(self, column_id: str, anchor: str) -> None:
        """Apply the Treeview-style body anchor to tksheet, not just remember it.

        Earlier ports stored ``anchor`` but never told tksheet about it.  That
        left criterion cells physically left-aligned while the column model
        said ``center``.  Any hit-test derived from the model therefore drifted
        away from what the user actually saw.  tksheet 7.x supports per-column
        alignment through public spans; keep render geometry and click geometry
        under one contract.
        """
        mapped = {"w": "left", "center": "center", "e": "right"}.get(anchor, "left")
        try:
            self.sheet[_excel_column(self._index[column_id])].align(mapped, redraw=False)
        except Exception:
            # Pinned 7.6.0 supports this.  Keep the fallback non-fatal so the
            # Treeview escape hatch remains usable if a vendor copy is missing.
            pass

    # -- columns and headings ---------------------------------------------
    # A heading can carry one affordance besides its own name.  It lives in a
    # fixed strip at the right edge of the header cell, so the rule "clicking a
    # heading sorts it" survives for every column without exceptions.
    GLYPH_PAD = 7                    # forgiving, but still on the mark

    def set_header_menu(self, column_id: str, callback: Callable[[Any], None] | None,
                        *, glyph_only: bool = True, glyph: str = "") -> None:
        """Give a heading a second action.

        glyph_only=True puts it in the right-edge strip, leaving the rest of
        the heading to sort.  glyph_only=False gives the WHOLE heading to the
        callback - which is what a collapsed column needs, because there is no
        room to draw a glyph and a hotspot nobody can see is not an affordance.
        """
        if callback is None:
            self._menus.pop(column_id, None)
        else:
            self._menus[column_id] = (callback, glyph_only, glyph)

    def _in_glyph_zone(self, event, column_id: str, glyph: str) -> bool:
        """Is the pointer on the DRAWN glyph.

        The first version measured from the column's right edge, which is only
        the same place as the glyph when the heading happens to fill its
        column.  Widen the column and the two drift apart, so the user has to
        click empty space far from the mark they can see - the same mistake as
        dividing a cell into five bands when the stars occupy two thirds of it.
        The heading text is measured instead, in the header's own font, and the
        hitbox is put around the glyph itself.
        """
        bounds = self.column_bounds(event, column_id)
        if bounds is None or not glyph:
            return False
        left, right = bounds
        title = self._titles.get(column_id, column_id)
        if not title.endswith(glyph):
            return False
        font = self.sheet.ops["header_font"]
        try:
            full = self.sheet.MT.get_txt_w(title, font)
            mark = self.sheet.MT.get_txt_w(glyph, font)
        except Exception:
            return False
        align = str(getattr(self.sheet.CH, "align", "c") or "c")
        if align.startswith("w"):
            text_right = left + 3 + full
        elif align.startswith("e"):
            text_right = right - 3
        else:
            text_right = left + (right - left + full) / 2
        text_right = min(text_right, right - 2)
        x = self.sheet.CH.canvasx(event.x)
        return (text_right - mark - self.GLYPH_PAD) <= x <= (text_right + self.GLYPH_PAD)

    def _heading_drag(self, _event) -> None:
        self._header_dragged = True

    def _heading_release(self, event) -> None:
        """Fire the heading command on a click, never on a width drag.

        Release, not press: tksheet's own header handler sometimes ends the
        binding chain on ButtonPress, so a press-plus-release pair silently
        misses clicks.  Dragging is told apart by whether any motion arrived
        while the button was down, which needs no press event at all.
        """
        dragged, self._header_dragged = self._header_dragged, False
        if dragged:
            return
        column_id = self.column_at(event, header=True)
        if not column_id:
            return
        entry = self._menus.get(column_id)
        if entry is not None:
            callback, glyph_only, glyph = entry
            if not glyph_only or self._in_glyph_zone(event, column_id, glyph):
                callback(event)
                return
        command = self._commands.get(column_id)
        if callable(command):
            command()


    def heading(self, column: str, *, text: str | None = None,
                command: Callable[[], None] | str = "") -> None:
        if column == _TREE_COLUMN:
            return                              # the tree column has no heading
        if text is not None:
            self._titles[column] = text
            self.sheet.set_header_data(text, c=self._index[column], redraw=False)
        self._commands[column] = command

    def redraw(self) -> None:
        """One explicit paint after a batch of Treeview-like heading updates."""
        try:
            self.sheet.redraw()
        except Exception:
            pass

    def column(self, column: str, *, width: int | None = None,
               minwidth: int | None = None, stretch: bool | None = None,
               anchor: str | None = None) -> None:
        if column == _TREE_COLUMN:
            if minwidth is not None:
                self._tree_min_width = max(1, int(minwidth))
            if width is not None:
                self._tree_width = max(int(width), self._tree_min_width)
                self.sheet.set_index_width(self._tree_width, redraw=False)
            return
        if minwidth is not None:
            self._minwidths[column] = max(1, int(minwidth))
        if anchor:
            self._anchor[column] = anchor
            self._apply_anchor(column, anchor)
        if stretch:
            self._stretch = column
        if width is not None:
            wanted = self._clamped_width(column, int(width))
            self._widths[column] = wanted
            self._apply_width(column, wanted)

    def set_base_colours(self, **colours: str) -> None:
        """Paint the widget's own surfaces from the Panel's semantic tokens.

        Without this the area to the right of the last column keeps tksheet's
        own default, which in the dark theme is a white slab beside a dark
        table.  Zones colour the columns; this colours everything that is not
        a column.
        """
        try:
            self.sheet.set_options(redraw=False, **colours)
        except Exception:
            pass
        self.repaint()

    def stretch_last_column(self) -> None:
        """Give the stretch column whatever width is left over.

        ttk.Treeview does this itself through stretch=True; tksheet keeps the
        widths it was given and leaves the remainder blank, so the tab's
        intent has to be re-expressed here.
        """
        if self._stretch is None:
            return
        i = self._displayed_index(self._stretch)
        if i is None:
            return
        try:
            available = self.sheet.winfo_width() - self.sheet.MT.winfo_x() - 24
            widths = self.sheet.get_column_widths()
            others = sum(w for n, w in enumerate(widths) if n != i)
            target = max(self._widths.get(self._stretch, 200), available - others)
            target = self._clamped_width(self._stretch, target)
            if abs(widths[i] - target) > 2:
                self.sheet.column_width(i, target, redraw=True)
        except Exception:
            pass

    def on_resize(self, func: Callable[[Any], None]) -> None:
        self.sheet.bind("<Configure>", func, add="+")

    # -- rows ---------------------------------------------------------------
    # A child row carries no text of its own, but an EMPTY text makes tksheet
    # fall back to printing the display row number in the tree column - a
    # counter that means nothing and looks like an id.  A single space is text,
    # so the fallback never fires and the column stays blank, as in Treeview.
    _BLANK = " "

    def insert(self, parent: str, index: str = "end", *, iid: str,
               text: str = "", values: Iterable[Any] = (),
               tags: tuple[str, ...] = (), open: bool = False) -> str:
        self.sheet.insert(parent=parent, iid=iid, text=text or self._BLANK,
                          values=list(values), undo=False)
        if tags:
            self._row_tags[iid] = tuple(tags)
        if open:
            self.sheet.tree_open(iid, redraw=False)
        return iid

    def item(self, iid: str, *, values: Iterable[Any] | None = None,
             text: str | None = None) -> None:
        if values is not None or text is not None:
            self.sheet.item(iid, values=None if values is None else list(values),
                            text=None if text is None else (text or self._BLANK),
                            undo=False, redraw=False)
        self.sheet.redraw()

    def delete(self, *iids: str) -> None:
        for iid in iids:
            self._row_tags.pop(iid, None)
            try:
                self.sheet.del_items(iid, undo=False)
            except Exception:
                pass

    def get_children(self, item: str = "") -> tuple[str, ...]:
        try:
            return tuple(self.sheet.get_children(item))
        except Exception:
            return ()

    def exists(self, iid: str) -> bool:
        try:
            return self.sheet.exists(iid)
        except Exception:
            return False

    def selection(self) -> tuple[str, ...]:
        try:
            rows = self.sheet.get_selected_rows(get_cells_as_rows=True)
        except Exception:
            return ()
        out = []
        for row in sorted(rows):
            iid = self.sheet.rowitem(row)
            if iid:
                out.append(str(iid))
        return tuple(out)

    def selection_set(self, iid: str) -> None:
        try:
            self.sheet.selection_set(iid)
        except Exception:
            pass

    def see(self, iid: str) -> None:
        try:
            self.sheet.see(iid)
        except Exception:
            pass

    # -- appearance ---------------------------------------------------------
    def tag_configure(self, tag: str, *, foreground: str | None = None,
                      background: str | None = None) -> None:
        entry = self._tags.setdefault(tag, {})
        if foreground:
            entry["fg"] = foreground
        if background:
            entry["bg"] = background

    def set_zones(
        self,
        zone_of: dict[str, str],
        palette: dict[str, tuple[str, str]],
        *,
        header_palette: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        """Paint semantic body and header surfaces independently.

        Earlier the header inherited the exact cell background.  That makes a
        dark table look flat and a light table almost white.  A separate header
        palette gives the heading row its own luminance without changing the
        selected-header colour supplied by tksheet.
        """
        self._zone_of = dict(zone_of)
        self._palette = dict(palette)
        self._header_palette = dict(header_palette or palette)
        self.repaint()

    def set_hover_foreground(self, colour: str | None) -> None:
        self._hover_fg = colour

    def _zone_bg(self, column_id: str) -> str | None:
        entry = self._palette.get(self._zone_of.get(column_id, ""))
        return entry[0] if entry else None

    def repaint(self) -> None:
        """Rebuild every semantic surface from the column model."""
        self.sheet.dehighlight_all(redraw=False)
        by_zone: dict[str, list[int]] = {}
        for cid in self._ids:
            zone = self._zone_of.get(cid)
            if zone and zone in self._palette:
                by_zone.setdefault(zone, []).append(self._index[cid])
        for zone, indexes in by_zone.items():
            bg, fg = self._palette[zone]
            self.sheet.highlight_columns(
                columns=indexes, bg=bg, fg=fg,
                highlight_header=False, redraw=False,
            )
            hbg, hfg = self._header_palette.get(zone, (bg, fg))
            for index in indexes:
                self.sheet.highlight_cells(
                    column=index, canvas="header", bg=hbg, fg=hfg, redraw=False,
                )
        self._paint_row_tags()
        self.sheet.redraw()

    def _paint_row_tags(self) -> None:
        """Tagged rows are painted cell by cell so the zone stripe survives."""
        if not self._row_tags:
            return
        for iid, tags in self._row_tags.items():
            style: dict[str, str] = {}
            for tag in tags:
                style.update(self._tags.get(tag, {}))
            if not style:
                continue
            row = self._displayed_row(iid)
            if row is None:
                continue
            for cid in self._ids:
                self.sheet.highlight_cells(
                    row=row, column=self._index[cid],
                    bg=style.get("bg") or self._zone_bg(cid) or False,
                    fg=style.get("fg") or False,
                    redraw=False,
                )

    def _displayed_row(self, iid: str) -> int | None:
        try:
            return self.sheet.itemrow(iid)
        except Exception:
            return None

    def hover(self, row: int | None) -> None:
        """Mark one displayed row without flattening its zones.

        Mouse hit-testing yields a displayed row, while cell highlight options
        are keyed by the underlying data row.  Convert through the stable iid so
        collapse/filtering cannot make the hover land on a different inference.
        """
        data_row: int | None = None
        if row is not None:
            try:
                iid = self.sheet.rowitem(row)
                data_row = self.sheet.itemrow(iid) if iid else None
            except Exception:
                data_row = None
        if data_row == self._hover_row:
            return
        if self._hover_row is not None:
            for i in range(len(self._ids)):
                self.sheet.dehighlight_cells(row=self._hover_row, column=i, redraw=False)
            self._hover_row = None
            self._paint_row_tags()
        if data_row is not None and self._hover_fg:
            for cid in self._ids:
                self.sheet.highlight_cells(
                    row=data_row, column=self._index[cid],
                    bg=self._zone_bg(cid) or False, fg=self._hover_fg, redraw=False,
                )
            self._hover_row = data_row
        self.sheet.redraw()

    # -- events -------------------------------------------------------------
    def on_select(self, func: Callable[[], None]) -> None:
        self.sheet.extra_bindings("cell_select", lambda _e: func())
        self.sheet.extra_bindings("row_select", lambda _e: func())

    def on_body_click(self, func: Callable[[Any], None]) -> None:
        self.sheet.MT.bind("<ButtonPress-1>", func, add="+")

    def on_body_double_click(self, func: Callable[[Any], None]) -> None:
        self.sheet.MT.bind("<Double-Button-1>", func, add="+")

    def on_body_right_click(self, func: Callable[[Any], None]) -> None:
        self.sheet.MT.bind("<ButtonPress-3>", func, add="+")

    def on_header_click(self, func: Callable[[Any], None]) -> None:
        self.sheet.CH.bind("<ButtonPress-1>", func, add="+")

    def on_header_right_click(self, func: Callable[[Any], None]) -> None:
        self.sheet.CH.bind("<ButtonPress-3>", func, add="+")

    def on_motion(self, func: Callable[[Any], None]) -> None:
        self.sheet.MT.bind("<Motion>", func, add="+")

    def on_leave(self, func: Callable[[Any], None]) -> None:
        self.sheet.MT.bind("<Leave>", func, add="+")

    def bind(self, sequence: str, func: Callable, add: str | None = None) -> None:
        self.sheet.MT.bind(sequence, func, add=add or "+")

    # -- hit testing --------------------------------------------------------
    def column_at(self, event, *, header: bool = False) -> str | None:
        """Column id under the pointer, or None.

        ``identify_column`` speaks DISPLAYED-column coordinates.  Once optional
        columns or unused criterion slots are hidden, that number is no longer
        the index in ``self._ids``.  Keep the conversion in the adapter so the
        History tab never has to know which coordinate space tksheet returned.
        Column drag/reorder is deliberately disabled, therefore ``_visible`` is
        the authoritative displayed order.
        """
        try:
            displayed = self.sheet.identify_column(event, exclude_header=not header)
        except Exception:
            return None
        shown = [cid for cid in self._ids if cid in self._visible]
        if displayed is None or not (0 <= displayed < len(shown)):
            return None
        return shown[displayed]

    def row_at(self, event) -> int | None:
        try:
            return self.sheet.identify_row(event, exclude_index=True)
        except Exception:
            return None

    def iid_at(self, event) -> str | None:
        row = self.row_at(event)
        if row is None:
            return None
        iid = self.sheet.rowitem(row)
        return str(iid) if iid else None

    def column_bounds(self, event=None, column_id: str = "") -> tuple[float, float] | None:
        """Live left/right canvas x of a DISPLAYED column.

        Canvas positions contain only visible columns.  Using the underlying
        data index here works accidentally until the first hidden column lies
        to the left of the target; then hitboxes drift or disappear.
        """
        i = self._displayed_index(column_id)
        if i is None:
            return None
        try:
            positions = self.sheet.get_column_widths(canvas_positions=True)
            if not (0 <= i + 1 < len(positions)):
                return None
            return positions[i], positions[i + 1]
        except Exception:
            return None

    def canvas_x(self, event) -> float:
        return self.sheet.MT.canvasx(event.x)

    def text_width(self, text: str) -> int:
        """Measured on tksheet's own canvas, so the font matches exactly."""
        try:
            return int(self.sheet.MT.get_txt_w(text))
        except Exception:
            return 0

    def set_segments(
        self, column_id: str, *, count: int, slot_width: float, redraw: bool = False
    ) -> None:
        """Render one logical column as fixed-width one-character text slots.

        The vendored MainTable owns the geometry.  History only opts a column
        into that generic renderer; no rating/star/triangle knowledge enters
        tksheet itself.
        """
        if column_id not in self._index:
            return
        datacn = self._index[column_id]
        opts = self.sheet.MT.col_options.setdefault(datacn, {})
        if int(count) <= 0 or float(slot_width) <= 0:
            opts.pop("segmented_text_count", None)
            opts.pop("segmented_text_width", None)
        else:
            opts["segmented_text_count"] = int(count)
            opts["segmented_text_width"] = float(slot_width)
        if redraw:
            self.sheet.redraw()

    # kit-font-style: adapter hook for the vendored per-cell font engine.
    _FONT_STYLES = ("normal", "bold", "italic", "bold italic")

    def set_column_font_style(
        self, column_id: str, style: str | None, *, redraw: bool = False
    ) -> None:
        """Style-only font override for one logical column.

        Family and size always come from the live global table font, so zoom
        and theme rebuilds keep working and a styled column can never freeze an
        old face.  Written straight into MT.col_options, the same controlled
        bypass set_segments already uses; the public Sheet/Span API is a
        separate, purely additive stage.
        """
        if column_id not in self._index:
            return
        datacn = self._index[column_id]
        if style is None:
            opts = self.sheet.MT.col_options.get(datacn)
            if opts is not None:
                opts.pop("font_style", None)
        else:
            # Canonical token order: Tk accepts "italic bold" too, and without
            # this the two spellings would key two identical font caches.
            style = " ".join(sorted(str(style).lower().split()))
            if style not in self._FONT_STYLES:
                raise ValueError(f"font_style must be one of {list(self._FONT_STYLES)}")
            self.sheet.MT.col_options.setdefault(datacn, {})["font_style"] = style
        if redraw:
            self.sheet.redraw()

    def column_font_style(self, column_id: str) -> str | None:
        """Current override for the column, or None."""
        if column_id not in self._index:
            return None
        style = self.sheet.MT.col_options.get(self._index[column_id], {}).get("font_style")
        return style if isinstance(style, str) and style else None

    def set_column_font_family(
        self, column_id: str, family: str | None, *, redraw: bool = False
    ) -> None:
        """Per-column font family override for the table body.

        Only the family is stored.  Size always comes from the live global
        table font, so zoom keeps working; style is a separate, independent
        override.  No name is validated against a list here on purpose - the
        engine accepts any family and the runtime guard in MainTable decides
        whether it may be drawn at the current size.  Written straight into
        MT.col_options, the same controlled bypass set_segments uses.
        """
        if column_id not in self._index:
            return
        datacn = self._index[column_id]
        family = None if family is None else str(family).strip()
        if not family:
            opts = self.sheet.MT.col_options.get(datacn)
            if opts is not None:
                opts.pop("font_family", None)
        else:
            self.sheet.MT.col_options.setdefault(datacn, {})["font_family"] = family
        if redraw:
            self.sheet.redraw()

    def column_font_family(self, column_id: str) -> str | None:
        """The requested family for the column, whether or not it is in use."""
        if column_id not in self._index:
            return None
        fam = self.sheet.MT.col_options.get(self._index[column_id], {}).get("font_family")
        if not isinstance(fam, str):
            return None
        fam = fam.strip()
        return fam or None

    def column_font_effective(self, column_id: str) -> dict:
        """What the column is actually drawn with, and why.

        The guard falls back silently, which reads exactly like a bug to
        whoever is picking fonts: you set Nunito, you see Segoe UI, you assume
        nothing happened.  This distinguishes "not applied" from "does not fit
        at this size".

        Returns status "none" (no override), "active", "fallback" (too tall
        right now), "substituted" (requested family was not resolved and the
        engine therefore fails closed to the base family), or "error".
        """
        family = self.column_font_family(column_id)
        if family is None:
            base = self.sheet.MT.PAR.ops.table_font
            return {"status": "none", "requested": None, "base": tuple(base),
                    "reason": "no family override on this column"}
        style = self.column_font_style(column_id)
        return self.sheet.MT.font_report(family, style)

    def segment_at(self, event, column_id: str) -> int | None:
        """1-based fixed segment under the pointer, or None outside the run.

        Draw and hit share MainTable.segmented_text_geometry/index.  Unicode
        ink, advance, fallback fonts and whole-string bbox measurements are not
        involved in the ordinal decision.
        """
        if column_id not in self._index:
            return None
        datacn = self._index[column_id]
        opts = self.sheet.MT.col_options.get(datacn, {})
        try:
            count = int(opts.get("segmented_text_count", 0))
            slot_width = float(opts.get("segmented_text_width", 0))
        except (TypeError, ValueError):
            return None
        if count <= 0 or slot_width <= 0:
            return None
        bounds = self.column_bounds(event, column_id)
        if bounds is None:
            return None
        index = self.sheet.MT.segmented_text_index(
            bounds[0], bounds[1], self.canvas_x(event), count, slot_width
        )
        return None if index is None else index + 1

    # -- tree ---------------------------------------------------------------
    def collapse_all(self) -> None:
        self.sheet.tree_close()

    def expand_all(self) -> None:
        self.sheet.tree_open()


def make_treeview(parent: tk.Widget, *, columns: Sequence[str],
                  displaycolumns: Sequence[str], show: str, height: int,
                  style: str) -> ttk.Treeview:
    """The stock widget, for the BACKEND == 'treeview' fallback."""
    return ttk.Treeview(
        parent, columns=list(columns), displaycolumns=list(displaycolumns),
        show=show, selectmode="browse", height=height, style=style,
    )


if __name__ == "__main__":
    # python kit_grid.py  - answers "is the port actually running?"
    print(backend_report())
