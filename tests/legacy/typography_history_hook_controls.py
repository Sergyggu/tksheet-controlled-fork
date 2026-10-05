from __future__ import annotations
import ast
import sys
from pathlib import Path

def main() -> int:
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HistoryTab"), None)
    if cls is None:
        print("FAIL HistoryTab class missing")
        return 1

    methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    checks = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'} {name}")
        checks.append(bool(cond))

    check("open method exists", "_open_typography_lab" in methods)
    check("lazy import only", "import kit_font_system" in text and "\nimport kit_font_system\n" not in text.split("class HistoryTab", 1)[0])
    check("catalogue path", 'kit_root / "font-catalog" / "font_catalog.json"' in text)
    check("palette path", 'kit_root / "font-catalog" / "font_palette.json"' in text)
    check("complete-picks gate", 'complete_for_designer_picks' in text)
    check("saved palette applied before Lab", "self._font_system.apply(self.tree)" in text)
    check("real HistoryGrid passed to Lab", "self.tree, master=self.tab_history" in text)
    check("native backend guarded", "if self._grid_is_native:" in ast.get_source_segment(text, methods["_open_typography_lab"]))
    check("button exists", 'text="Typography Lab…"' in text and "command=self._open_typography_lab" in text)
    check("dispose destroys Lab", 'lab.top.destroy()' in ast.get_source_segment(text, methods["dispose"]))
    check("startup does not auto-apply typography", text.count("self._font_system.apply(self.tree)") == 1)

    passed = sum(checks)
    print(f"TYPOGRAPHY HISTORY HOOK: {passed}/{len(checks)}")
    return 0 if all(checks) else 1

if __name__ == "__main__":
    raise SystemExit(main())
