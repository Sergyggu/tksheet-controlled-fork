"""Run preserved September controls using a temporary reconstructed Kit layout."""
from pathlib import Path
import shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='tksheet-controls-') as tmp:
    kit = Path(tmp)
    shutil.copytree(root / 'tksheet', kit / 'vendor/tksheet')
    (kit / 'vendor/tksheet-7.6.0.dist-info').mkdir()
    shutil.copy2(root / 'integration/supergui/kit_grid.py', kit / 'kit_grid.py')
    subprocess.run([sys.executable, str(root / 'tests/legacy/font_family_tier1_controls.py'), '--kit', str(kit)], check=True)
for control, target in [('typography_final_ui_controls.py', 'kit_font_system.py'), ('typography_history_hook_controls.py', 'kit_panel_tab_history.py')]:
    subprocess.run([sys.executable, str(root / 'tests/legacy' / control), str(root / 'integration/supergui' / target)], check=True)
subprocess.run([sys.executable, str(root / 'integration/supergui/kit_font_system.py'), 'selftest'], check=True)
