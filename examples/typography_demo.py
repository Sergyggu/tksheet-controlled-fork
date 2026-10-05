"""Run after installing this fork in a fresh environment."""
import tkinter as tk
from tkinter import font
from tksheet import Sheet

root = tk.Tk()
root.title('tksheet controlled fork — typography and segments')
sheet = Sheet(root, headers=['Normal', 'Local bold', 'Guarded family', 'Segments'],
              data=[['Example', 'Emphasis', 'Family sample', '★★★☆☆']])
sheet.pack(fill='both', expand=True)
sheet.enable_bindings()
mt = sheet.MT
mt.col_options.setdefault(1, {})['font_style'] = 'bold'
mt.col_options.setdefault(2, {})['font_family'] = font.nametofont('TkFixedFont').actual('family')
mt.col_options.setdefault(3, {}).update(segmented_text_count=5, segmented_text_width=16)
sheet.refresh()
print(mt.font_report(mt.col_options[2]['font_family']))
root.mainloop()
