from __future__ import annotations
import ast
import sys
from pathlib import Path

def main() -> int:
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    lab = classes.get("TypographyLab")
    fs = classes.get("FontSystem")
    checks = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'} {name}")
        checks.append(bool(cond))

    check("TypographyLab exists", lab is not None)
    check("FontSystem exists", fs is not None)
    for label in (
        "Load palette…", "Save checkpoint…", "Revert saved", "Base preview",
        "Apply working", "Refresh status", "Save & Close", "Cancel",
    ):
        check(f"button {label}", f'text="{label}"' in text)

    check("single unified Lab", text.count("class TypographyLab") == 1)
    check("role naming contract", "lowercase_ascii_snake_case" in text and "ROLE_NAME_RE" in text)
    check("transactional opening snapshot", "_opening_palette" in text)
    check("cancel restores opening palette", "copy.deepcopy(self._opening_palette)" in text)
    check("load palette does not replace canonical path",
          "self.sys.replace_palette(self.sys.read_palette(path))" in text)
    check("checkpoint uses save-as dialog", "asksaveasfilename" in text)
    check("window close has save/discard/cancel", "askyesnocancel" in text)
    check("preview-only save guard", "Preview-only changes" in text)
    check("internal rows can be hidden", "Show internal columns" in text)
    check("scrollable matrix", "Scrollbar(holder, orient=\"vertical\"" in text)

    passed = sum(checks)
    print(f"TYPOGRAPHY FINAL UI CONTROLS: {passed}/{len(checks)}")
    return 0 if all(checks) else 1

if __name__ == "__main__":
    raise SystemExit(main())
