#!/usr/bin/env python3
"""History tab — the inference journal (H6a).

Flat list first: filters, run details, failure stage, session provenance,
open artifact, explicit sync.  Group-by prompt/model/recipe is H6b.

Three provenance states, not two.  A run made minutes ago, while the Kit
session is still open, has provenance waiting for an authoritative EXITED;
showing it as "not recorded" would read as data loss when nothing was lost.

Ports follow kit_panel_tab_state.StateTab: everything the tab needs arrives
in the constructor, nothing is reached through the panel by attribute.

Normal reading stays read-only through kit_history.  User-directed Remove/Restore
uses the narrow kit_history_mutate service, which opens a write connection only
for the duration of that explicit operation.

The journal shows what happened.  It does not recommend.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Any, Callable

import sqlite3

import kit_panel_theme as theme
import kit_grid
import kit_history
import kit_history_mutate


ALL = "\u2014 all \u2014"
NO_GROUP = "\u2014 none \u2014"
# Runs nobody has annotated are a normal, and today overwhelming, case.
# They need a filter value of their own; ALL would hide the distinction.
NO_LABEL = "\u2014 no label \u2014"

# Grouping choices, label -> logical field on a run row.
GROUPINGS = (
    ("Prompt", "prompt"),
    ("Model", "model_name"),
    ("Kit mode", "recipe"),
    ("Group", "label"),
)

GROUP_PREFIX = "G:"

# Period presets. None means "no bound", so All needs no date arithmetic.
PERIODS = (
    ("All", None),
    ("Today", 0),
    ("Last 7 days", 6),
    ("Last 30 days", 29),
)
CUSTOM = "Custom"

# Theme/language switching rebuilds the Panel and therefore creates a new
# HistoryTab object.  Keep only transient UI choices in memory so a rebuild is
# not interpreted as "reset my period".  Nothing here is persisted to disk.
_UI_STATE_BY_DB: dict[str, dict[str, Any]] = {}


# History UX design knobs ---------------------------------------------------
# USER-TUNING ZONE: typography / geometry for the History tab.
#
# Colour knobs intentionally live in kit_panel_theme.py as HISTORY_* tokens.
# Keeping fonts/geometry here preserves the theme module as a colour system
# while still giving History one obvious place for direct visual tuning.
#
# Change these constants instead of hunting through ttk.Style / tk.Text calls.
HISTORY_TABLE_FONT = ("Segoe UI", 10)
HISTORY_TABLE_ROW_HEIGHT = 22

HISTORY_HEADER_FONT = ("Segoe UI Semibold", 10)

HISTORY_DETAIL_TITLE_FONT = ("Segoe UI Semibold", 9)
HISTORY_DETAIL_FONT = ("Consolas", 9)
HISTORY_DETAIL_INITIAL_HEIGHT = 12

# Splitter is deliberately transient: it survives a theme/language rebuild in
# this process, but is not a preference written to disk.
HISTORY_SPLITTER_WIDTH = 6
HISTORY_TABLE_MIN_HEIGHT = 170
HISTORY_DETAIL_MIN_HEIGHT = 140
# Arrow-keying down a page of fifty runs must not decode fifty PNGs, so the
# preview waits for the selection to settle.
HISTORY_PREVIEW_DELAY_MS = 160
HISTORY_WORK_MIN_WIDTH = 560
HISTORY_COLUMN_FLOOR = 24
# The user's own two columns have a resting width of a few characters and a
# working width.  Collapsed they still read as their zone, because colour keeps
# the identity that the text can no longer carry.
HISTORY_ANNOTATION_COLUMNS = ("label", "note")
HISTORY_ANNOTATION_COLLAPSED = 52
HISTORY_ANNOTATION_WORKING = {"label": 130, "note": 240}

# Super-Accordion evaluation zone. These are fixed INTERNAL display slots, not
# SQL columns: criteria remain rows in the annotation extension. Preallocating
# ten hidden slots keeps the grid adapter deterministic while enforcing the UX
# ceiling agreed for one Group. A slot only becomes visible when one concrete
# Group is the active filter context.
HISTORY_CRITERIA_MAX = 10
HISTORY_CRITERION_WIDTH = 108
# Five fixed renderer slots live inside the 108px criterion cell.  This is a UI
# geometry constant, deliberately NOT derived from Unicode/font measurements.
HISTORY_RATING_SLOT_WIDTH = 16
HISTORY_CRITERION_PLUS_WIDTH = 34
EVALUATION_PLUS_GLYPH = "✚"
EVALUATION_PLUS_COLUMN = "eval_plus"
EVALUATION_SLOT_COLUMNS = tuple(f"criterion_{i}" for i in range(HISTORY_CRITERIA_MAX))
EVALUATION_COLUMN_IDS = (EVALUATION_PLUS_COLUMN,) + EVALUATION_SLOT_COLUMNS
QUALITY_GLYPH_FULL, QUALITY_GLYPH_EMPTY = "★", "☆"
ISSUE_GLYPH_FULL, ISSUE_GLYPH_EMPTY = "▲", "△"

HISTORY_HINT_DELAY_MS = 400
HISTORY_HINT_LINES = 5
HISTORY_HINT_WIDTH = 62
HEADER_MENU_GLYPH = "\u2304"          # a visible handle, so nothing hides in a manual
HISTORY_PREVIEW_MIN_WIDTH = 220
HISTORY_PREVIEW_INITIAL_WIDTH = 360
# Decoded images are large; keep a few for going back and forth, not a gallery.
HISTORY_PREVIEW_CACHE = 4
# The long side of a close-up.  Tk divides by whole numbers only, so 512 is
# exact for a 1024 source and "as near as an integer divisor allows" for
# anything else - 832x1216 becomes 416x608.  Never an upscale: a 384 px result
# stays 384, because an enlarged thumbnail misrepresents what it stands for.
HISTORY_PREVIEW_LARGE = 512
HISTORY_DEFAULT_SPLIT_FRACTION = 0.58

# The tree/index column is a view scaffold, not the user's Group data column.
# A fixed 340 px slab made a two-run group consume almost a third of the table.
# Keep a modest default and tune the width to the active Collect-by dimension.
HISTORY_GROUP_COLUMN_WIDTH = 240
HISTORY_GROUP_COLUMN_MIN_WIDTH = 150
HISTORY_GROUP_COLUMN_BY_LOGICAL = {
    "label": 190,
    "recipe": 230,
    "model_name": 280,
    "prompt": 340,
}

PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class ColumnSpec:
    """Single source of truth for a History Treeview column.

    ``sample`` is a geometry contract, not user-visible data: it represents a
    realistic cell value that should fit without clipping at the current
    History body font.  The runtime minimum is measured from BOTH the translated
    heading (including sort-arrow reserve) and this sample, so DPI/font/language
    changes cannot silently invalidate a hard-coded pixel floor.

    Long free-text columns intentionally use representative rather than
    worst-case samples: Model/Failure/Prompt are allowed to elide or clip
    unusually long values instead of forcing a huge minimum width.

    ``getter`` produces display text. ``sort_key`` works on raw data and may
    return None; None is always sorted to the bottom in both directions.
    """

    id: str
    title: str
    width: int
    sample: str
    default_visible: bool
    getter: Callable[[Any, dict[str, Any]], Any]
    sort_key: Callable[[dict[str, Any]], Any] | None = None
    anchor: str = "w"
    translate_sample: bool = False


def _text_sort(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).casefold()


def _number_sort(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _col_date(tab: Any, row: dict[str, Any]) -> str:
    return kit_history.format_stamp(row.get("created_at") or row.get("started_at"))


def _sort_date(row: dict[str, Any]):
    return kit_history.parse_stamp(row.get("created_at") or row.get("started_at"))


def _col_status(tab: Any, row: dict[str, Any]) -> str:
    return tab._status_display(row.get("status"))


def _sort_status(row: dict[str, Any]):
    return _text_sort(row.get("status"))


def _col_model(tab: Any, row: dict[str, Any]) -> str:
    return kit_history.elide(row.get("model_name") or tab.tr("unknown"), 40)


def _sort_model(row: dict[str, Any]):
    return _text_sort(row.get("model_name"))


def _col_recipe(tab: Any, row: dict[str, Any]) -> str:
    return tab._recipe_display(row)


def _sort_recipe(row: dict[str, Any]):
    return _text_sort(row.get("recipe"))


def _col_resolution(_tab: Any, row: dict[str, Any]) -> str:
    width, height = row.get("width"), row.get("height")
    return f"{width} × {height}" if width and height else ""


def _sort_resolution(row: dict[str, Any]):
    try:
        width, height = int(row.get("width")), int(row.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (width * height, width, height)


def _col_steps(_tab: Any, row: dict[str, Any]) -> Any:
    return row.get("steps") if row.get("steps") is not None else ""


def _sort_steps(row: dict[str, Any]):
    return _number_sort(row.get("steps"))


def _col_seed(_tab: Any, row: dict[str, Any]) -> Any:
    return row.get("seed") if row.get("seed") is not None else ""


def _sort_seed(row: dict[str, Any]):
    return _number_sort(row.get("seed"))


def _col_scheduler(_tab: Any, row: dict[str, Any]) -> str:
    return str(row.get("scheduler") or "")


def _sort_scheduler(row: dict[str, Any]):
    return _text_sort(row.get("scheduler"))


def _col_cfg(_tab: Any, row: dict[str, Any]) -> Any:
    return row.get("cfg_scale") if row.get("cfg_scale") is not None else ""


def _sort_cfg(row: dict[str, Any]):
    return _number_sort(row.get("cfg_scale"))


def _col_time(_tab: Any, row: dict[str, Any]) -> str:
    return kit_history.format_seconds(row.get("wall_seconds"))


def _sort_time(row: dict[str, Any]):
    return _number_sort(row.get("wall_seconds"))


def _col_device(_tab: Any, row: dict[str, Any]) -> str:
    return str(row.get("device") or "")


def _sort_device(row: dict[str, Any]):
    return _text_sort(row.get("device"))


def _col_failure(_tab: Any, row: dict[str, Any]) -> str:
    return str(row.get("failure_node") or "")


def _sort_failure(row: dict[str, Any]):
    return _text_sort(row.get("failure_node"))


def _col_error_class(_tab: Any, row: dict[str, Any]) -> str:
    return str(row.get("failure_class") or row.get("error_type") or "")


def _sort_error_class(row: dict[str, Any]):
    return _text_sort(row.get("failure_class") or row.get("error_type"))


def _col_metadata(_tab: Any, row: dict[str, Any]) -> str:
    return str(row.get("metadata_source") or "")


def _sort_metadata(row: dict[str, Any]):
    return _text_sort(row.get("metadata_source"))


def _col_label(_tab: Any, row: dict[str, Any]) -> str:
    # A comment is far too long for a table cell, but the fact that one EXISTS
    # is exactly what someone scanning the list needs to see.
    text = kit_history.elide(row.get("label"), 20)
    if str(row.get("note") or "").strip():
        text = f"{text}  \u270e" if text else "\u270e"
    return text


def _col_comment(_tab: Any, row: dict[str, Any]) -> str:
    return kit_history.elide(row.get("note"), 60)


def _sort_comment(row: dict[str, Any]):
    return _text_sort(row.get("note"))


def _sort_label(row: dict[str, Any]):
    return _text_sort(row.get("label"))


def _col_prompt(_tab: Any, row: dict[str, Any]) -> str:
    return kit_history.elide(row.get("prompt"), 90)


def _col_pick(tab: Any, row: dict[str, Any]) -> str:
    try:
        run_id = int(row.get("run_id"))
    except (TypeError, ValueError):
        return ""
    return "☑" if run_id in tab._marked_run_ids else "☐"


def _col_eval_plus(_tab: Any, _row: dict[str, Any]) -> str:
    return ""


def _criterion_getter(slot: int):
    return lambda tab, row: tab._criterion_cell_text(slot, row)


def _criterion_sorter(slot: int):
    return lambda row: _number_sort(row.get(f"_rating_slot_{slot}"))


CRITERION_COLUMN_SPECS = tuple(
    ColumnSpec(
        EVALUATION_SLOT_COLUMNS[slot], "Criterion", HISTORY_CRITERION_WIDTH,
        "★★★★★", False, _criterion_getter(slot), _criterion_sorter(slot),
        anchor="center",
    )
    for slot in range(HISTORY_CRITERIA_MAX)
)


# Core columns preserve H7.2's default view. The optional columns use data
# query_runs already returned; enabling one is a display operation, not a new
# database query.
COLUMN_SPECS = (
    ColumnSpec("pick", "☐", 22, "☑", True, _col_pick, None, anchor="center"),
    ColumnSpec("date", "Date", 90, "30 Aug 23:59", True, _col_date, _sort_date),
    ColumnSpec(
        "status", "Status", 76, "In progress", True, _col_status, _sort_status,
        translate_sample=True,
    ),
    # Representative, not worst-case: very long model registrations are allowed
    # to elide rather than forcing every row to reserve their full length.
    ColumnSpec("model", "Model", 170, "Wan2.2-TI2V-5B", True, _col_model, _sort_model),
    ColumnSpec(
        "recipe", "Kit mode", 84, "not recorded", True, _col_recipe, _sort_recipe,
        translate_sample=True,
    ),
    # The user's own axis sits next to the machine ones, in the default view:
    # a label is only useful if it is visible without being switched on.
    ColumnSpec("label", "Group", 130, "bench_01_V100", True, _col_label, _sort_label),
    # The comment now has a column of its own.  The pencil in the Group cell
    # stays: once this column is collapsed to a few characters - its resting
    # state while the old columns are being read - that mark is again the only
    # thing that says a comment exists at all.
    ColumnSpec("note", "Comment", 180,
               "\u043b\u0443\u0447\u0448\u0435 \u043c\u0435\u043b\u043a\u0438\u0439 \u0442\u0435\u043a\u0441\u0442",
               True, _col_comment, _sort_comment),
    # Thin + plate and ten criterion slots are structural UI columns. They are
    # hidden unless one Group is active and the annotation zone is expanded.
    # Time follows them permanently as neutral context.
    ColumnSpec(EVALUATION_PLUS_COLUMN, EVALUATION_PLUS_GLYPH, HISTORY_CRITERION_PLUS_WIDTH,
               EVALUATION_PLUS_GLYPH, False, _col_eval_plus, None, anchor="center"),
    *CRITERION_COLUMN_SPECS,
    ColumnSpec("resolution", "Resolution", 86, "1024 × 1024", False, _col_resolution, _sort_resolution),
    ColumnSpec("steps", "Steps", 37, "150", False, _col_steps, _sort_steps, anchor="e"),
    ColumnSpec("seed", "Seed", 57, "4294967295", False, _col_seed, _sort_seed, anchor="e"),
    ColumnSpec("scheduler", "Scheduler", 64, "euler", False, _col_scheduler, _sort_scheduler),
    ColumnSpec("cfg", "CFG", 70, "12.5", False, _col_cfg, _sort_cfg, anchor="e"),
    ColumnSpec("time", "Time", 41, "1234.5 s", True, _col_time, _sort_time, anchor="e"),
    ColumnSpec("device", "Device", 48, "cuda:0", False, _col_device, _sort_device),
    # Optional since R1B: empty for every completed run, and 170 px is the
    # widest thing in the table doing nothing.  The data is untouched - the
    # column is one right-click away and Run details always shows a failure.
    ColumnSpec("failure", "Failure stage", 83, "ideogram4_text_encoder", False, _col_failure, _sort_failure),
    ColumnSpec("error_class", "Error class", 81, "OutOfMemoryError", False, _col_error_class, _sort_error_class),
    ColumnSpec("metadata", "Metadata source", 170, "execution_graph", False, _col_metadata, _sort_metadata),
    # Prompt intentionally stays unsortable in v1; the sample protects a useful
    # preview floor, not an impossible promise to fit an arbitrary full prompt.
    ColumnSpec(
        "prompt", "Prompt", 634, "A useful prompt preview remains readable",
        True, _col_prompt, None,
    ),
)
COLUMN_BY_ID = {spec.id: spec for spec in COLUMN_SPECS}
if len(COLUMN_BY_ID) != len(COLUMN_SPECS):
    raise RuntimeError("History column ids must be unique")
if any(spec.anchor not in {"w", "center", "e"} for spec in COLUMN_SPECS):
    raise RuntimeError("History column anchor must be w, center or e")

DEFAULT_VISIBLE_COLUMNS = tuple(spec.id for spec in COLUMN_SPECS if spec.default_visible)
# Evaluation columns are controlled by the Super-Accordion state, never by the
# generic right-click optional-column menu.
OPTIONAL_COLUMNS = tuple(
    spec for spec in COLUMN_SPECS
    if not spec.default_visible and spec.id not in EVALUATION_COLUMN_IDS
)

# Colour as information architecture.  Every column belongs to exactly one
# semantic zone; the grid paints zones, not columns, so a criterion column added
# later needs no entry here - it simply arrives carrying a zone.  Anything not
# named is machine-recorded provenance, which is the honest default.
COLUMN_ZONES = {
    "pick": "system",
    "label": "group",
    "note": "comment",
    EVALUATION_PLUS_COLUMN: "action",
    "time": "context",
}
DEFAULT_ZONE = "provenance"


def zone_of(column_id: str) -> str:
    return COLUMN_ZONES.get(column_id, DEFAULT_ZONE)


def flash_palette() -> dict[str, tuple[str, str, str]]:
    """Zone -> (background, foreground, edge) for the hover hint.

    A separate family from zone_palette on purpose: the hint has to say which
    column it came from and, at the same time, that it is not part of it.
    """
    return {
        "group": (str(theme.HISTORY_FLASH_GROUP_BG), str(theme.HISTORY_FLASH_GROUP_FG),
                  str(theme.HISTORY_FLASH_GROUP_EDGE)),
        "comment": (str(theme.HISTORY_FLASH_COMMENT_BG), str(theme.HISTORY_FLASH_COMMENT_FG),
                    str(theme.HISTORY_FLASH_COMMENT_EDGE)),
    }


def zone_palette() -> dict[str, tuple[str, str]]:
    """Zone -> (background, foreground) in the theme that is active now."""
    return {
        "system": (str(theme.HISTORY_ZONE_SYSTEM_BG), str(theme.HISTORY_ZONE_SYSTEM_FG)),
        "provenance": (str(theme.HISTORY_ZONE_PROVENANCE_BG), str(theme.HISTORY_ZONE_PROVENANCE_FG)),
        "group": (str(theme.HISTORY_ZONE_GROUP_BG), str(theme.HISTORY_ZONE_GROUP_FG)),
        "comment": (str(theme.HISTORY_ZONE_COMMENT_BG), str(theme.HISTORY_ZONE_COMMENT_FG)),
        "quality": (str(theme.HISTORY_ZONE_QUALITY_BG), str(theme.HISTORY_ZONE_QUALITY_FG)),
        "issue": (str(theme.HISTORY_ZONE_ISSUE_BG), str(theme.HISTORY_ZONE_ISSUE_FG)),
        "action": (str(theme.HISTORY_ZONE_ACTION_BG), str(theme.HISTORY_ZONE_ACTION_FG)),
        "context": (str(theme.HISTORY_ZONE_CONTEXT_BG), str(theme.HISTORY_ZONE_CONTEXT_FG)),
    }


def zone_header_palette() -> dict[str, tuple[str, str]]:
    """Zone -> header colours, deliberately independent from body colours.

    Dark headers sit one visual storey above the body; Light headers instead
    need *more* pigment, not more white.  Keeping this separate also means the
    selected-column highlight remains tksheet's selection surface rather than
    being accidentally brightened with the whole header row.
    """
    return {
        "system": (str(theme.HISTORY_HEADER_ZONE_SYSTEM_BG), str(theme.HISTORY_HEADER_ZONE_SYSTEM_FG)),
        "provenance": (str(theme.HISTORY_HEADER_ZONE_PROVENANCE_BG), str(theme.HISTORY_HEADER_ZONE_PROVENANCE_FG)),
        "group": (str(theme.HISTORY_HEADER_ZONE_GROUP_BG), str(theme.HISTORY_HEADER_ZONE_GROUP_FG)),
        "comment": (str(theme.HISTORY_HEADER_ZONE_COMMENT_BG), str(theme.HISTORY_HEADER_ZONE_COMMENT_FG)),
        "quality": (str(theme.HISTORY_HEADER_ZONE_QUALITY_BG), str(theme.HISTORY_HEADER_ZONE_QUALITY_FG)),
        "issue": (str(theme.HISTORY_HEADER_ZONE_ISSUE_BG), str(theme.HISTORY_HEADER_ZONE_ISSUE_FG)),
        "action": (str(theme.HISTORY_HEADER_ZONE_ACTION_BG), str(theme.HISTORY_HEADER_ZONE_ACTION_FG)),
        "context": (str(theme.HISTORY_HEADER_ZONE_CONTEXT_BG), str(theme.HISTORY_HEADER_ZONE_CONTEXT_FG)),
    }

# Windows returns this when a console process is ended with Ctrl+C / window
# close.  Naming it is not the same as calling it a clean shutdown: the exit
# code belongs to the host session, not to any inference run.
_EXIT_CODE_NAMES = {3221225786: "STATUS_CONTROL_C_EXIT"}

# Column text for each provenance state.  Kept apart from the state names so
# the distinction survives translation.
_EMPTY_GROUP = {
    "label": "no group",
    "prompt": "no prompt recorded",
    "model_name": "unknown model",
    "recipe": "not recorded",
}

_RECIPE_LABEL = {
    kit_history.PENDING: "pending",
    kit_history.UNRECORDED: "not recorded",
}


# Windows reports the physical virtual-key here regardless of the active
# keyboard layout, which is exactly what the layout-independent bindings need.
_CLIPBOARD_KEYCODES = {67: "<<Copy>>", 88: "<<Cut>>", 86: "<<Paste>>"}
_SELECT_ALL_KEYCODE = 65


def _preview_box(holder: tuple[int, int], large: bool) -> tuple[int, int]:
    """Which box the result is fitted into: the panel, or a 512 px close-up.

    Two working modes, not two thumbnail sizes.  Fit answers "which run is
    this" while the table is being read; 512 answers "is this one any good",
    which is what the whole evaluation matrix is being built for.  The
    original stays behind FULL Res - a dense table is the wrong place for it.
    """
    if not large:
        return holder
    return (max(holder[0], HISTORY_PREVIEW_LARGE), max(holder[1], HISTORY_PREVIEW_LARGE))


def _preview_scale(source: tuple[int, int], target: tuple[int, int]) -> int:
    """How many times to subsample an image so it fits the preview panel.

    Tk's own PhotoImage can only divide by whole numbers - there is no
    resampling in the standard library - so this returns the smallest integer
    that makes both dimensions fit.  1 means "show it as it is": a small image
    is never enlarged, because an upscaled thumbnail would misrepresent the
    result it is standing for.
    """
    width, height = source
    max_width, max_height = target
    if min(width, height, max_width, max_height) <= 0:
        return 1
    # Ceiling of each ratio: subsample(k) yields ceil(w / k) pixels, and
    # k = ceil(w / max) is the smallest divisor that keeps that within bounds.
    # No arbitrary ceiling on k - a 8K result should still be reduced to fit.
    return max(
        1,
        -(-width // max_width),
        -(-height // max_height),
    )


def _install_clipboard(widget: tk.Widget, *, tr: Callable[[str], str], dark: bool) -> None:
    """Give one text widget a clipboard that works on any keyboard layout.

    Tk resolves ``<Control-c>`` from the KEYSYM, so while a Cyrillic layout is
    active the character is not ``c`` and the default Text/Entry class bindings
    never fire.  The widget then looks like it simply has no clipboard.  Bind
    the physical keycode instead and raise the virtual events ourselves; add a
    right-click menu so the mouse-only path exists too.
    """
    def select_all() -> None:
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
        else:
            try:
                widget.select_range(0, "end")
            except Exception:
                pass

    def on_control(event):
        if event.keycode == _SELECT_ALL_KEYCODE:
            select_all()
            return "break"
        virtual = _CLIPBOARD_KEYCODES.get(event.keycode)
        if virtual is None:
            return None
        widget.event_generate(virtual)
        return "break"

    widget.bind("<Control-KeyPress>", on_control, add="+")

    # Parented to the widget, so Tk keeps it alive for the widget's lifetime.
    menu = tk.Menu(widget, tearoff=False)
    if dark:
        try:
            menu.configure(**theme.dark_menu_options())
        except Exception:
            pass
    for caption, virtual in ((tr("Copy"), "<<Copy>>"), (tr("Cut"), "<<Cut>>"),
                             (tr("Paste"), "<<Paste>>")):
        menu.add_command(
            label=caption,
            command=lambda event_name=virtual: widget.event_generate(event_name),
        )
    menu.add_separator()
    menu.add_command(label=tr("Select all"), command=select_all)

    def popup(event):
        widget.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget.bind("<Button-3>", popup, add="+")


def _about_for_group(
    records: dict[str, str], previous_key: str, new_key: str
) -> str | None:
    """Which description the About box must show after the group name changed.

    The description is SHARED, so carrying one group's text onto another would
    silently overwrite work that belongs to a whole set of runs.  The rule:

        now naming an existing group  -> always show THAT group's description
        was an existing group, now a new name -> show nothing; never inherit
        still typing a new name       -> leave what was typed alone (None)

    Returns the text to display, or None to leave the box untouched.
    """
    if new_key == previous_key:
        return None
    if new_key in records:
        return records[new_key]
    if previous_key in records:
        return ""
    return None


def _ask_annotation(
    parent: tk.Widget,
    *,
    tr: Callable[[str], str],
    title: str,
    prompt: str,
    groups: list[dict[str, Any]],
    subject: str = "",
    group: str = "",
    about: str = "",
    note: str | None = None,
    dark: bool = False,
) -> tuple[str, str, str] | None:
    """Editor for group membership, the group description and one comment.

    Two levels live here and they are deliberately not symmetrical.  The group
    description is SHARED by every run in the group, so the dialog says so;
    the comment belongs to one inference and is offered only on the single-run
    path (``note=None`` is the bulk path).

    ``subject`` names what is being edited.  History has two selections at once
    - ticked rows for bulk actions, one highlighted row for details - and
    without saying which one this dialog is about, the user has to guess.

    Returns None when the user cancels; empty strings are real answers and mean
    "clear it".
    """
    window = tk.Toplevel(parent)
    window.title(title)
    window.transient(parent.winfo_toplevel())
    window.configure(bg=theme.SURFACE_APP)
    answer: dict[str, tuple[str, str, str] | None] = {"value": None}

    tk.Label(
        window, text=prompt, bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
        justify="left", anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 2))
    if subject:
        tk.Label(
            window, text=subject, bg=theme.SURFACE_APP, fg=theme.TEXT_MUTED,
            justify="left", anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 6))

    records = {
        kit_history.group_key(item.get("name")): str(item.get("about") or "")
        for item in groups
    }
    group_var = tk.StringVar(value=str(group or ""))
    group_box = ttk.Combobox(
        window, values=[str(item.get("name")) for item in groups],
        textvariable=group_var, width=36,
    )
    group_box.pack(fill="x", padx=12)
    _install_clipboard(group_box, tr=tr, dark=dark)
    tk.Label(
        window,
        text=tr("Existing groups are offered; the name is matched ignoring case."),
        bg=theme.SURFACE_APP, fg=theme.TEXT_MUTED, justify="left", anchor="w",
    ).pack(fill="x", padx=12, pady=(4, 0))

    tk.Label(
        window, text=tr("About this group"), bg=theme.SURFACE_APP,
        fg=theme.TEXT_SECTION, anchor="w",
    ).pack(fill="x", padx=12, pady=(10, 2))
    about_box = tk.Text(
        window, width=46, height=3, wrap="word", relief="flat",
        bg=theme.SURFACE_CARD, fg=theme.TEXT_PRIMARY,
        insertbackground=theme.TEXT_PRIMARY,
    )
    about_box.insert("1.0", str(about or ""))
    about_box.pack(fill="x", padx=12)
    _install_clipboard(about_box, tr=tr, dark=dark)

    # The box must always show the description of the group currently named,
    # otherwise Save writes one group's text onto another.  A trace catches
    # both picking from the list and typing, which <<ComboboxSelected>> alone
    # would not.
    shown = {"key": kit_history.group_key(group)}

    def _group_named(*_args) -> None:
        new_key = kit_history.group_key(group_var.get())
        text = _about_for_group(records, shown["key"], new_key)
        shown["key"] = new_key
        if text is None:
            return
        about_box.delete("1.0", "end")
        about_box.insert("1.0", text)

    group_var.trace_add("write", _group_named)
    tk.Label(
        window, text=tr("Shared by every run in this group."),
        bg=theme.SURFACE_APP, fg=theme.TEXT_MUTED, justify="left", anchor="w",
    ).pack(fill="x", padx=12, pady=(4, 0))

    def confirm() -> None:
        answer["value"] = (
            group_var.get().strip(),
            about_box.get("1.0", "end-1c").strip(),
            comment.get("1.0", "end-1c").strip() if comment is not None else "",
        )
        window.destroy()

    # Packed before the comment box so the buttons keep the bottom edge while
    # the comment takes every pixel the user gives the window.
    buttons = tk.Frame(window, bg=theme.SURFACE_APP)
    buttons.pack(side="bottom", fill="x", padx=12, pady=12)
    tk.Button(
        buttons, text=tr("Cancel"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
        padx=10, pady=3, cursor="hand2", command=window.destroy,
    ).pack(side="right")
    tk.Button(
        buttons, text=tr("Save"), relief="flat", bg=theme.SURFACE_CONTROL,
        padx=10, pady=3, cursor="hand2", command=confirm,
    ).pack(side="right", padx=(0, 6))

    comment: tk.Text | None = None
    if note is not None:
        tk.Label(
            window, text=tr("Comment"), bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 4))
        comment_box = tk.Frame(window, bg=theme.SURFACE_APP)
        comment_box.pack(fill="both", expand=True, padx=12)
        comment = tk.Text(
            comment_box, width=46, height=7, wrap="word",
            relief="flat", bg=theme.SURFACE_CARD, fg=theme.TEXT_PRIMARY,
            insertbackground=theme.TEXT_PRIMARY,
        )
        comment_scroll = ttk.Scrollbar(
            comment_box, orient="vertical", command=comment.yview
        )
        comment.configure(yscrollcommand=comment_scroll.set)
        comment.insert("1.0", str(note or ""))
        comment.pack(side="left", fill="both", expand=True)
        comment_scroll.pack(side="right", fill="y")
        _install_clipboard(comment, tr=tr, dark=dark)

    window.bind("<Escape>", lambda _e: window.destroy())
    window.resizable(True, True)
    if comment is None:
        window.minsize(420, 240)
        window.geometry("560x300")
    else:
        window.minsize(420, 380)
        window.geometry("560x500")
    group_box.focus_set()

    # Open over the Panel rather than wherever the window manager felt like.
    window.update_idletasks()
    root = parent.winfo_toplevel()
    try:
        x = root.winfo_rootx() + max(0, (root.winfo_width() - window.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - window.winfo_height()) // 3)
        window.geometry(f"+{x}+{y}")
    except Exception:
        pass

    window.grab_set()
    parent.wait_window(window)
    return answer["value"]


def _ask_criterion(
    parent: tk.Widget,
    *,
    tr: Callable[[str], str],
    suggestions: list[str],
    dark: bool = False,
) -> tuple[str, str] | None:
    """Create one Group-owned evaluation axis.

    Names used elsewhere are suggestions, not shared identity.  Polarity is
    explicit and never inferred from words in the criterion name.
    """
    window = tk.Toplevel(parent)
    window.title(tr("Add criterion"))
    window.transient(parent.winfo_toplevel())
    window.configure(bg=theme.SURFACE_APP)
    answer: dict[str, tuple[str, str] | None] = {"value": None}

    tk.Label(
        window, text=tr("Criterion name"), bg=theme.SURFACE_APP,
        fg=theme.TEXT_SECTION, anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 4))
    name_var = tk.StringVar()
    box = ttk.Combobox(window, values=suggestions, textvariable=name_var, width=38)
    box.pack(fill="x", padx=12)
    _install_clipboard(box, tr=tr, dark=dark)
    tk.Label(
        window, text=tr("Names used in other groups are offered as suggestions."),
        bg=theme.SURFACE_APP, fg=theme.TEXT_MUTED, anchor="w", justify="left",
    ).pack(fill="x", padx=12, pady=(4, 10))

    tk.Label(
        window, text=tr("What does a higher mark mean?"), bg=theme.SURFACE_APP,
        fg=theme.TEXT_SECTION, anchor="w",
    ).pack(fill="x", padx=12, pady=(0, 4))
    kind_var = tk.StringVar(value=kit_history.CRITERION_QUALITY)
    for value, caption, bg, fg in (
        (kit_history.CRITERION_QUALITY, tr("★ Quality — more stars is better"),
         theme.HISTORY_ZONE_QUALITY_BG, theme.HISTORY_ZONE_QUALITY_FG),
        (kit_history.CRITERION_ISSUE, tr("▲ Issue — more triangles is worse"),
         theme.HISTORY_ZONE_ISSUE_BG, theme.HISTORY_ZONE_ISSUE_FG),
    ):
        tk.Radiobutton(
            window, text=caption, variable=kind_var, value=value,
            bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
            selectcolor=bg, anchor="w", padx=8, pady=4,
        ).pack(fill="x", padx=12, pady=(0, 3))

    def confirm() -> None:
        name = name_var.get().strip()
        if not name:
            return
        answer["value"] = (name, kind_var.get())
        window.destroy()

    buttons = tk.Frame(window, bg=theme.SURFACE_APP)
    buttons.pack(fill="x", padx=12, pady=12)
    tk.Button(
        buttons, text=tr("Cancel"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
        padx=10, pady=3, cursor="hand2", command=window.destroy,
    ).pack(side="right")
    tk.Button(
        buttons, text=tr("Add"), relief="flat", bg=theme.SURFACE_CONTROL,
        padx=10, pady=3, cursor="hand2", command=confirm,
    ).pack(side="right", padx=(0, 6))

    window.bind("<Escape>", lambda _e: window.destroy())
    window.bind("<Return>", lambda _e: confirm())
    window.resizable(False, False)
    window.update_idletasks()
    root = parent.winfo_toplevel()
    try:
        x = root.winfo_rootx() + max(0, (root.winfo_width() - window.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - window.winfo_height()) // 3)
        window.geometry(f"+{x}+{y}")
    except Exception:
        pass
    box.focus_set()
    window.grab_set()
    parent.wait_window(window)
    return answer["value"]


class HistoryTab:
    def __init__(
        self,
        parent: tk.Widget,
        *,
        menu_parent: tk.Misc,
        tr: Callable[[str], str],
        clipboard: Callable[[str], None],
        theme_name_getter: Callable[[], str],
        history_db: Path,
        outputs_dir: Path,
        collect: Callable[[], tuple[int, str]] | None,
        update_idletasks: Callable[[], None],
        after: Callable[..., str],
        strings: dict[str, object],
    ) -> None:
        self.tab_history = parent
        self._menu_parent = menu_parent
        self.tr = tr
        self._clipboard = clipboard
        self._theme_name_getter = theme_name_getter
        self._history_db = history_db
        self._outputs_dir = outputs_dir
        self._collect = collect
        self._update_idletasks = update_idletasks
        self._after = after
        self._strings = strings

        self._handle: kit_history.HistoryHandle | None = None
        self._preview_cache: dict[str, Any] = {}
        self._preview_shown: Any = None
        self._preview_job: str | None = None
        self._preview_run: int | None = None
        self._preview_scaled: dict[tuple[str, int], Any] = {}
        # Collapsed is the resting state: the user's own axis stays a coloured
        # stripe until it is wanted.  Both the state and the working widths
        # live in _UI_STATE_BY_DB, because the widths survive a theme rebuild
        # and a flag that did not would disagree with what is on screen.
        self._zone_expanded = False
        self._zone_widths: dict[str, int] = {}
        self._hint: tk.Toplevel | None = None
        self._hint_job: str | None = None
        self._hint_key: tuple[str, str] | None = None
        self._preview_geometry: tuple[int, int] | None = None
        self._rows: list[dict[str, Any]] = []
        self._sessions: list[Any] = []
        self._groups: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}
        self._by_iid: dict[str, dict[str, Any]] = {}
        self._selected_detail: dict[str, Any] | None = None
        self._marked_run_ids: set[int] = set()

        # Evaluation is a view of ONE active Group. Stable DB identities stay
        # in the reader/writer; these are only the current display slots.
        self._active_eval_group: dict[str, Any] | None = None
        self._criteria: list[dict[str, Any]] = []
        self._criterion_by_column: dict[str, dict[str, Any]] = {}
        self._ratings: dict[tuple[int, int], int] = {}

        # Transient view state.  It is mirrored into _UI_STATE_BY_DB during a
        # theme/language rebuild, never written to disk.
        self._page = 1
        self._sort_column = "date"
        self._sort_desc = True
        self._saved_split_fraction = HISTORY_DEFAULT_SPLIT_FRACTION
        self._column_vars: dict[str, tk.BooleanVar] = {}

        # Development-only bridge into the separately qualified typography layer.
        # Nothing is loaded or applied during normal History startup: opening the
        # Lab is the explicit boundary where the designer opts into live fitting.
        self._font_system: Any | None = None
        self._typography_lab: Any | None = None

    # -- lifecycle ---------------------------------------------------------
    def dispose(self) -> None:
        """Release the read-only handle.  Called on panel rebuild."""
        # Theme/language switching destroys and rebuilds History.  A Toplevel
        # created by Typography Lab must not survive with a dead grid reference.
        lab = self._typography_lab
        if lab is not None:
            try:
                if lab.top.winfo_exists():
                    lab.top.destroy()
            except Exception:
                pass
        self._typography_lab = None
        self._font_system = None

        self._remember_ui_state()
        if self._preview_job is not None:
            try:
                self.tab_history.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None
        self._preview_shown = None
        self._hide_hint()
        self._preview_cache.clear()
        self._preview_scaled.clear()
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _ui_state_key(self) -> str:
        try:
            return str(self._history_db.resolve())
        except Exception:
            return str(self._history_db)

    def _current_split_fraction(self) -> float | None:
        panes = getattr(self, "history_panes", None)
        if panes is None:
            return None
        try:
            height = int(panes.winfo_height())
            if height < HISTORY_TABLE_MIN_HEIGHT + HISTORY_DETAIL_MIN_HEIGHT:
                return None
            _x, y = panes.sash_coord(0)
            fraction = float(y) / float(height)
            return min(0.82, max(0.25, fraction))
        except Exception:
            return None

    def _remember_ui_state(self) -> None:
        """Remember view choices across a rebuild, not across restarts."""
        required = (
            "f_status", "f_model", "f_recipe", "f_label", "f_group", "f_period",
            "from_var", "to_var", "search_var", "page_size_var", "show_removed_var",
            "preview_var",
        )
        if not all(hasattr(self, name) for name in required):
            return

        group_logical = None
        chosen_group = self.f_group.get()
        for label, logical in GROUPINGS:
            if chosen_group == self.tr(label):
                group_logical = logical
                break

        period_key = CUSTOM
        chosen_period = self.f_period.get()
        for label, _days in PERIODS:
            if chosen_period == self.tr(label):
                period_key = label
                break

        old_state = _UI_STATE_BY_DB.get(self._ui_state_key()) or {}
        split_fraction = self._current_split_fraction()
        if split_fraction is None:
            split_fraction = float(old_state.get("split_fraction") or self._saved_split_fraction)

        optional = [
            spec.id for spec in OPTIONAL_COLUMNS
            if self._column_vars.get(spec.id) is not None and self._column_vars[spec.id].get()
        ]
        try:
            page_size = int(self.page_size_var.get())
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE

        _UI_STATE_BY_DB[self._ui_state_key()] = {
            "status": self.f_status.get(),
            "model": self.f_model.get(),
            "recipe": self.f_recipe.get(),
            "preview": bool(self.preview_var.get()),
            "preview_large": bool(self.preview_large_var.get()),
            "label": self.f_label.get(),
            "group": group_logical,
            "period": period_key,
            "from": self.from_var.get(),
            "to": self.to_var.get(),
            "search": self.search_var.get(),
            "page": max(1, int(self._page)),
            "page_size": page_size if page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE,
            "sort_column": self._sort_column,
            "sort_desc": bool(self._sort_desc),
            "optional_columns": optional,
            "split_fraction": split_fraction,
            "show_removed": bool(self.show_removed_var.get()),
            "column_widths": self._current_column_widths(old_state),
            "zone_expanded": bool(self._zone_expanded),
            "zone_widths": dict(self._zone_widths),
        }

    def _current_column_widths(self, old_state: dict[str, Any]) -> dict[str, int]:
        """Widths the user dragged, so a theme switch does not undo them.

        Changing the theme rebuilds the whole Panel, which throws the table
        away and builds a new one at the default widths.  Everything else the
        user had arranged already survives that through this state, and column
        widths belong in the same place.
        """
        if self._grid_is_native:
            return dict(old_state.get("column_widths") or {})
        try:
            widths = self.tree.current_widths()
        except Exception:
            return dict(old_state.get("column_widths") or {})
        # Printed so a layout arranged by hand can become the new default
        # without anyone estimating pixels from a screenshot.
        print("[History] column widths: {"
              + ", ".join(f'"{cid}": {widths[cid]}'
                          for cid in (spec.id for spec in COLUMN_SPECS) if cid in widths)
              + "}")
        return widths

    def _restore_ui_state(self) -> None:
        state = _UI_STATE_BY_DB.get(self._ui_state_key())
        if not state:
            return
        self._zone_expanded = bool(state.get("zone_expanded", False))
        self._zone_widths = dict(state.get("zone_widths") or {})
        widths = state.get("column_widths") or {}
        if widths and not self._grid_is_native:
            try:
                self.tree.set_widths(widths)
            except Exception:
                pass
        self.f_status.set(state.get("status") or ALL)
        self.f_model.set(state.get("model") or ALL)
        self.f_recipe.set(state.get("recipe") or ALL)
        self.f_label.set(state.get("label") or ALL)
        self.preview_large_var.set(bool(state.get("preview_large", False)))
        if state.get("preview"):
            self.preview_var.set(True)
            self.toggle_preview()
        self.search_var.set(state.get("search") or "")
        self.from_var.set(state.get("from") or "")
        self.to_var.set(state.get("to") or "")

        logical = state.get("group")
        if logical:
            for label, candidate in GROUPINGS:
                if candidate == logical:
                    self.f_group.set(self.tr(label))
                    break
        else:
            self.f_group.set(NO_GROUP)

        period_key = state.get("period") or "All"
        self.f_period.set(self.tr(period_key))

        try:
            page_size = int(state.get("page_size") or DEFAULT_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        if page_size not in PAGE_SIZES:
            page_size = DEFAULT_PAGE_SIZE
        self.page_size_var.set(str(page_size))
        self._page = max(1, int(state.get("page") or 1))

        sort_column = str(state.get("sort_column") or "date")
        if sort_column not in COLUMN_BY_ID or COLUMN_BY_ID[sort_column].sort_key is None:
            sort_column = "date"
        self._sort_column = sort_column
        self._sort_desc = bool(state.get("sort_desc", True))

        enabled = set(state.get("optional_columns") or ())
        for spec in OPTIONAL_COLUMNS:
            var = self._column_vars.get(spec.id)
            if var is not None:
                var.set(spec.id in enabled)

        try:
            fraction = float(state.get("split_fraction") or HISTORY_DEFAULT_SPLIT_FRACTION)
        except (TypeError, ValueError):
            fraction = HISTORY_DEFAULT_SPLIT_FRACTION
        self._saved_split_fraction = min(0.82, max(0.25, fraction))
        self.show_removed_var.set(bool(state.get("show_removed", False)))

    # -- build -------------------------------------------------------------
    def build_history(self) -> None:
        self._configure_tree_style()

        # Row 1: primary journal actions + compact categorical filters.
        bar = tk.Frame(self.tab_history, bg=theme.SURFACE_APP)
        bar.pack(fill="x", padx=6, pady=(10, 4))

        self.refresh_button = tk.Button(
            bar, text=self.tr("Sync now"), relief="flat", bd=0,
            bg=theme.HISTORY_SYNC_BG, fg=theme.HISTORY_SYNC_FG,
            activebackground=theme.HISTORY_SYNC_ACTIVE_BG,
            activeforeground=theme.HISTORY_SYNC_FG,
            highlightthickness=1, highlightbackground=theme.HISTORY_SYNC_EDGE,
            highlightcolor=theme.HISTORY_SYNC_EDGE,
            padx=10, pady=4, cursor="hand2", command=self.refresh_journal,
        )
        self.refresh_button.pack(side="left", padx=(4, 4))

        self.save_button = tk.Button(
            bar, text=self.tr("Save a copy…"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
            padx=8, pady=4, cursor="hand2", command=self.save_journal_copy,
        )
        self.save_button.pack(side="left", padx=(0, 8))

        self.f_status = self._filter(bar, "Status")
        self.f_model = self._filter(bar, "Model", width=26)
        self.f_recipe = self._filter(bar, "Kit mode")

        tk.Label(bar, text=self.tr("Collect by"), bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION).pack(
            side="left", padx=(12, 4)
        )
        self.f_group = ttk.Combobox(
            bar, values=[NO_GROUP] + [self.tr(label) for label, _ in GROUPINGS],
            state="readonly", width=12,
        )
        self.f_group.set(NO_GROUP)
        self.f_group.pack(side="left")
        self.f_group.bind("<<ComboboxSelected>>", lambda _e: self._group_changed())

        # Row 2: calendar bounds + the expanding Search field + Apply/Clear.
        # Search gets the spare width instead of being squeezed by row 1.
        period_bar = tk.Frame(self.tab_history, bg=theme.SURFACE_APP)
        period_bar.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(period_bar, text=self.tr("Period"), bg=theme.SURFACE_APP,
                 fg=theme.TEXT_SECTION).pack(side="left", padx=(0, 4))
        self.f_period = ttk.Combobox(
            period_bar, values=[self.tr(label) for label, _ in PERIODS],
            state="readonly", width=14,
        )
        self.f_period.set(self.tr("All"))
        self.f_period.pack(side="left")
        self.f_period.bind("<<ComboboxSelected>>", lambda _e: self._period_chosen())
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        for label, var in ((self.tr("from"), self.from_var), (self.tr("to"), self.to_var)):
            tk.Label(period_bar, text=label, bg=theme.SURFACE_APP,
                     fg=theme.TEXT_SECTION).pack(side="left", padx=(10, 4))
            entry = tk.Entry(
                period_bar, textvariable=var, width=11, relief="flat",
                bg=theme.SURFACE_CONTROL_ALT, fg=theme.TEXT_PRIMARY,
                insertbackground=theme.TEXT_PRIMARY, justify="center",
            )
            entry.pack(side="left")
            entry.bind("<Return>", lambda _e: self._custom_dates())
            entry.bind("<FocusOut>", lambda _e: self._custom_dates())
        # Group is the doorway into the Super-Accordion.  It occupies the
        # former redundant YYYY-MM-DD hint's place and carries the same plum
        # semantics as the Group column below, so the relationship is visible
        # before the user has learned it from a manual.
        self.group_filter_plate = tk.Frame(
            period_bar, bg=theme.HISTORY_GROUP_FILTER_EDGE, bd=0,
            highlightthickness=0,
        )
        self.group_filter_plate.pack(side="left", padx=(12, 0))
        tk.Label(
            self.group_filter_plate, text=self.tr("Group"),
            bg=theme.HISTORY_GROUP_FILTER_BG, fg=theme.HISTORY_GROUP_FILTER_FG,
            padx=7, pady=2,
        ).pack(side="left", padx=1, pady=1)
        self.f_label = ttk.Combobox(
            self.group_filter_plate, values=[ALL], state="readonly", width=12,
        )
        self.f_label.set(ALL)
        self.f_label.pack(side="left", padx=(0, 1), pady=1)
        self.f_label.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())

        clear_button = tk.Button(
            period_bar, text=self.tr("Clear"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
            padx=8, pady=3, cursor="hand2", command=self.clear_filters,
        )
        clear_button.pack(side="right", padx=(4, 0))
        apply_button = tk.Button(
            period_bar, text=self.tr("Apply"), relief="flat", bg=theme.SURFACE_CONTROL,
            padx=8, pady=3, cursor="hand2", command=self.apply_filters,
        )
        apply_button.pack(side="right", padx=(6, 0))

        tk.Label(period_bar, text=self.tr("Search"), bg=theme.SURFACE_APP,
                 fg=theme.TEXT_SECTION).pack(side="left", padx=(14, 4))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            period_bar, textvariable=self.search_var, width=24, relief="flat",
            bg=theme.SURFACE_CONTROL_ALT, fg=theme.TEXT_PRIMARY,
            insertbackground=theme.TEXT_PRIMARY,
        )
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _e: self.apply_filters())

        # Row 3: outcome only.  Controls stay above; this row reads as the result
        # of the current selection plus the last explicit Sync.
        summary_bar = tk.Frame(self.tab_history, bg=theme.SURFACE_APP)
        summary_bar.pack(fill="x", padx=10, pady=(0, 4))
        self.count_label = tk.Label(
            summary_bar, text="", bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION, anchor="w",
        )
        self.count_label.pack(side="left")
        self.refresh_state = tk.Label(
            summary_bar, text="", bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION, anchor="w",
        )
        self.refresh_state.pack(side="left", padx=(18, 0))

        # Image-view controls belong to the image workspace, not to Run details.
        # Keeping them above the table makes the three levels legible as one
        # compact mode strip: embedded preview, 512 close-up, external FULL Res.
        self.preview_var = tk.BooleanVar(value=False)
        self.preview_large_var = tk.BooleanVar(value=False)
        self.preview_controls = tk.Frame(summary_bar, bg=theme.SURFACE_APP)
        self.preview_controls.pack(side="right")

        self.preview_plate = tk.Frame(
            self.preview_controls, bd=0, highlightthickness=1,
            highlightbackground=theme.HISTORY_PREVIEW_TOGGLE_EDGE,
            highlightcolor=theme.HISTORY_PREVIEW_TOGGLE_EDGE,
            bg=theme.HISTORY_PREVIEW_TOGGLE_BG,
        )
        self.preview_plate.pack(side="left", padx=(0, 6))
        self.preview_toggle = tk.Checkbutton(
            self.preview_plate, text=self.tr("Preview"), variable=self.preview_var,
            command=self.toggle_preview, relief="flat", bd=0, padx=7, pady=1,
            bg=theme.HISTORY_PREVIEW_TOGGLE_BG, fg=theme.HISTORY_PREVIEW_TOGGLE_FG,
            activebackground=theme.HISTORY_PREVIEW_TOGGLE_ON_BG,
            activeforeground=theme.HISTORY_PREVIEW_TOGGLE_FG,
            selectcolor=theme.HISTORY_PREVIEW_TOGGLE_ON_BG,
            highlightthickness=0, cursor="hand2",
        )
        self.preview_toggle.pack()

        self.preview_large_plate = tk.Frame(
            self.preview_controls, bd=0, highlightthickness=1,
            highlightbackground=theme.HISTORY_PREVIEW_512_EDGE,
            highlightcolor=theme.HISTORY_PREVIEW_512_EDGE,
            bg=theme.HISTORY_PREVIEW_512_DISABLED_BG,
        )
        self.preview_large_plate.pack(side="left", padx=(0, 6))
        self.preview_large_toggle = tk.Checkbutton(
            self.preview_large_plate, text=str(HISTORY_PREVIEW_LARGE),
            variable=self.preview_large_var, command=self.toggle_preview_size,
            relief="flat", bd=0, padx=7, pady=1,
            bg=theme.HISTORY_PREVIEW_512_DISABLED_BG,
            fg=theme.HISTORY_PREVIEW_512_DISABLED_FG,
            disabledforeground=theme.HISTORY_PREVIEW_512_DISABLED_FG,
            activebackground=theme.HISTORY_PREVIEW_512_ON_BG,
            activeforeground=theme.HISTORY_PREVIEW_512_FG,
            selectcolor=theme.HISTORY_PREVIEW_512_ON_BG,
            highlightthickness=0, cursor="hand2", state="disabled",
        )
        self.preview_large_toggle.pack()

        # Product language: FULL Res is intentionally invariant in RU/EN.
        self.open_button = tk.Button(
            self.preview_controls, text="FULL Res", relief="flat", bd=0,
            bg=theme.HISTORY_FULLRES_BG, fg=theme.HISTORY_FULLRES_FG,
            activebackground=theme.HISTORY_FULLRES_ACTIVE_BG,
            activeforeground=theme.HISTORY_FULLRES_FG,
            highlightthickness=1, highlightbackground=theme.HISTORY_FULLRES_EDGE,
            highlightcolor=theme.HISTORY_FULLRES_EDGE,
            padx=11, pady=2, cursor="hand2", command=self.open_result,
        )
        self.open_button.pack(side="left")
        self._style_preview_controls()

        # TEMPORARY DEVELOPMENT HOOK.  Keep it out of the permanent History UX
        # until the real-grid fitting round is accepted.  Because preview_controls
        # was packed first on the right, this button lands immediately to its left.
        self.typography_lab_button = tk.Button(
            summary_bar,
            text="Typography Lab…",
            relief="flat",
            bd=0,
            bg=theme.SURFACE_CONTROL_ALT,
            fg=theme.TEXT_PRIMARY,
            activebackground=theme.SURFACE_CONTROL,
            activeforeground=theme.TEXT_PRIMARY,
            padx=8,
            pady=2,
            cursor="hand2",
            command=self._open_typography_lab,
        )
        self.typography_lab_button.pack(side="right", padx=(0, 8))

        self.migration_bar = tk.Frame(self.tab_history, bg=theme.SURFACE_WARNING)
        tk.Label(
            self.migration_bar, bg=theme.SURFACE_WARNING, fg=theme.TEXT_WARNING_NOTICE,
            anchor="w", justify="left", wraplength=1100,
            text=self.tr("Groups and comments are hidden: this journal was written by an "
                         "earlier Kit and needs a one-time migration."),
        ).pack(side="left", padx=8, pady=5)
        tk.Button(
            self.migration_bar, text=self.tr("Migrate now\u2026"), relief="flat",
            bg=theme.SURFACE_CONTROL_ALT, fg=theme.TTK_TEXT, command=self.migrate_journal,
        ).pack(side="right", padx=8, pady=4)

        # The result stands beside the WHOLE working area now.  Table and Run
        # details stay stacked on the left and keep their own vertical sash, so
        # the picture no longer takes its width from the details text.
        self.detail_panes = tk.PanedWindow(
            self.tab_history, orient="horizontal", bd=0, relief="flat",
            sashwidth=HISTORY_SPLITTER_WIDTH, sashrelief="flat",
            opaqueresize=True, showhandle=False, bg=theme.OUTLINE_PANEL,
        )
        self.detail_panes.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        work_area = tk.Frame(self.detail_panes, bg=theme.SURFACE_APP)
        self.detail_panes.add(work_area, minsize=HISTORY_WORK_MIN_WIDTH, stretch="always")

        # One draggable vertical split owns both the table and Run details.
        self.history_panes = tk.PanedWindow(
            work_area,
            orient="vertical",
            bd=0,
            relief="flat",
            sashwidth=HISTORY_SPLITTER_WIDTH,
            sashrelief="flat",
            opaqueresize=True,
            showhandle=False,
            bg=theme.OUTLINE_PANEL,
        )
        self.history_panes.pack(fill="both", expand=True)

        table_box = tk.Frame(self.history_panes, bg=theme.SURFACE_APP)
        detail_box = tk.Frame(self.history_panes, bg=theme.HISTORY_DETAIL_PANEL_BG)
        self.history_panes.add(table_box, minsize=HISTORY_TABLE_MIN_HEIGHT, stretch="always")
        self.history_panes.add(detail_box, minsize=HISTORY_DETAIL_MIN_HEIGHT, stretch="always")

        tree_area = tk.Frame(table_box, bg=theme.SURFACE_APP)
        tree_area.pack(fill="both", expand=True)
        tree_area.grid_rowconfigure(0, weight=1)
        tree_area.grid_columnconfigure(0, weight=1)

        self._column_vars = {
            spec.id: tk.BooleanVar(value=False) for spec in OPTIONAL_COLUMNS
        }
        # The grid speaks the Treeview verbs either way; kit_grid.BACKEND
        # decides which widget answers them.  The stock path stays until the
        # ported panel has been live-tested.
        self._grid_is_native = not kit_grid.available()
        # A port that quietly runs on the old widget looks exactly like a port
        # that was never installed, so which backend answered is stated once,
        # where the operator will see it.
        print(f"[History] {kit_grid.backend_report()}")
        if self._grid_is_native:
            self.tree = kit_grid.make_treeview(
                tree_area,
                columns=[spec.id for spec in COLUMN_SPECS],
                displaycolumns=DEFAULT_VISIBLE_COLUMNS,
                show="headings", height=12, style="History.Treeview",
            )
        else:
            self.tree = kit_grid.HistoryGrid(
                tree_area,
                columns=[spec.id for spec in COLUMN_SPECS],
                displaycolumns=DEFAULT_VISIBLE_COLUMNS,
                titles={spec.id: self.tr(spec.title) for spec in COLUMN_SPECS},
                show="headings", height=12,
                table_font=HISTORY_TABLE_FONT,
                header_font=HISTORY_HEADER_FONT,
                row_height=HISTORY_TABLE_ROW_HEIGHT,
                tree_width=HISTORY_GROUP_COLUMN_WIDTH,
            )
        # Measure the actual fonts History uses, not TkDefaultFont/TkHeadingFont.
        # This makes minimum widths adapt to DPI/font changes and translated
        # headings instead of relying on pixel floors measured on one machine.
        self._history_body_font = tkfont.Font(
            root=self.tab_history, font=HISTORY_TABLE_FONT
        )
        self._history_header_font = tkfont.Font(
            root=self.tab_history, font=HISTORY_HEADER_FONT
        )
        self._history_column_padding = max(
            12, self._history_body_font.measure("0") + 4
        )

        for spec in COLUMN_SPECS:
            # spec.width is a layout arranged by hand on a real screen, not a
            # guess, so it is used as given.  The font-measured value stays as
            # the resize floor: the user may narrow a column below its heading
            # deliberately, but nothing should collapse to nothing.
            floor = min(self._column_minwidth(spec), max(spec.width, HISTORY_COLUMN_FLOOR))
            if spec.id in EVALUATION_SLOT_COLUMNS:
                # Fixed segmented control: 5 * 16px = 80px, centred in a 108px
                # cell.  Never derive this floor from star/triangle font metrics.
                floor = max(floor, HISTORY_CRITERION_WIDTH)
            self.tree.column(
                spec.id,
                width=spec.width,
                minwidth=floor,
                stretch=(spec.id == "prompt"),
                anchor=spec.anchor,
            )
        self.tree.heading("#0", text=self.tr("Group"))
        self.tree.column(
            "#0", width=HISTORY_GROUP_COLUMN_WIDTH,
            minwidth=HISTORY_GROUP_COLUMN_MIN_WIDTH, stretch=False, anchor="w",
        )
        if not self._grid_is_native:
            for column_id in EVALUATION_SLOT_COLUMNS:
                self.tree.set_segments(
                    column_id,
                    count=kit_history.RATING_MAX,
                    slot_width=HISTORY_RATING_SLOT_WIDTH,
                    redraw=False,
                )

        if self._grid_is_native:
            vscroll = ttk.Scrollbar(tree_area, orient="vertical", command=self.tree.yview)
            hscroll = ttk.Scrollbar(tree_area, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
            vscroll.grid(row=0, column=1, sticky="ns")
            hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("removed", foreground=theme.TEXT_MUTED)
        self._bind_grid()

        self.column_menu = tk.Menu(self._menu_parent, tearoff=False)
        if self._theme_name_getter() == "dark":
            try:
                self.column_menu.configure(**theme.dark_menu_options())
            except Exception:
                pass
        for spec in OPTIONAL_COLUMNS:
            self.column_menu.add_checkbutton(
                label=self.tr(spec.title),
                variable=self._column_vars[spec.id],
                command=self._optional_columns_changed,
            )

        # Client-side paging is intentionally below the complete filtered+sorted
        # result.  query_runs no longer has a hidden 2000-row cap.
        page_bar = tk.Frame(table_box, bg=theme.SURFACE_APP)
        page_bar.pack(fill="x", pady=(4, 0))
        tk.Label(page_bar, text=self.tr("Rows/page"), bg=theme.SURFACE_APP,
                 fg=theme.TEXT_SECTION).pack(side="left", padx=(2, 4))
        self.page_size_var = tk.StringVar(value=str(DEFAULT_PAGE_SIZE))
        self.page_size_box = ttk.Combobox(
            page_bar, values=[str(n) for n in PAGE_SIZES], state="readonly", width=5,
            textvariable=self.page_size_var,
        )
        self.page_size_box.pack(side="left")
        self.page_size_box.bind("<<ComboboxSelected>>", self._page_size_changed)
        self.page_range_label = tk.Label(
            page_bar, text="", bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
        )
        self.page_range_label.pack(side="left", padx=(12, 0))

        self.show_removed_var = tk.BooleanVar(value=False)
        self.show_removed = tk.Checkbutton(
            page_bar, text=self.tr("Show removed"), variable=self.show_removed_var,
            command=self._show_removed_changed, bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
            selectcolor=theme.SURFACE_CONTROL_ALT, activebackground=theme.SURFACE_APP,
            activeforeground=theme.TEXT_PRIMARY,
        )
        self.show_removed.pack(side="left", padx=(16, 4))
        self.selected_label = tk.Label(
            page_bar, text=self.tr("Selected 0"), bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
        )
        self.selected_label.pack(side="left", padx=(8, 4))
        self.remove_button = tk.Button(
            page_bar, text=self.tr("Remove selected"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
            padx=7, pady=1, cursor="hand2", command=self.remove_selected, state="disabled",
        )
        self.remove_button.pack(side="left", padx=(2, 2))
        self.restore_button = tk.Button(
            page_bar, text=self.tr("Restore selected"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
            padx=7, pady=1, cursor="hand2", command=self.restore_selected, state="disabled",
        )
        self.restore_button.pack(side="left", padx=(2, 0))
        # Assign to group / Remove from group used to stand here, between
        # Restore selected and the pager.  They now open from the glyph in the
        # Group heading, next to the cells they change; the objects keep
        # existing so their enabled state still tracks the marks.
        holder = tk.Frame(page_bar)          # never packed: kept off the strip
        # The marks are already a bulk-selection mechanism; annotation reuses
        # them rather than introducing a second way to choose rows.
        self.set_group_button = tk.Button(
            holder, text=self.tr("Assign to group\u2026"), relief="flat",
            bg=theme.SURFACE_CONTROL_ALT, padx=7, pady=1, cursor="hand2",
            command=self.set_group_selected, state="disabled",
        )
        self.clear_group_button = tk.Button(
            holder, text=self.tr("Remove from group"), relief="flat",
            bg=theme.SURFACE_CONTROL_ALT, padx=7, pady=1, cursor="hand2",
            command=self.clear_group_selected, state="disabled",
        )

        self.page_buttons: list[tk.Button] = []
        for symbol, action in (
            ("»", self._page_last), ("›", self._page_next),
            ("‹", self._page_previous), ("«", self._page_first),
        ):
            button = tk.Button(
                page_bar, text=symbol, relief="flat", bg=theme.SURFACE_CONTROL_ALT,
                padx=7, pady=1, cursor="hand2", command=action,
            )
            button.pack(side="right", padx=(2, 0))
            self.page_buttons.append(button)
        self.page_number_label = tk.Label(
            page_bar, text="", bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION,
        )
        self.page_number_label.pack(side="right", padx=(0, 8))

        head = tk.Frame(detail_box, bg=theme.HISTORY_DETAIL_PANEL_BG)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(
            head,
            text=self.tr("Run details"),
            bg=theme.HISTORY_DETAIL_PANEL_BG,
            fg=theme.HISTORY_DETAIL_TITLE_TEXT,
            font=HISTORY_DETAIL_TITLE_FONT,
        ).pack(side="left")
        self.copy_button = tk.Button(
            head, text=self.tr("Copy details"), relief="flat", bg=theme.SURFACE_CONTROL_ALT,
            padx=10, pady=3, cursor="hand2", command=self.copy_details,
        )
        self.copy_button.pack(side="right")
        # Kept off the details bar: annotation is reached from the Group and
        # Comment headings, next to the cells it changes.  The object survives
        # because its enabled state still tracks the selection.
        annotate_holder = tk.Frame(head)
        self.annotate_button = tk.Button(
            annotate_holder, text=self.tr("Annotate\u2026"), relief="flat",
            bg=theme.SURFACE_CONTROL_ALT,
            padx=10, pady=3, cursor="hand2", command=self.edit_annotation,
        )


        detail_body = tk.Frame(detail_box, bg=theme.HISTORY_DETAIL_PANEL_BG)
        detail_body.pack(fill="both", expand=True)
        # Run details now own the full width under the table; the result panel
        # is a sibling of this whole column, added by toggle_preview().
        text_box = tk.Frame(detail_body, bg=theme.HISTORY_DETAIL_PANEL_BG)
        text_box.pack(fill="both", expand=True)
        self.detail = tk.Text(
            text_box,
            wrap="word",
            height=HISTORY_DETAIL_INITIAL_HEIGHT,
            font=HISTORY_DETAIL_FONT,
            bg=theme.HISTORY_DETAIL_BG,
            fg=theme.HISTORY_DETAIL_TEXT,
            insertbackground=theme.HISTORY_DETAIL_CURSOR,
            relief="flat",
            cursor="xterm",
        )
        detail_scroll = ttk.Scrollbar(text_box, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        self.preview_box = tk.Frame(self.detail_panes, bg=theme.HISTORY_DETAIL_BG)
        self.preview_title = tk.Label(
            self.preview_box, text="", bg=theme.HISTORY_DETAIL_BG,
            fg=theme.HISTORY_DETAIL_TEXT, font=HISTORY_DETAIL_FONT,
            anchor="w", justify="left", wraplength=HISTORY_PREVIEW_INITIAL_WIDTH,
        )
        self.preview_title.pack(fill="x", padx=6, pady=(6, 4))
        self.preview_holder = tk.Frame(self.preview_box, bg=theme.HISTORY_DETAIL_BG)
        self.preview_holder.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.preview_image = tk.Label(
            self.preview_holder, text="", bg=theme.HISTORY_DETAIL_BG,
            fg=theme.TEXT_SUBDUED, font=HISTORY_DETAIL_FONT,
            wraplength=HISTORY_PREVIEW_INITIAL_WIDTH, justify="left",
        )
        self.preview_image.pack(fill="both", expand=True)
        self.preview_holder.bind("<Configure>", self._preview_resized)
        self.detail.bind("<Button-3>", self._detail_context_menu)

        self.history_menu = tk.Menu(self._menu_parent, tearoff=False)
        self.history_menu.add_command(label=self.tr("Copy details"), command=self.copy_details)
        self.history_menu.add_command(label=self.tr("Copy prompt"), command=self.copy_prompt)
        self.history_menu.add_separator()
        self.history_menu.add_command(label="FULL Res", command=self.open_result)

        self._restore_ui_state()
        self._apply_display_columns()
        self._apply_zone_state()
        self.load_journal()
        self._after(0, lambda: self._restore_split_position(0))

    def _open_typography_lab(self) -> None:
        """Open the designer-only live font fitter against the real History grid.

        Normal History startup is unchanged: kit_font_system is imported lazily,
        the catalogue is read only after the button is pressed, and a missing or
        incompatible typography layer reports an error without touching the grid.
        A saved palette is applied at that explicit boundary so the Lab controls
        and the live table cannot begin in contradictory states.
        """
        if self._grid_is_native:
            messagebox.showinfo(
                "Typography Lab",
                "Typography Lab requires the tksheet HistoryGrid backend.",
                parent=self._menu_parent,
            )
            return

        # Reuse an already-open Lab instead of spawning competing designer panels.
        lab = self._typography_lab
        if lab is not None:
            try:
                if lab.top.winfo_exists():
                    lab.refresh()
                    lab.top.deiconify()
                    lab.top.lift()
                    return
            except Exception:
                pass
            self._typography_lab = None

        kit_root = Path(__file__).resolve().parent
        catalog_path = kit_root / "font-catalog" / "font_catalog.json"
        palette_path = kit_root / "font-catalog" / "font_palette.json"

        if not catalog_path.is_file():
            messagebox.showerror(
                "Typography Lab",
                f"Master font catalogue not found:\n{catalog_path}",
                parent=self._menu_parent,
            )
            return

        try:
            # Deliberately lazy: History remains independent of this development
            # layer unless the operator explicitly opens the Lab.
            import kit_font_system

            if self._font_system is None:
                fonts = kit_font_system.FontSystem.load(catalog_path, palette_path)
                if fonts.catalog.get("complete_for_designer_picks") is not True:
                    raise RuntimeError(
                        "font_catalog.json is not complete for designer picks"
                    )
                self._font_system = fonts

            # This is the first intentional product-grid mutation: replace any
            # stale live family overrides with the in-memory/saved palette, then
            # build the Lab against exactly that state.
            self._font_system.apply(self.tree)
            self._typography_lab = self._font_system.open_lab(
                self.tree, master=self.tab_history
            )
        except Exception as exc:  # noqa: BLE001 - development hook must fail soft
            self._typography_lab = None
            messagebox.showerror(
                "Typography Lab",
                f"{type(exc).__name__}: {exc}",
                parent=self._menu_parent,
            )

    def _show_migration_bar(self, needed: bool) -> None:
        """A journal from an earlier Kit is complete, readable and one step away.

        The notice used to live in the status line, where the next sync wrote
        over it - so the Panel went back to hiding the groups silently, which
        is the exact failure the notice existed to prevent.  It has its own
        strip now, and the strip carries the action, because a migration is a
        write and writes in this Kit follow from something the user pressed.
        """
        if not needed:
            self.migration_bar.pack_forget()
            return
        self.migration_bar.pack(fill="x", padx=6, pady=(0, 4), before=self.detail_panes)

    def migrate_journal(self) -> None:
        if not messagebox.askyesno(
            self.tr("Migrate this journal"),
            self.tr("Your groups and comments are safe on disk; this adds the "
                    "tables this Kit needs to store criteria and ratings.\n\n"
                    "Save a copy first if you have not already."),
            parent=self._menu_parent,
        ):
            return
        try:
            outcome = kit_history_mutate.migrate_extension(self._history_db)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(self.tr("Migration failed"), str(exc),
                                 parent=self._menu_parent)
            return
        self.load_journal()
        self.refresh_state.configure(text=str(outcome))

    def _bind_grid(self) -> None:
        """One place where the two backends stop looking alike."""
        if self._grid_is_native:
            self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
            self.tree.bind("<Button-1>", self._tree_left_click, add="+")
            self.tree.bind("<Double-1>", self._body_double_click)
            self.tree.bind("<Button-3>", self._heading_context_menu, add="+")
            self.tree.bind("<Shift-MouseWheel>", self._shift_mousewheel, add="+")
            return
        # Everything that is NOT a column - the slab beyond the last one, the
        # index, the grid lines - is painted from the Panel's own tokens, or a
        # white default would sit beside a dark table.
        self.tree.set_base_colours(
            table_bg=str(theme.HISTORY_TABLE_BG),
            table_fg=str(theme.HISTORY_TABLE_TEXT),
            table_grid_fg=str(theme.OUTLINE_PANEL),
            header_bg=str(theme.HISTORY_HEADER_BG),
            header_fg=str(theme.HISTORY_HEADER_TEXT),
            header_grid_fg=str(theme.OUTLINE_PANEL),
            index_bg=str(theme.HISTORY_HEADER_BG),
            index_fg=str(theme.HISTORY_HEADER_TEXT),
            index_grid_fg=str(theme.OUTLINE_PANEL),
            top_left_bg=str(theme.HISTORY_HEADER_BG),
            top_left_fg=str(theme.HISTORY_HEADER_TEXT),
            table_selected_cells_bg=str(theme.HISTORY_TABLE_SELECTED_BG),
            table_selected_cells_fg=str(theme.HISTORY_TABLE_SELECTED_TEXT),
            table_selected_rows_bg=str(theme.HISTORY_TABLE_SELECTED_BG),
            table_selected_rows_fg=str(theme.HISTORY_TABLE_SELECTED_TEXT),
            outline_thickness=0,
        )
        self.tree.set_zones(
            {spec.id: zone_of(spec.id) for spec in COLUMN_SPECS},
            zone_palette(), header_palette=zone_header_palette(),
        )
        self.tree.set_hover_foreground(str(theme.HISTORY_HOVER_TEXT))
        self.tree.on_resize(lambda _e: self.tree.stretch_last_column())
        self.tree.on_select(self._selection_changed)
        self.tree.on_body_click(self._tree_left_click)
        self.tree.on_body_double_click(self._body_double_click)
        self.tree.on_body_right_click(self._rating_context_menu)
        self.tree.on_header_right_click(self._heading_context_menu)
        self.tree.on_motion(self._row_hover)
        self.tree.on_leave(lambda _e: (self.tree.hover(None), self._hide_hint()))
        self.tree.bind("<Shift-MouseWheel>", self._shift_mousewheel)

    def _row_hover(self, event) -> None:
        """Mark the row under the cursor by its text, never by a row fill."""
        if self._grid_is_native:
            return
        self.tree.hover(self.tree.row_at(event))
        self._schedule_hint(event)

    # -- the user's two columns ---------------------------------------------
    def _annotation_menu(self, event) -> None:
        """Actions for Group and Comment, where those columns actually are.

        These used to sit in the bottom maintenance strip among Remove and
        Restore, which is the wrong neighbourhood for the user's own notes and
        far from the cells they act on.
        """
        menu = tk.Menu(self._menu_parent, tearoff=0)
        if self._theme_name_getter() == "dark":
            menu.configure(**theme.dark_menu_options())
        marked = len(self._marked_rows())
        menu.add_command(
            label=self.tr("Assign to group\u2026"), command=self.set_group_selected,
            state="normal" if marked else "disabled",
        )
        menu.add_command(
            label=self.tr("Remove from group"), command=self.clear_group_selected,
            state="normal" if marked else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=self.tr("Annotate\u2026"), command=self.edit_annotation,
            state="normal" if self._current_run_id() is not None else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label=self.tr("Collapse these columns"), command=self.toggle_annotation_zone,
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def toggle_annotation_zone(self) -> None:
        """Group, Comment and evaluation move together without rebuilding rows.

        This is a pure VIEW transition.  Recreating all tksheet rows here made
        a simple accordion click feel like a 2005-era round trip (~0.5-0.8 s on
        the live journal) even though no data had changed.  The hidden columns
        already carry their values, so displaycolumns/widths/headings are all
        that needs to move.
        """
        if self._zone_expanded:
            self._remember_zone_widths()
        self._zone_expanded = not self._zone_expanded
        self._apply_zone_state()

    def _remember_zone_widths(self) -> None:
        """Keep the width the USER worked at, not the measured minimum.

        Someone who widened Comment to 260px and collapsed the zone expects
        260px back, not whatever the font happens to measure.
        """
        if self._grid_is_native:
            return
        live = self.tree.current_widths()
        for column_id in HISTORY_ANNOTATION_COLUMNS:
            width = live.get(column_id)
            if width and width > HISTORY_ANNOTATION_COLLAPSED:
                self._zone_widths[column_id] = int(width)

    def _apply_zone_state(self) -> None:
        """Widths, heading glyph and header action, from one flag.

        Collapsed, the whole heading expands the zone: there is no room to draw
        the glyph, and a hotspot that cannot be seen is not an affordance - it
        is something the user has to find by guessing.
        """
        if self._grid_is_native:
            return
        widths: dict[str, int] = {}
        for column_id in HISTORY_ANNOTATION_COLUMNS:
            spec = COLUMN_BY_ID.get(column_id)
            if spec is None:
                continue
            if not self._zone_expanded:
                widths[column_id] = HISTORY_ANNOTATION_COLLAPSED
            else:
                widths[column_id] = self._zone_widths.get(
                    column_id, max(spec.width, HISTORY_ANNOTATION_WORKING.get(column_id, spec.width))
                )
        self.tree.set_widths(widths)
        for column_id in HISTORY_ANNOTATION_COLUMNS:
            if self._zone_expanded:
                self.tree.set_header_menu(column_id, self._annotation_menu,
                                          glyph_only=True, glyph=HEADER_MENU_GLYPH)
            else:
                self.tree.set_header_menu(
                    column_id, lambda _e: self.toggle_annotation_zone(), glyph_only=False
                )
        self._apply_display_columns()
        self._refresh_evaluation_zones()

    # -- evaluation / Super-Accordion ---------------------------------------
    def _active_group_name(self) -> str:
        """A criterion has meaning only when the Group filter names one Group."""
        if not hasattr(self, "f_label"):
            return ""
        chosen = str(self.f_label.get() or "").strip()
        return "" if chosen in {ALL, NO_LABEL} else chosen

    def _sync_evaluation_context(self) -> None:
        """Load criteria/ratings for the one Group the user is examining."""
        self._active_eval_group = None
        self._criteria = []
        self._criterion_by_column = {}
        self._ratings = {}
        for row in self._rows:
            for slot in range(HISTORY_CRITERIA_MAX):
                row.pop(f"_rating_slot_{slot}", None)

        if (self._grid_is_native or self._handle is None or not self._handle.available
                or self._handle.annotation_state != kit_history.ANNOTATION_READY):
            self._refresh_evaluation_zones()
            return
        name = self._active_group_name()
        if not name:
            self._refresh_evaluation_zones()
            return
        wanted = kit_history.group_key(name)
        group = next(
            (g for g in kit_history.all_groups(self._handle)
             if kit_history.group_key(g.get("name")) == wanted),
            None,
        )
        if group is None or group.get("group_id") is None:
            self._refresh_evaluation_zones()
            return
        self._active_eval_group = group
        gid = int(group["group_id"])
        self._criteria = kit_history.criteria_for_group(self._handle.connection, gid)
        self._ratings = kit_history.ratings_for_group(self._handle.connection, gid)
        for slot, criterion in enumerate(self._criteria[:HISTORY_CRITERIA_MAX]):
            column_id = EVALUATION_SLOT_COLUMNS[slot]
            self._criterion_by_column[column_id] = criterion
            cid = int(criterion["criterion_id"])
            for row in self._rows:
                try:
                    run_id = int(row.get("run_id"))
                except (TypeError, ValueError):
                    continue
                value = self._ratings.get((run_id, cid))
                if value is not None:
                    row[f"_rating_slot_{slot}"] = value
        self._refresh_evaluation_zones()

    def _refresh_evaluation_zones(self) -> None:
        if self._grid_is_native or not hasattr(self, "tree"):
            return
        zones = {spec.id: zone_of(spec.id) for spec in COLUMN_SPECS}
        for column_id, criterion in self._criterion_by_column.items():
            zones[column_id] = (
                "issue" if criterion.get("kind") == kit_history.CRITERION_ISSUE else "quality"
            )
        self.tree.set_zones(zones, zone_palette(), header_palette=zone_header_palette())

    def _criterion_for_column(self, column_id: str | None) -> dict[str, Any] | None:
        return self._criterion_by_column.get(str(column_id or ""))

    def _criterion_cell_text(self, slot: int, row: dict[str, Any]) -> str:
        if not (0 <= slot < HISTORY_CRITERIA_MAX):
            return ""
        criterion = self._criterion_by_column.get(EVALUATION_SLOT_COLUMNS[slot])
        if criterion is None:
            return ""
        value = row.get(f"_rating_slot_{slot}")
        if value is None:
            return ""
        try:
            score = max(kit_history.RATING_MIN, min(kit_history.RATING_MAX, int(value)))
        except (TypeError, ValueError):
            return ""
        if criterion.get("kind") == kit_history.CRITERION_ISSUE:
            return ISSUE_GLYPH_FULL * score + ISSUE_GLYPH_EMPTY * (5 - score)
        return QUALITY_GLYPH_FULL * score + QUALITY_GLYPH_EMPTY * (5 - score)

    def _criterion_slot(self, column_id: str) -> int | None:
        try:
            return EVALUATION_SLOT_COLUMNS.index(column_id)
        except ValueError:
            return None

    def _rating_from_event(self, event, column_id: str) -> int | None:
        """Return the visible fixed slot clicked, or None outside the control.

        Rating glyphs are now five independent Canvas text items at fixed slot
        centres.  The grid asks the same vendored geometry used by the renderer,
        so Unicode ink/advance/fallback metrics cannot move the ordinal target.
        Empty side margins are deliberately NOT rating targets: clicking there
        may select the row, but must never write to the ratings table.
        """
        value = self.tree.segment_at(event, column_id)
        if value is None:
            return None
        return max(kit_history.RATING_MIN, min(kit_history.RATING_MAX, int(value)))

    def add_criterion(self) -> None:
        group = self._active_eval_group
        if group is None:
            messagebox.showinfo(
                self.tr("Add criterion"),
                self.tr("Choose one Group in the Group filter before adding criteria."),
                parent=self.tab_history,
            )
            return
        if len(self._criteria) >= HISTORY_CRITERIA_MAX:
            messagebox.showinfo(
                self.tr("Add criterion"),
                self.tr("This Group already has the maximum of {n} criteria.").format(
                    n=HISTORY_CRITERIA_MAX
                ),
                parent=self.tab_history,
            )
            return
        suggestions = (
            kit_history.distinct_criteria_names(self._handle)
            if self._handle is not None else []
        )
        chosen = _ask_criterion(
            self.tab_history, tr=self.tr, suggestions=suggestions,
            dark=self._theme_name_getter() == "dark",
        )
        if chosen is None:
            return
        name, kind = chosen
        focus = self._current_run_id()
        try:
            result = kit_history_mutate.add_criterion(
                self._history_db, group["name"], name, kind
            )
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(self.tr("Add criterion"), exc)
            return
        if result.changed == 0:
            messagebox.showinfo(
                self.tr("Add criterion"),
                self.tr("That criterion already exists in this Group."),
                parent=self.tab_history,
            )
            return
        self._after_evaluation(result.changed, focus=focus)

    def _after_evaluation(self, changed: int, focus: int | None = None) -> None:
        """Refresh only the evaluation layer; do not rebuild the whole tree.

        A criterion/rating write does not change the History run set.  Calling
        ``load_journal()`` here used to clear and recreate every tree parent
        with ``open=False``, so adding one criterion visibly collapsed the
        user's current rollout.  Reopen the read-only handle to observe the
        committed write, then refresh criteria, headings, display columns and
        the already-existing run rows in place.
        """
        try:
            if self._handle is not None:
                self._handle.close()
        except Exception:
            pass
        self._handle = kit_history.open_db(self._history_db)
        if not self._handle.available:
            # A genuine journal failure still falls back to the full safe path.
            self.load_journal()
            return
        self._sessions = kit_history.session_index(self._handle)
        self._sync_evaluation_context()
        self._apply_display_columns()
        for iid, row in tuple(self._by_iid.items()):
            try:
                if self.tree.exists(iid):
                    self.tree.item(iid, values=self._row_values(row))
            except Exception:
                pass
        self._refresh_headings()
        if focus is not None:
            iid = str(focus)
            try:
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
            except Exception:
                pass
        self.refresh_state.configure(
            text=self.tr("Evaluation updated · {n} change(s)").format(n=changed)
        )

    def _set_rating(self, iid: str, row: dict[str, Any], column_id: str, value: int | None) -> None:
        criterion = self._criterion_for_column(column_id)
        slot = self._criterion_slot(column_id)
        if criterion is None or slot is None:
            return
        run_id = int(row["run_id"])
        cid = int(criterion["criterion_id"])
        try:
            result = kit_history_mutate.set_rating(self._history_db, run_id, cid, value)
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(self.tr("Rating"), exc)
            return
        if not result.matched:
            self.refresh_state.configure(
                text=self.tr("Rating refused: this run is no longer in the active Group.")
            )
            return
        key = (run_id, cid)
        if value is None:
            self._ratings.pop(key, None)
            row.pop(f"_rating_slot_{slot}", None)
        else:
            score = int(value)
            self._ratings[key] = score
            row[f"_rating_slot_{slot}"] = score
        self.tree.item(iid, values=self._row_values(row))
        self.refresh_state.configure(
            text=(
                self.tr("Rating cleared · {name}").format(name=criterion["name"])
                if value is None else
                self.tr("Rating updated · {name}: {value}/5").format(
                    name=criterion["name"], value=int(value)
                )
            )
        )

    def _rating_context_menu(self, event):
        column_id = self._column_at(event)
        criterion = self._criterion_for_column(column_id)
        iid = self._iid_at(event)
        row = self._by_iid.get(iid) if iid else None
        if criterion is None or row is None or iid is None:
            return None
        slot = self._criterion_slot(str(column_id))
        current = row.get(f"_rating_slot_{slot}") if slot is not None else None
        menu = tk.Menu(self._menu_parent, tearoff=False)
        if self._theme_name_getter() == "dark":
            menu.configure(**theme.dark_menu_options())
        menu.add_command(
            label=self.tr("Clear rating"),
            state="normal" if current is not None else "disabled",
            command=lambda: self._set_rating(iid, row, str(column_id), None),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _criterion_header_menu(self, event, column_id: str) -> str:
        criterion = self._criterion_for_column(column_id)
        group = self._active_eval_group
        if criterion is None or group is None:
            return "break"
        menu = tk.Menu(self._menu_parent, tearoff=False)
        if self._theme_name_getter() == "dark":
            menu.configure(**theme.dark_menu_options())
        menu.add_command(
            label=self.tr("Rename criterion…"),
            command=lambda: self._rename_criterion(criterion),
        )
        menu.add_command(
            label=self.tr("Delete criterion…"),
            command=lambda: self._delete_criterion(criterion),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _rename_criterion(self, criterion: dict[str, Any]) -> None:
        group = self._active_eval_group
        if group is None:
            return
        wanted = simpledialog.askstring(
            self.tr("Rename criterion"), self.tr("Criterion name"),
            initialvalue=str(criterion.get("name") or ""), parent=self.tab_history,
        )
        if wanted is None or not wanted.strip():
            return
        focus = self._current_run_id()
        try:
            result = kit_history_mutate.rename_criterion(
                self._history_db, group["name"], criterion["name"], wanted.strip()
            )
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(self.tr("Rename criterion"), exc)
            return
        if result.changed:
            self._after_evaluation(result.changed, focus=focus)

    def _delete_criterion(self, criterion: dict[str, Any]) -> None:
        group = self._active_eval_group
        if group is None:
            return
        focus = self._current_run_id()
        try:
            result = kit_history_mutate.delete_criterion(
                self._history_db, group["name"], criterion["name"]
            )
        except kit_history_mutate.RatedGroupError as exc:
            if not messagebox.askyesno(
                self.tr("Delete criterion"),
                self.tr("This criterion has {n} rating(s). Delete the criterion and those ratings?").format(
                    n=exc.ratings
                ),
                parent=self.tab_history,
            ):
                return
            try:
                result = kit_history_mutate.delete_criterion(
                    self._history_db, group["name"], criterion["name"], force=True
                )
            except Exception as inner:  # noqa: BLE001
                self._report_write_failure(self.tr("Delete criterion"), inner)
                return
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(self.tr("Delete criterion"), exc)
            return
        if result.changed:
            self._after_evaluation(result.changed, focus=focus)

    # -- flash hints ---------------------------------------------------------
    def _schedule_hint(self, event) -> None:
        """Full text of a Group description or a comment, on hover.

        Only over these two columns: a hint that follows the cursor across
        Date and Seed is noise.  The delay is on the way IN, so crossing the
        column on the way somewhere else never opens anything.
        """
        column_id = self._column_at(event)
        iid = self._iid_at(event) if column_id in HISTORY_ANNOTATION_COLUMNS else None
        key = (column_id or "", iid or "")
        if key == self._hint_key:
            return
        self._hide_hint()
        self._hint_key = key
        if not iid or self._by_iid.get(iid) is None:
            return
        x_root, y_root = event.x_root, event.y_root
        self._hint_job = self._after(
            HISTORY_HINT_DELAY_MS, lambda: self._show_hint(column_id, iid, x_root, y_root)
        )

    def _hint_text(self, column_id: str, row: dict[str, Any]) -> str:
        if column_id == "note":
            return str(row.get("note") or "").strip()
        name = str(row.get("label") or "").strip()
        about = str(row.get("group_about") or "").strip()
        return "\n".join(part for part in (name, about) if part)

    def _show_hint(self, column_id: str, iid: str, x_root: int, y_root: int) -> None:
        self._hint_job = None
        row = self._by_iid.get(iid)
        if row is None:
            return
        text = self._hint_text(column_id, row)
        if not text:
            return
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            lines.extend(textwrap.wrap(paragraph, HISTORY_HINT_WIDTH) or [""])
            if len(lines) >= HISTORY_HINT_LINES:
                break
        if len(lines) > HISTORY_HINT_LINES:
            lines = lines[:HISTORY_HINT_LINES]
            lines[-1] = lines[-1].rstrip() + " \u2026"
        zone = "comment" if column_id == "note" else "group"
        bg, fg, edge = flash_palette()[zone]
        hint = tk.Toplevel(self._menu_parent)
        hint.wm_overrideredirect(True)
        hint.wm_geometry(f"+{x_root + 16}+{y_root + 18}")
        frame = tk.Frame(hint, bg=edge)          # the edge is the zone's own colour
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text="\n".join(lines), justify="left", anchor="w",
            bg=bg, fg=fg, bd=0, padx=9, pady=7,
            font=HISTORY_TABLE_FONT, wraplength=520,
        ).pack(padx=1, pady=1)
        self._hint = hint

    def _hide_hint(self) -> None:
        if self._hint_job is not None:
            try:
                self.tab_history.after_cancel(self._hint_job)
            except Exception:
                pass
            self._hint_job = None
        if self._hint is not None:
            try:
                self._hint.destroy()
            except Exception:
                pass
            self._hint = None

    def _column_at(self, event) -> str | None:
        """Which column id the pointer is over, or None outside a cell."""
        if not self._grid_is_native:
            return self.tree.column_at(event)
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        column = self.tree.identify_column(event.x)
        visible = tuple(self.tree.cget("displaycolumns"))
        try:
            return visible[int(column[1:]) - 1]
        except Exception:
            return None

    def _iid_at(self, event) -> str | None:
        if not self._grid_is_native:
            return self.tree.iid_at(event)
        return self.tree.identify_row(event.y) or None

    def _filter(self, parent: tk.Widget, label: str, *, width: int = 16) -> ttk.Combobox:
        tk.Label(parent, text=self.tr(label), bg=theme.SURFACE_APP, fg=theme.TEXT_SECTION).pack(
            side="left", padx=(8, 4)
        )
        box = ttk.Combobox(parent, values=[ALL], state="readonly", width=width)
        box.set(ALL)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())
        return box

    def _configure_tree_style(self) -> None:
        """Treeview is new to this panel, so give it the panel's own tokens."""
        style = ttk.Style(self.tab_history)
        # SURFACE_CODE intentionally stays dark even in the light theme; that
        # is useful for code/detail panes but made Treeview rows dark-on-dark.
        # A data table belongs to the application surface instead.
        style.configure(
            "History.Treeview",
            background=theme.HISTORY_TABLE_BG,
            fieldbackground=theme.HISTORY_TABLE_BG,
            foreground=theme.HISTORY_TABLE_TEXT,
            font=HISTORY_TABLE_FONT,
            rowheight=HISTORY_TABLE_ROW_HEIGHT,
            borderwidth=0,
        )
        style.configure(
            "History.Treeview.Heading",
            background=theme.HISTORY_HEADER_BG,
            foreground=theme.HISTORY_HEADER_TEXT,
            font=HISTORY_HEADER_FONT,
            relief="flat",
        )
        style.map(
            "History.Treeview",
            background=[("selected", theme.HISTORY_TABLE_SELECTED_BG)],
            foreground=[("selected", theme.HISTORY_TABLE_SELECTED_TEXT)],
        )

    # -- data --------------------------------------------------------------
    def load_journal(self) -> None:
        self.dispose()
        self._handle = kit_history.open_db(self._history_db)
        if not self._handle.available:
            self._clear_table()
            self.count_label.configure(text=self.tr("Journal unavailable"))
            self._show_detail(self._handle.message)
            return
        self._show_migration_bar(
            self._handle.annotation_state == kit_history.ANNOTATION_OUTDATED
        )
        self._sessions = kit_history.session_index(self._handle)
        if not self.from_var.get():
            span = kit_history.journal_span(self._handle)
            if span:
                self.from_var.set(span[0])
                self.to_var.set(span[1])
        for box, logical in ((self.f_status, "status"), (self.f_model, "model_name"), (self.f_recipe, "recipe")):
            values = [ALL] + kit_history.distinct_values(
                self._handle, logical, include_excluded=bool(self.show_removed_var.get())
            )
            box.configure(values=values)
            if box.get() not in values:
                box.set(ALL)
        label_values = [ALL, NO_LABEL] + kit_history.distinct_labels(
            self._handle, include_excluded=bool(self.show_removed_var.get())
        )
        self.f_label.configure(values=label_values)
        if self.f_label.get() not in label_values:
            self.f_label.set(ALL)
        # Sync/reload should not throw a user from page 5 back to page 1. User
        # filter/sort changes do reset the page explicitly.
        self.apply_filters(reset_page=False)

    def apply_filters(self, *, reset_page: bool = True) -> None:
        if self._handle is None or not self._handle.available:
            return
        pick = lambda box: (None if box.get() == ALL else box.get())  # noqa: E731
        since = until = None
        if self.f_period.get() != self.tr("All"):
            bounds = kit_history.day_bounds_utc(self.from_var.get(), self.to_var.get())
            if bounds is None:
                self.count_label.configure(text=self.tr("Dates must be YYYY-MM-DD"))
                return
            since, until = bounds
        self._rows = kit_history.query_runs(
            self._handle,
            status=pick(self.f_status),
            model=pick(self.f_model),
            recipe=pick(self.f_recipe),
            search=(self.search_var.get().strip() or None),
            since=since,
            until=until,
            include_excluded=bool(self.show_removed_var.get()),
            limit=None,
        )
        # Labels live in the annotation extension, not in runs, so the label
        # filter narrows the already-materialised result instead of adding a
        # parameter to the collector-facing query.
        chosen_label = self.f_label.get()
        if chosen_label == NO_LABEL:
            self._rows = [r for r in self._rows if not str(r.get("label") or "").strip()]
        elif chosen_label != ALL:
            self._rows = [r for r in self._rows if str(r.get("label") or "") == chosen_label]

        self._sync_evaluation_context()
        self._apply_display_columns()

        if reset_page:
            self._clear_marks()
            self._page = 1
        self._fill_table()

    def clear_filters(self) -> None:
        for box in (self.f_status, self.f_model, self.f_recipe, self.f_label):
            box.set(ALL)
        self.search_var.set("")
        self.show_removed_var.set(False)
        self._clear_marks()
        self.f_group.set(NO_GROUP)
        self.f_period.set(self.tr("All"))
        span = kit_history.journal_span(self._handle) if self._handle else None
        if span:
            self.from_var.set(span[0])
            self.to_var.set(span[1])
        self.apply_filters()

    def _period_chosen(self) -> None:
        chosen = self.f_period.get()
        for label, days in PERIODS:
            if chosen != self.tr(label):
                continue
            if days is None:
                span = kit_history.journal_span(self._handle) if self._handle else None
                if span:
                    self.from_var.set(span[0])
                    self.to_var.set(span[1])
            else:
                self.from_var.set(kit_history.days_ago_local(days))
                self.to_var.set(kit_history.today_local())
            self.apply_filters()
            return
        self.apply_filters()

    def _custom_dates(self) -> None:
        """Typing a date means the range is no longer one of the presets."""
        if self.f_period.get() != self.tr(CUSTOM):
            self.f_period.set(self.tr(CUSTOM))
        self.apply_filters()

    def save_journal_copy(self) -> None:
        """Write a consistent copy through SQLite's own backup API."""
        if not self._history_db.is_file():
            messagebox.showinfo(self.tr("Save a copy\u2026"), self.tr("There is no journal to copy yet."))
            return
        target = filedialog.asksaveasfilename(
            parent=self.tab_history,
            title=self.tr("Save a copy of the journal"),
            defaultextension=".sqlite",
            initialfile=f"kit_history_{kit_history.today_local()}.sqlite",
            filetypes=[("SQLite journal", "*.sqlite"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            source = sqlite3.connect(self._history_db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(self.tr("Save a copy\u2026"), f"{type(exc).__name__}: {exc}")
            return
        messagebox.showinfo(
            self.tr("Save a copy\u2026"),
            self.tr("Journal copied to:") + f"\n\n{target}",
        )

    def refresh_journal(self) -> None:
        """Collect, then reload.  Collection is the injected port's job."""
        if self._collect is None:
            messagebox.showinfo(
                self.tr("Sync now"),
                self.tr(
                    "The journal collector is not installed with this Kit build.\n\n"
                    "The tab is showing the journal as it was last collected."
                ),
            )
            return
        before = (self._handle.counts.get("runs", 0) if self._handle and self._handle.available else 0)
        before_sha = kit_history.journal_sha256(self._history_db)
        self.refresh_button.configure(state="disabled")
        self.refresh_state.configure(text=self.tr("Syncing \u2026"))
        self._show_detail(self.tr("Collecting journal \u2026"))
        self._update_idletasks()
        try:
            rc, text = self._collect()
        except Exception as exc:  # noqa: BLE001
            rc, text = -1, f"{type(exc).__name__}: {exc}"
        finally:
            self.refresh_button.configure(state="normal")
        self.load_journal()

        if rc != 0:
            # The collector output stays out of the status line and goes where
            # there is room for it.
            self.refresh_state.configure(text=self.tr("Sync failed"))
            self._show_detail(
                self.tr("The journal collector reported a problem.")
                + f"\n\n[exit code {rc}]\n\n{text}"
            )
            return

        after = self._handle.counts.get("runs", 0) if self._handle and self._handle.available else 0
        after_sha = kit_history.journal_sha256(self._history_db)
        added = after - before
        changed = before_sha != after_sha
        if added > 0:
            self.refresh_state.configure(
                text=self.tr("Updated \u00b7 +{n} \u00b7 {total} runs").format(n=added, total=after)
            )
        elif changed:
            # A real History update can enrich an existing run (for example
            # pending -> exact recipe after EXITED) without adding a row.
            self.refresh_state.configure(
                text=self.tr("Updated \u00b7 {total} runs").format(total=after)
            )
        else:
            self.refresh_state.configure(
                text=self.tr("Up to date \u00b7 {total} runs").format(total=after)
            )

    # -- table -------------------------------------------------------------
    def _clear_table(self) -> None:
        self._by_iid.clear()
        self._groups.clear()
        self._selected_detail = None
        # Filtering, paging or a refresh drops the selection, and the
        # preview must go with it: an image left beside "Select a run"
        # silently attributes an old result to the current table.
        if self._preview_job is not None:
            try:
                self.tab_history.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None
        self._preview_run = None
        if getattr(self, "preview_var", None) is not None and self.preview_var.get():
            self._show_preview(None, self.tr("Select a run to see its result."))
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _grouping(self) -> tuple[str, str] | None:
        chosen = self.f_group.get()
        for label, logical in GROUPINGS:
            if chosen == self.tr(label):
                return label, logical
        return None

    def _status_display(self, value: Any) -> str:
        raw = str(value or "unknown").strip()
        pretty = raw.replace("_", " ").replace("-", " ").capitalize()
        return self.tr(pretty)

    def _recipe_display(self, row: dict[str, Any]) -> str:
        recipe = row.get("recipe")
        if recipe:
            suffix = "*" if row.get("session_end_quality") == "bounded" else ""
            return str(recipe) + suffix
        state = row.get("_state") or kit_history.UNRECORDED
        return self.tr(_RECIPE_LABEL.get(state, "not recorded"))

    def _row_values(self, row: dict[str, Any]) -> tuple[Any, ...]:
        values = tuple(spec.getter(self, row) for spec in COLUMN_SPECS)
        # Positional drift here would silently put values under the wrong heading.
        # Keep the failure loud even in optimized/user builds.
        if len(values) != len(COLUMN_SPECS):
            raise RuntimeError("History column contract mismatch")
        return values

    def _group_row_values(self, summary: dict[str, Any]) -> tuple[Any, ...]:
        average = (
            self.tr("avg {t}").format(t=kit_history.format_seconds(summary["average"]))
            if summary["average"] is not None else ""
        )
        # The average used to live under Failure stage.  That column is optional
        # now, so it moves to whichever wide column is actually on screen rather
        # than disappearing with it.
        visible = set(self._visible_column_ids())
        average_column = "failure" if "failure" in visible else "prompt"
        mapped = {
            "status": self.tr("{n} runs").format(n=summary["runs"]),
            "model": self._counts_text(summary),
            "time": kit_history.format_duration(summary["completed_seconds"]),
            average_column: average,
        }
        return tuple(mapped.get(spec.id, "") for spec in COLUMN_SPECS)

    def _visible_column_ids(self) -> tuple[str, ...]:
        optional = {
            spec.id for spec in OPTIONAL_COLUMNS
            if self._column_vars.get(spec.id) is not None and self._column_vars[spec.id].get()
        }
        accordion = bool(self._zone_expanded and not self._grid_is_native)
        evaluation = bool(accordion and self._active_eval_group is not None)
        active_slots = set(
            EVALUATION_SLOT_COLUMNS[:min(len(self._criteria), HISTORY_CRITERIA_MAX)]
        )
        visible: list[str] = []
        for spec in COLUMN_SPECS:
            if spec.id == EVALUATION_PLUS_COLUMN:
                # The + is the discoverability affordance of the accordion.
                # Keep it visible whenever the annotation zone is expanded; if
                # no single Group is active, add_criterion() explains what the
                # user must choose instead of making the feature disappear.
                if accordion:
                    visible.append(spec.id)
                continue
            if spec.id in EVALUATION_SLOT_COLUMNS:
                if evaluation and spec.id in active_slots:
                    visible.append(spec.id)
                continue
            if spec.default_visible or spec.id in optional:
                visible.append(spec.id)
        return tuple(visible)

    def _apply_display_columns(self) -> None:
        visible = self._visible_column_ids()
        self.tree.configure(displaycolumns=visible)
        if self._sort_column not in visible:
            self._sort_column, self._sort_desc = "date", True
        self._refresh_headings()

    def _optional_columns_changed(self) -> None:
        self._apply_display_columns()
        self._fill_table()

    def _pick_heading_text(self) -> str:
        visible = {int(r["run_id"]) for r in self._by_iid.values() if r.get("run_id") is not None}
        return "☑" if visible and visible.issubset(self._marked_run_ids) else "☐"

    def _update_mark_controls(self) -> None:
        rows = [r for r in self._rows if int(r.get("run_id") or -1) in self._marked_run_ids]
        active = sum(r.get("excluded_at") in (None, "") for r in rows)
        removed = len(rows) - active
        self.selected_label.configure(text=self.tr("Selected {n}").format(n=len(rows)))
        self.remove_button.configure(state=("normal" if active else "disabled"))
        self.restore_button.configure(state=("normal" if removed else "disabled"))
        marked = "normal" if rows else "disabled"
        for button in ("set_group_button", "clear_group_button"):
            widget = getattr(self, button, None)
            if widget is not None:
                widget.configure(state=marked)
        self._refresh_headings()

    def _clear_marks(self) -> None:
        self._marked_run_ids.clear()
        if hasattr(self, "selected_label"):
            self._update_mark_controls()

    def _body_double_click(self, event):
        """Open FULL Res only from non-interactive History cells.

        A user correcting a star often produces a double-click while searching
        for the target.  Opening the external viewer from that gesture is a
        surprising side effect and makes the hitbox bug feel even worse.
        Evaluation, annotation, the + plate and the mark checkbox therefore
        consume a double-click; ordinary provenance cells keep the convenient
        FULL Res shortcut.
        """
        column_id = self._column_at(event)
        if (column_id == "pick" or column_id in HISTORY_ANNOTATION_COLUMNS
                or column_id in EVALUATION_COLUMN_IDS):
            return "break"
        self.open_result()
        return "break"

    def _tree_left_click(self, event):
        column_id = self._column_at(event)
        iid = self._iid_at(event)
        row = self._by_iid.get(iid) if iid else None

        criterion = self._criterion_for_column(column_id)
        if criterion is not None and row is not None and iid is not None:
            value = self._rating_from_event(event, str(column_id))
            if value is not None:
                self._set_rating(iid, row, str(column_id), value)
            # Do not break: tksheet still selects the row, so Preview and Run
            # details follow the inference.  A click in the empty side margin
            # selects only; it is not silently converted to rating 1 or 5.
            return None

        if column_id != "pick":
            return None
        if row is None:
            return "break"
        run_id = int(row["run_id"])
        if run_id in self._marked_run_ids:
            self._marked_run_ids.remove(run_id)
        else:
            self._marked_run_ids.add(run_id)
        self.tree.item(iid, values=self._row_values(row))
        self._update_mark_controls()
        return "break"

    def _toggle_page_marks(self) -> None:
        visible = {int(r["run_id"]) for r in self._by_iid.values() if r.get("run_id") is not None}
        if not visible:
            return
        if visible.issubset(self._marked_run_ids):
            self._marked_run_ids.difference_update(visible)
        else:
            self._marked_run_ids.update(visible)
        for iid, row in self._by_iid.items():
            self.tree.item(iid, values=self._row_values(row))
        self._update_mark_controls()

    def _show_removed_changed(self) -> None:
        self._clear_marks()
        self._page = 1
        self.load_journal()

    def _mutate_marked(self, *, excluded: bool) -> None:
        eligible = [
            int(r["run_id"]) for r in self._rows
            if int(r.get("run_id") or -1) in self._marked_run_ids
            and ((r.get("excluded_at") in (None, "")) if excluded else (r.get("excluded_at") not in (None, "")))
        ]
        if not eligible:
            return
        verb = self.tr("Remove from History") if excluded else self.tr("Restore to History")
        if not messagebox.askyesno(
            verb,
            (
                self.tr("Remove {n} selected run(s) from the normal History view?").format(n=len(eligible))
                + "\n\n"
                + self.tr("InvokeAI source records and result files are not modified. Removed runs can be restored with Show removed.")
            )
            if excluded
            else self.tr("Restore {n} selected run(s) to the normal History view?").format(n=len(eligible)),
            parent=self.tab_history,
        ):
            return
        try:
            result = kit_history_mutate.set_excluded(self._history_db, eligible, excluded=excluded)
        except Exception as exc:
            messagebox.showerror(verb, f"{type(exc).__name__}: {exc}", parent=self.tab_history)
            return
        self._clear_marks()
        self.load_journal()
        self.refresh_state.configure(
            text=self.tr("History view updated · {n} run(s)").format(n=result.changed)
        )

    def remove_selected(self) -> None:
        self._mutate_marked(excluded=True)

    def restore_selected(self) -> None:
        self._mutate_marked(excluded=False)

    def _marked_rows(self) -> list[dict[str, Any]]:
        return [
            row for row in self._rows
            if int(row.get("run_id") or -1) in self._marked_run_ids
        ]

    def _known_groups(self) -> list[dict[str, Any]]:
        """Every group with its description, empty ones included.

        Empty groups are offered too, so one prepared in advance is reachable;
        the descriptions travel with them because the dialog has to be able to
        show the right one the moment the user names a different group.
        """
        if self._handle is None or not self._handle.available:
            return []
        return kit_history.all_groups(self._handle)

    def _about_of(self, name: str, groups: list[dict[str, Any]] | None = None) -> str:
        if not name:
            return ""
        key = kit_history.group_key(name)
        for group in (self._known_groups() if groups is None else groups):
            if kit_history.group_key(group.get("name")) == key:
                return str(group.get("about") or "")
        return ""

    def _save_about(self, name: str, about: str, groups: list[dict[str, Any]]) -> None:
        """Write the shared description only when it actually changed.

        Assignment must not be able to blank out a description just because the
        dialog was opened on a different group.
        """
        if not name or about.strip() == self._about_of(name, groups).strip():
            return
        kit_history_mutate.set_group_about(self._history_db, name, about)

    def _current_run_id(self) -> int | None:
        run = (self._selected_detail or {}).get("run") or {}
        try:
            return int(run["run_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def _run_subject(self, run: dict[str, Any]) -> str:
        """'04 Sep 20:01 · Ideogram 4 (fp8)' - which run this dialog is about."""
        return " \u00b7 ".join(
            part for part in (
                kit_history.format_stamp(run.get("started_at") or run.get("created_at")),
                str(run.get("model_name") or ""),
            ) if part
        )

    def _after_annotation(self, changed: int, focus: int | None = None) -> None:
        """Reload from the journal, then put the user back where they were.

        Refilling the table destroys and re-inserts every row, which drops the
        tree selection and empties Run details.  Re-selecting the row restores
        both, because the selection event renders the details again.
        """
        self.load_journal()
        if focus is not None:
            iid = str(focus)
            try:
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
            except Exception:
                pass
        self.refresh_state.configure(
            text=self.tr("Annotation updated \u00b7 {n} run(s)").format(n=changed)
        )

    def set_group_selected(self) -> None:
        rows = self._marked_rows()
        if not rows:
            return
        verb = self.tr("Assign to group\u2026")
        current = {str(row.get("label") or "") for row in rows}
        # Offer the shared group back only when they really do share one.
        shared = current.pop() if len(current) == 1 else ""
        focus = self._current_run_id()
        groups = self._known_groups()
        chosen = _ask_annotation(
            self.tab_history,
            tr=self.tr,
            title=verb,
            prompt=self.tr("Group {n} selected run(s)").format(n=len(rows)),
            subject=self.tr("Ticked rows only. Comments are not touched."),
            groups=groups,
            group=shared,
            about=self._about_of(shared, groups),
            dark=self._theme_name_getter() == "dark",
        )
        if chosen is None:
            return
        name, about, _note = chosen
        try:
            result = kit_history_mutate.assign_group(
                self._history_db, [int(row["run_id"]) for row in rows], name
            )
            self._save_about(name, about, groups)
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(verb, exc)
            return
        self._after_annotation(result.changed, focus=focus)

    def clear_group_selected(self) -> None:
        """Take the marked runs out of their group.

        The group itself survives with its description, and every individual
        comment survives too: a comment is about the inference, not about the
        membership.  Putting a run back is then just another assignment.
        """
        rows = self._marked_rows()
        if not rows:
            return
        verb = self.tr("Remove from group")
        focus = self._current_run_id()
        if not messagebox.askyesno(
            verb,
            self.tr("Remove {n} selected run(s) from their group?").format(n=len(rows))
            + "\n\n"
            + self.tr("The group and the individual comments are kept."),
            parent=self.tab_history,
        ):
            return
        try:
            result = kit_history_mutate.clear_group(
                self._history_db, [int(row["run_id"]) for row in rows]
            )
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(verb, exc)
            return
        self._after_annotation(result.changed, focus=focus)

    def edit_annotation(self) -> None:
        """Group, group description and comment for ONE run: the highlighted one.

        A comment answers "what did this particular result mean", so it is
        deliberately not a bulk operation.  All three go in one transaction.
        """
        verb = self.tr("Annotate\u2026")
        detail = self._selected_detail
        run = (detail or {}).get("run") or {}
        if not run:
            messagebox.showinfo(verb, self.tr("Select a run first."), parent=self.tab_history)
            return
        run_id = int(run["run_id"])
        annotation = (detail or {}).get("annotation") or {}
        chosen = _ask_annotation(
            self.tab_history,
            tr=self.tr,
            title=verb,
            prompt=self.tr("Annotation for this run"),
            subject=self._run_subject(run),
            groups=self._known_groups(),
            group=str(annotation.get("group") or ""),
            about=str(annotation.get("about") or ""),
            note=str(annotation.get("note") or ""),
            dark=self._theme_name_getter() == "dark",
        )
        if chosen is None:
            return
        name, about, note = chosen
        try:
            result = kit_history_mutate.set_annotation(
                self._history_db, run_id, group=name, about=about, note=note
            )
        except Exception as exc:  # noqa: BLE001
            self._report_write_failure(verb, exc)
            return
        self._after_annotation(result.changed, focus=run_id)

    def _style_preview_controls(self) -> None:
        """Paint the Preview/512 plates from their current toggle state.

        The checkboxes keep native binary semantics, while the surrounding
        colour blocks make the mode visible at a glance.  512 is muted when
        Preview is off because it has no independent meaning then.
        """
        preview_on = bool(self.preview_var.get())
        preview_bg = (theme.HISTORY_PREVIEW_TOGGLE_ON_BG if preview_on
                      else theme.HISTORY_PREVIEW_TOGGLE_BG)
        self.preview_plate.configure(
            bg=preview_bg, highlightbackground=theme.HISTORY_PREVIEW_TOGGLE_EDGE,
            highlightcolor=theme.HISTORY_PREVIEW_TOGGLE_EDGE,
        )
        self.preview_toggle.configure(
            bg=preview_bg, activebackground=theme.HISTORY_PREVIEW_TOGGLE_ON_BG,
            fg=theme.HISTORY_PREVIEW_TOGGLE_FG,
            activeforeground=theme.HISTORY_PREVIEW_TOGGLE_FG,
            selectcolor=theme.HISTORY_PREVIEW_TOGGLE_ON_BG,
        )

        large_on = preview_on and bool(self.preview_large_var.get())
        large_bg = (theme.HISTORY_PREVIEW_512_ON_BG if large_on
                    else theme.HISTORY_PREVIEW_512_BG if preview_on
                    else theme.HISTORY_PREVIEW_512_DISABLED_BG)
        large_fg = (theme.HISTORY_PREVIEW_512_FG if preview_on
                    else theme.HISTORY_PREVIEW_512_DISABLED_FG)
        self.preview_large_plate.configure(
            bg=large_bg, highlightbackground=theme.HISTORY_PREVIEW_512_EDGE,
            highlightcolor=theme.HISTORY_PREVIEW_512_EDGE,
        )
        self.preview_large_toggle.configure(
            bg=large_bg, fg=large_fg, disabledforeground=large_fg,
            activebackground=theme.HISTORY_PREVIEW_512_ON_BG,
            activeforeground=theme.HISTORY_PREVIEW_512_FG,
            selectcolor=theme.HISTORY_PREVIEW_512_ON_BG,
        )

    def toggle_preview_size(self) -> None:
        """Switch between fitting the panel and a 512 px close-up.

        The panel widens with the picture, which is the point: preview size is
        really a switch between reading the table and judging the image, and
        the sash position is remembered either way.
        """
        self.preview_large_var.set(bool(self.preview_large_var.get()))
        self._style_preview_controls()
        if self.preview_large_var.get():
            try:
                width = int(self.detail_panes.winfo_width())
                if width > HISTORY_PREVIEW_LARGE + HISTORY_WORK_MIN_WIDTH:
                    self.detail_panes.sash_place(
                        0, width - HISTORY_PREVIEW_LARGE - 24, 0)
            except Exception:
                pass
        self._preview_run = None          # the same run, at a different size
        self._render_preview()

    def _report_write_failure(self, verb: str, exc: Exception) -> None:
        """Say what the data layer actually refused, in its own words.

        Two of its refusals are deliberate answers rather than faults, and each
        carries the numbers the user needs: a rated group says how much work is
        at stake, an out-of-date journal says which step is missing.  A bare
        traceback would throw both away and read as a crash.
        """
        if isinstance(exc, kit_history_mutate.RatedGroupError):
            messagebox.showwarning(
                verb,
                self.tr("{group} holds {criteria} criterion(s) and {ratings} rating(s). "
                        "Withdraw the ratings first if it should really go.").format(
                    group=exc.group, criteria=exc.criteria, ratings=exc.ratings),
                parent=self.tab_history,
            )
            return
        if isinstance(exc, kit_history_mutate.OutdatedExtensionError):
            self._show_migration_bar(True)
            messagebox.showwarning(
                verb,
                self.tr("Groups and comments are hidden: this journal was written by an "
                        "earlier Kit and needs a one-time migration."),
                parent=self.tab_history,
            )
            return
        messagebox.showerror(verb, f"{type(exc).__name__}: {exc}", parent=self.tab_history)

    def _heading_context_menu(self, event):
        if self._grid_is_native:
            if self.tree.identify_region(event.x, event.y) not in {"heading", "separator"}:
                return None
        else:
            column_id = self.tree.column_at(event, header=True)
            if self._criterion_for_column(column_id) is not None:
                return self._criterion_header_menu(event, str(column_id))
            if column_id == EVALUATION_PLUS_COLUMN:
                menu = tk.Menu(self._menu_parent, tearoff=False)
                if self._theme_name_getter() == "dark":
                    menu.configure(**theme.dark_menu_options())
                menu.add_command(label=self.tr("Add criterion"), command=self.add_criterion)
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
                return "break"
        try:
            self.column_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.column_menu.grab_release()
        return "break"

    def _heading_floor_text(self, spec: ColumnSpec) -> str:
        """Longest normal heading state this column must fit.

        Reserve a sort arrow for sortable columns even when that column is not
        currently active, otherwise the heading would grow/clamp on first click.
        """
        if spec.id == EVALUATION_PLUS_COLUMN:
            return EVALUATION_PLUS_GLYPH
        if spec.id in EVALUATION_SLOT_COLUMNS:
            return "★★★★★"
        text = spec.title if spec.id == "pick" else self.tr(spec.title)
        if spec.id == "date":
            text = f"{text} ({self.tr('local')})"
        if spec.sort_key is not None:
            text += " ▼"
        return text

    def _column_minwidth(self, spec: ColumnSpec) -> int:
        """Compute a DPI/font/translation-aware Treeview minimum width."""
        header_px = self._history_header_font.measure(self._heading_floor_text(spec))
        sample = self.tr(spec.sample) if spec.translate_sample else spec.sample
        sample_px = self._history_body_font.measure(sample)
        return max(header_px, sample_px) + int(self._history_column_padding)

    def _heading_text(self, spec: ColumnSpec) -> str:
        if spec.id == EVALUATION_PLUS_COLUMN:
            return EVALUATION_PLUS_GLYPH
        if spec.id in EVALUATION_SLOT_COLUMNS:
            criterion = self._criterion_for_column(spec.id)
            text = str((criterion or {}).get("name") or "")
            if (criterion is not None and self._grouping() is None
                    and self._sort_column == spec.id):
                text += " ▼" if self._sort_desc else " ▲"
            return text
        text = self._pick_heading_text() if spec.id == "pick" else self.tr(spec.title)
        if spec.id == "date":
            text = f"{text} ({self.tr('local')})"
        if self._grouping() is None and self._sort_column == spec.id and spec.sort_key is not None:
            text += " ▼" if self._sort_desc else " ▲"
        if spec.id in HISTORY_ANNOTATION_COLUMNS and self._zone_expanded:
            text = f"{text}  {HEADER_MENU_GLYPH}"
        return text

    def _refresh_headings(self) -> None:
        grouped = self._grouping() is not None
        for spec in COLUMN_SPECS:
            if spec.id == EVALUATION_PLUS_COLUMN:
                command = self.add_criterion if (self._zone_expanded and not self._grid_is_native) else ""
            elif spec.id == "pick":
                command = self._toggle_page_marks
            elif spec.id in EVALUATION_SLOT_COLUMNS:
                command = (
                    (lambda key=spec.id: self._sort_by(key))
                    if self._criterion_for_column(spec.id) is not None and not grouped else ""
                )
            else:
                command = (lambda key=spec.id: self._sort_by(key)) if spec.sort_key is not None else ""
            self.tree.heading(spec.id, text=self._heading_text(spec), command=command)
        self.tree.heading("#0", text=self.tr("Group"))
        if grouped:
            # Sorting and bulk "select all" are disabled in grouped mode. The
            # + action is not sorting and remains available for the one active
            # user Group even if its runs are collected by another view key.
            for spec in COLUMN_SPECS:
                command = self.add_criterion if (
                    spec.id == EVALUATION_PLUS_COLUMN
                    and self._zone_expanded and not self._grid_is_native
                ) else ""
                self.tree.heading(spec.id, text=self._heading_text(spec), command=command)
        if not self._grid_is_native:
            self.tree.redraw()

    def _sort_by(self, column_id: str) -> None:
        if self._grouping() is not None:
            return
        spec = COLUMN_BY_ID.get(column_id)
        if spec is None or spec.sort_key is None:
            return
        if column_id in EVALUATION_SLOT_COLUMNS and self._criterion_for_column(column_id) is None:
            return
        if self._sort_column == column_id:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_column = column_id
            self._sort_desc = False
        self._page = 1
        self._fill_table()

    def _sorted_rows(self) -> list[dict[str, Any]]:
        spec = COLUMN_BY_ID.get(self._sort_column)
        if spec is None or spec.sort_key is None:
            return list(self._rows)
        present: list[tuple[Any, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for row in self._rows:
            try:
                key = spec.sort_key(row)
            except Exception:
                key = None
            if key is None:
                missing.append(row)
            else:
                present.append((key, row))
        try:
            present.sort(key=lambda item: item[0], reverse=self._sort_desc)
        except TypeError:
            # A malformed historical value must not break the entire journal.
            present.sort(key=lambda item: str(item[0]).casefold(), reverse=self._sort_desc)
        # Contract: missing values are last for ASC and DESC.
        return [row for _key, row in present] + missing

    def _page_size(self) -> int:
        try:
            value = int(self.page_size_var.get())
        except (TypeError, ValueError):
            value = DEFAULT_PAGE_SIZE
        return value if value in PAGE_SIZES else DEFAULT_PAGE_SIZE

    def _page_size_changed(self, _event=None) -> None:
        self._clear_marks()
        self._page = 1
        self._fill_table()

    def _page_count(self, total: int) -> int:
        size = self._page_size()
        return max(1, (max(0, total) + size - 1) // size)

    def _set_page(self, page: int) -> None:
        if self._grouping() is not None:
            return
        pages = self._page_count(len(self._rows))
        target = max(1, min(int(page), pages))
        if target == self._page:
            return
        self._clear_marks()
        self._page = target
        self._fill_table()

    def _page_first(self) -> None:
        self._set_page(1)

    def _page_previous(self) -> None:
        self._set_page(self._page - 1)

    def _page_next(self) -> None:
        self._set_page(self._page + 1)

    def _page_last(self) -> None:
        self._set_page(self._page_count(len(self._rows)))

    def _tree_width_for_grouping(self) -> int:
        grouping = self._grouping()
        if grouping is None:
            return HISTORY_GROUP_COLUMN_WIDTH
        _label, logical = grouping
        return int(HISTORY_GROUP_COLUMN_BY_LOGICAL.get(logical, HISTORY_GROUP_COLUMN_WIDTH))

    def _apply_tree_group_width(self) -> None:
        width = self._tree_width_for_grouping()
        self.tree.column(
            "#0", width=width, minwidth=HISTORY_GROUP_COLUMN_MIN_WIDTH,
            stretch=False, anchor="w",
        )

    def _group_changed(self) -> None:
        self._clear_marks()
        self._page = 1
        self._apply_tree_group_width()
        self._fill_table()

    def _shift_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if delta:
            self.tree.xview_scroll(-1 if delta > 0 else 1, "units")
            return "break"
        return None

    def _update_page_controls(self, total: int) -> None:
        grouped = self._grouping() is not None
        if grouped:
            self.page_size_box.configure(state="disabled")
            self.page_range_label.configure(text=self.tr("{n} runs").format(n=total))
            self.page_number_label.configure(text="")
            for button in self.page_buttons:
                button.configure(state="disabled")
            return

        self.page_size_box.configure(state="readonly")
        pages = self._page_count(total)
        self._page = max(1, min(self._page, pages))
        size = self._page_size()
        first = 0 if total == 0 else (self._page - 1) * size + 1
        last = min(total, self._page * size)
        self.page_range_label.configure(text=f"{first}–{last} / {total}")
        self.page_number_label.configure(text=f"{self.tr('Page')} {self._page}/{pages}")

        # page_buttons were packed in last/next/previous/first order.
        last_b, next_b, prev_b, first_b = self.page_buttons
        first_b.configure(state=("normal" if self._page > 1 else "disabled"))
        prev_b.configure(state=("normal" if self._page > 1 else "disabled"))
        next_b.configure(state=("normal" if self._page < pages else "disabled"))
        last_b.configure(state=("normal" if self._page < pages else "disabled"))

    def _restore_split_position(self, attempt: int = 0) -> None:
        try:
            height = int(self.history_panes.winfo_height())
        except Exception:
            return
        minimum = HISTORY_TABLE_MIN_HEIGHT + HISTORY_DETAIL_MIN_HEIGHT
        if height <= minimum and attempt < 4:
            self._after(40, lambda: self._restore_split_position(attempt + 1))
            return
        if height <= 1:
            return
        fraction = min(0.82, max(0.25, float(self._saved_split_fraction)))
        y = int(height * fraction)
        y = max(HISTORY_TABLE_MIN_HEIGHT, min(y, height - HISTORY_DETAIL_MIN_HEIGHT))
        try:
            self.history_panes.sash_place(0, 0, y)
        except Exception:
            pass

    def _fill_table(self) -> None:
        self._clear_table()
        for row in self._rows:
            row["_state"] = kit_history.correlation_state(row, self._sessions)

        grouping = self._grouping()
        self._apply_tree_group_width()
        self.tree.configure(show="tree headings" if grouping else "headings")
        self._refresh_headings()

        if grouping is None:
            ordered = self._sorted_rows()
            pages = self._page_count(len(ordered))
            self._page = max(1, min(self._page, pages))
            size = self._page_size()
            start = (self._page - 1) * size
            shown = ordered[start:start + size]
            for row in shown:
                iid = str(row.get("run_id"))
                self._by_iid[iid] = row
                self.tree.insert(
                    "", "end", iid=iid, values=self._row_values(row),
                    tags=(("removed",) if row.get("excluded_at") else ()),
                )
        else:
            label, logical = grouping
            # Grouping remains one unpaged tree in v1.  Group rows are already
            # compact/collapsible; paginating inside them creates ambiguous UX.
            secondary = "recipe" if logical == "model_name" else "model_name"
            for index, (key, rows) in enumerate(kit_history.group_rows(self._rows, logical)):
                summary = kit_history.group_summary(rows)
                group_iid = f"{GROUP_PREFIX}{index}"
                self._groups[group_iid] = (label, key, rows)
                self.tree.insert(
                    "", "end", iid=group_iid, open=False,
                    text=(
                        kit_history.elide_middle(key, 70) if logical == "prompt" and key
                        else kit_history.elide(key or self.tr(_EMPTY_GROUP[logical]), 70)
                    ),
                    values=self._group_row_values(summary),
                )
                for row in sorted(
                    rows,
                    key=lambda r: (str(r.get(secondary) or ""), str(r.get("started_at") or "")),
                ):
                    iid = str(row.get("run_id"))
                    self._by_iid[iid] = row
                    self.tree.insert(
                        group_iid, "end", iid=iid, values=self._row_values(row),
                        tags=(("removed",) if row.get("excluded_at") else ()),
                    )

        self._update_page_controls(len(self._rows))
        self._update_mark_controls()

        summary = kit_history.completed_only_summary(self._rows)
        # Timing is reported only for terminal statuses. Unknown/future statuses
        # are still counted explicitly so an in_progress row never disappears
        # from the summary just because STATUS_ORDER predates it.
        parts = [self.tr("{n} runs").format(n=summary["total"])]
        for status in kit_history.STATUS_ORDER:
            bucket = summary.get(status) or {}
            if bucket.get("runs"):
                parts.append(
                    f"{self.tr(status)} {bucket['runs']} · "
                    f"{kit_history.format_duration(bucket['seconds'])}"
                )
        extra_counts: dict[str, int] = {}
        for row in self._rows:
            raw = str(row.get("status") or "unknown").casefold()
            if raw not in kit_history.STATUS_ORDER:
                extra_counts[raw] = extra_counts.get(raw, 0) + 1
        for status in sorted(extra_counts):
            parts.append(f"{self._status_display(status)} {extra_counts[status]}")

        if self._rows:
            self.count_label.configure(text="     ".join(parts))
            self._show_detail(self.tr("Select a run to see its details."))
        else:
            self.count_label.configure(
                text=self.tr("0 runs") + "     " + self.tr("No matches")
            )
            self._show_detail(self.tr("No runs match the current filters."))

    def _counts_text(self, summary: dict[str, Any]) -> str:
        parts = []
        for status in kit_history.STATUS_ORDER:
            if summary.get(status):
                parts.append(f"{self.tr(status)} {summary[status]}")
        return " \u00b7 ".join(parts)

    def _selection_changed(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection or self._handle is None:
            return
        if selection[0].startswith(GROUP_PREFIX):
            self._selected_detail = None
            self._show_detail(self._render_group(*self._groups[selection[0]]))
            self._schedule_preview()
            return
        row = self._by_iid.get(selection[0])
        if row is None:
            return
        detail = kit_history.query_run_detail(self._handle, int(row["run_id"]))
        self._selected_detail = detail
        self._show_detail(self._render_detail(detail))
        self._schedule_preview()

    # -- details -----------------------------------------------------------
    def _render_group(self, label: str, key: str, rows: list[dict[str, Any]]) -> str:
        """What a group of runs adds up to.

        Reports, does not rank.  The breakdown is ordered by how many runs each
        value has, not by which looks best, and failed time is never folded into
        the completed average.
        """
        summary = kit_history.group_summary(rows)
        logical = dict(GROUPINGS)[label]
        lines = [f"{self.tr('GROUP')} \u00b7 {self.tr(label)}", "", f"  {key or self.tr(_EMPTY_GROUP[logical])}", ""]
        # A user group carries a description; it belongs at the head of its own
        # summary, where the question "what was this set for?" is being asked.
        about = ""
        if logical == "label" and key:
            about = next(
                (str(row.get("group_about") or "") for row in rows if row.get("group_about")),
                "",
            )
        if about:
            lines += [f"  {about}", ""]
        lines.append(
            "  "
            + self.tr("{n} runs").format(n=summary["runs"])
            + "   "
            + self._counts_text(summary)
        )
        lines.append(
            "  "
            + self.tr("completed time {t}").format(
                t=kit_history.format_duration(summary["completed_seconds"])
            )
            + (
                "   " + self.tr("average {t}").format(
                    t=kit_history.format_seconds(summary["average"])
                )
                if summary["average"] is not None else ""
            )
        )
        for status, seconds in (("failed", summary["failed_seconds"]), ("canceled", summary["canceled_seconds"])):
            if seconds:
                lines.append(
                    "  "
                    + self.tr("{s} time {t}").format(
                        s=self.tr(status), t=kit_history.format_duration(seconds)
                    )
                )

        secondary = "recipe" if logical == "model_name" else "model_name"
        buckets = kit_history.group_rows(rows, secondary)
        if len(buckets) > 1:
            heading = "BY KIT MODE" if secondary == "recipe" else "BY MODEL"
            lines += ["", self.tr(heading), ""]
            lines.append(
                f"  {self.tr('value'):<34}{self.tr('runs'):>5}{self.tr('ok'):>5}"
                f"{self.tr('fail'):>6}{self.tr('average'):>11}"
            )
            for value, bucket_rows in buckets:
                inner = kit_history.group_summary(bucket_rows)
                name = kit_history.elide(value or self.tr(_EMPTY_GROUP[secondary]), 33)
                average = (
                    kit_history.format_seconds(inner["average"])
                    if inner["average"] is not None else "\u2014"
                )
                lines.append(
                    f"  {name:<34}{inner['runs']:>5}{inner['completed']:>5}"
                    f"{inner['failed']:>6}{average:>11}"
                )
        return "\n".join(lines)

    def _render_detail(self, detail: dict[str, Any]) -> str:
        run = detail.get("run") or {}
        if not run:
            return self.tr("This run is no longer in the journal.")

        lines: list[str] = [self.tr("RUN"), ""]

        def put(label: str, value: Any) -> None:
            if value not in (None, "", []):
                lines.append(f"  {self.tr(label):<16}{value}")

        put("Status", self.tr(str(run.get("status") or "unknown").capitalize()))
        put("Model", run.get("model_name"))
        # Which physical file this run produced, up here with the rest of
        # its identity: the picture answers "what was it", the filename
        # answers "which file on disk".
        produced = detail.get("artifacts") or []
        if produced:
            more = len(produced) - 1
            put("Result", f"{produced[0].get('name') or ''}"
                + (f"   +{more}" if more > 0 else ""))
        put("Prompt", run.get("prompt"))
        put("Negative", run.get("negative"))
        put("Seed", run.get("seed"))
        if run.get("width") and run.get("height"):
            put("Resolution", f"{run['width']} \u00d7 {run['height']}")
        put("Steps", run.get("steps"))
        put("Scheduler", run.get("scheduler"))
        put("CFG", run.get("cfg_scale"))
        put("Wall time", kit_history.format_seconds(run.get("wall_seconds")))
        started = run.get("started_at") or run.get("created_at")
        if started:
            # Local for reading, UTC alongside it so Copy details stays unambiguous.
            put("Started", f"{kit_history.format_stamp(started)}   (UTC {started})")
        put("Device", run.get("device"))
        put("Metadata", run.get("metadata_source"))
        if run.get("excluded_at"):
            put("History", self.tr("Removed from normal view") + f"   (UTC {run.get('excluded_at')})")

        annotation = detail.get("annotation") or {}
        if annotation.get("group") or annotation.get("note"):
            lines += ["", self.tr("USER ANNOTATION"), ""]
            put("Group", annotation.get("group"))
            put("About", annotation.get("about"))
            put("Comment", annotation.get("note"))

        components = detail.get("components") or []
        if components:
            lines += ["", self.tr("COMPONENTS"), ""]
            for component in components:
                role = str(component.get("role") or "")
                name = component.get("model_name") or ""
                # Historical name, exactly as the run recorded it.  The current
                # registry may show something else, or nothing at all.
                lines.append(f"  {role:<24}{name}")

        failures = detail.get("failures") or []
        if failures:
            lines += ["", self.tr("FAILURE"), ""]
            for failure in failures:
                put("Class", failure.get("error_class") or run.get("error_type"))
                put("Node", failure.get("node_type"))
                put("Message", failure.get("error_message"))
        elif run.get("error_type"):
            lines += ["", self.tr("FAILURE"), ""]
            put("Class", run.get("error_type"))
            put("Message", run.get("error_message"))

        artifacts = detail.get("artifacts") or []
        lines += ["", self.tr("RESULT"), ""]
        if artifacts:
            for artifact in artifacts:
                lines.append(f"  {artifact.get('kind') or 'file':<16}{artifact.get('name')}")
        else:
            lines.append("  " + self.tr("No result file."))

        lines += ["", self.tr("KIT SESSION"), ""]
        session = detail.get("session")
        if not session:
            state = kit_history.correlation_state(run, self._sessions)
            if state == kit_history.PENDING:
                lines.append(
                    "  "
                    + self.tr(
                        "Pending — the Kit session is still open.\n"
                        "  Close InvokeAI, then press Sync now: the Kit mode and the\n"
                        "  launch snapshot attach once the session records its exit."
                    )
                )
            else:
                # NULL provenance is broader than "predates History": a new
                # manual/external InvokeAI run can also have no matching Kit
                # session.  State only the fact we actually know.
                lines.append("  " + self.tr("No matching Kit session was recorded for this run."))
            return "\n".join(lines)

        put("Kit mode", session.get("recipe"))
        put("Launcher", session.get("launcher"))
        put("Session", session.get("kit_session_id"))
        for label, key in (("Started", "started_at"), ("Finished", "finished_at")):
            raw = session.get(key)
            if raw:
                put(label, f"{kit_history.format_stamp(raw)}   (UTC {raw})")
        if session.get("end_quality") == "bounded" and session.get("finished_at_bound"):
            raw = session.get("finished_at_bound")
            put("End quality", self.tr("bounded — process observed dead no later than") + f" {kit_history.format_stamp(raw)}   (UTC {raw})")
        code = session.get("exit_code")
        if code is not None:
            name = _EXIT_CODE_NAMES.get(int(code)) if str(code).lstrip("-").isdigit() else None
            put("Process exit", f"{code}" + (f"  ({name})" if name else ""))

        snapshot = session.get("snapshot") or {}
        gpu = snapshot.get("gpu_config") or {}
        if gpu:
            put("GPU", gpu.get("GPU_NAME"))
            put("VRAM", f"{gpu.get('GPU_VRAM_MIB')} MiB" if gpu.get("GPU_VRAM_MIB") else None)
            arch = " / ".join(x for x in (gpu.get("GPU_ARCH"), gpu.get("GPU_CC")) if x)
            put("Architecture", arch or None)
        put("Runtime", f"torch {snapshot['torch_version']}" if snapshot.get("torch_version") else None)
        put("InvokeAI", snapshot.get("invokeai_version"))
        contract = snapshot.get("contract_snapshot") or {}
        substrate = (contract.get("substrate") or {}) if isinstance(contract, dict) else {}
        profile = " / ".join(
            x for x in (substrate.get("profile"), contract.get("memory_profile")) if x
        )
        put("Profile", profile.upper() if profile else None)
        put("Launcher SHA", snapshot.get("launcher_sha256"))
        put("Snapshot", snapshot.get("outcome"))
        put("Panel", snapshot.get("panel_version"))
        return "\n".join(lines)

    def _show_detail(self, text: str) -> None:
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)

    # -- preview -----------------------------------------------------------
    def toggle_preview(self) -> None:
        """Show or hide the result panel.  The table above never moves."""
        self.preview_large_toggle.configure(
            state="normal" if self.preview_var.get() else "disabled")
        self._style_preview_controls()
        if self.preview_var.get():
            try:
                self.detail_panes.add(
                    self.preview_box, minsize=HISTORY_PREVIEW_MIN_WIDTH,
                    width=HISTORY_PREVIEW_INITIAL_WIDTH, stretch="never",
                )
            except Exception:
                pass
            self._schedule_preview()
        else:
            try:
                self.detail_panes.forget(self.preview_box)
            except Exception:
                pass
            self._preview_shown = None
            self._preview_geometry = None

    def _preview_resized(self, event) -> None:
        """Re-fit after a real resize only.

        Rendering changes the label's own size, so reacting to every
        <Configure> would chase its own tail.
        """
        size = (int(event.width), int(event.height))
        if self._preview_geometry == size:
            return
        self._preview_geometry = size
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        if self._preview_job is not None:
            try:
                self.tab_history.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None
        if not getattr(self, "preview_var", None) or not self.preview_var.get():
            return
        # Rating a run does not change which picture is on screen.  Guarding on
        # the RUN rather than on a longer delay means a click inside the row
        # already shown costs nothing at all, while arrowing down a page still
        # coalesces through HISTORY_PREVIEW_DELAY_MS below.
        current = self._current_run_id()
        if (current is not None and current == self._preview_run
                and self._preview_shown is not None):
            return
        self._preview_run = current
        self._preview_job = self._after(HISTORY_PREVIEW_DELAY_MS, self._render_preview)

    def _render_preview(self) -> None:
        self._preview_job = None
        if not self.preview_var.get():
            return
        detail = self._selected_detail or {}
        if not (detail.get("run") or {}):
            self._show_preview(None, self.tr("Select a run to see its result."))
            return
        artifacts = detail.get("artifacts") or []
        if not artifacts:
            # A failed run has no picture, and saying so is the honest answer;
            # leaving the previous one up would attribute it to this run.
            self._show_preview(None, self.tr("This run produced no result file."))
            return
        name = str(artifacts[0].get("name") or "")
        extra = len(artifacts) - 1
        caption = name + (f"   +{extra}" if extra > 0 else "")
        path = kit_history.resolve_artifact_path(self._outputs_dir, artifacts[0])
        if path is None or not path.is_file():
            self._show_preview(None, self.tr("Result file is no longer present."), caption)
            return
        image = self._preview_photo(path)
        if image is None:
            # Tk reads PNG and GIF and nothing else; video and WEBP land here.
            self._show_preview(None, self.tr("This result cannot be shown here."), caption)
            return
        self._show_preview(image, "", caption)

    def _preview_photo(self, path: Path):
        try:
            key = str(path)
            original = self._preview_cache.get(key)
            if original is None:
                original = tk.PhotoImage(file=key)
                self._preview_cache[key] = original
                while len(self._preview_cache) > HISTORY_PREVIEW_CACHE:
                    self._preview_cache.pop(next(iter(self._preview_cache)))
            holder = (
                max(1, int(self.preview_holder.winfo_width()) - 4),
                max(1, int(self.preview_holder.winfo_height()) - 4),
            )
            box = _preview_box(holder, bool(self.preview_large_var.get()))
            factor = _preview_scale((original.width(), original.height()), box)
            if factor <= 1:
                return original
            # Derived images get their own bounded cache keyed by (file, factor),
            # so switching between the two modes re-uses work instead of
            # decoding the PNG again - and one cap covers both sizes rather
            # than two independent ones growing side by side.
            derived_key = (key, factor)
            scaled = self._preview_scaled.get(derived_key)
            if scaled is None:
                scaled = original.subsample(factor, factor)
                self._preview_scaled[derived_key] = scaled
                while len(self._preview_scaled) > HISTORY_PREVIEW_CACHE:
                    self._preview_scaled.pop(next(iter(self._preview_scaled)))
            return scaled
        except Exception:
            return None

    def _show_preview(self, image, message: str, caption: str = "") -> None:
        self.preview_title.configure(text=caption)
        # Tk drops an image the moment nothing references it, so the currently
        # displayed one is held here rather than only by the widget.
        self._preview_shown = image
        if image is None:
            self.preview_image.configure(image="", text=message)
        else:
            self.preview_image.configure(image=image, text="")

    # -- actions -----------------------------------------------------------
    def open_result(self) -> None:
        verb = "FULL Res"
        detail = self._selected_detail
        if not detail:
            messagebox.showinfo(verb, self.tr("Select a run first."))
            return
        artifacts = detail.get("artifacts") or []
        if not artifacts:
            messagebox.showinfo(verb, self.tr("This run produced no result file."))
            return
        path = kit_history.resolve_artifact_path(self._outputs_dir, artifacts[0])
        if path is None or not path.is_file():
            messagebox.showinfo(
                verb,
                self.tr("Result file is no longer present.") + f"\n\n{path}",
            )
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(verb, f"{type(exc).__name__}: {exc}")

    def copy_details(self) -> None:
        text = self.detail.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo(self.tr("Copy details"), self.tr("There is nothing to copy yet."))
            return
        self._clipboard(text)

    def copy_prompt(self) -> None:
        detail = self._selected_detail or {}
        prompt = (detail.get("run") or {}).get("prompt")
        if not prompt:
            messagebox.showinfo(self.tr("Copy prompt"), self.tr("This run has no recorded prompt."))
            return
        self._clipboard(str(prompt))

    def _detail_context_menu(self, event) -> None:
        try:
            self.history_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.history_menu.grab_release()
