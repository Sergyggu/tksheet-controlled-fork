"""Standalone behavioral checks using real Tk and the installed package."""
import tkinter as tk
import unittest
from tksheet import Sheet

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.sheet = Sheet(self.root, data=[['★★★☆☆', 'text']])
        self.mt = self.sheet.MT
        self.root.update_idletasks()

    def tearDown(self):
        self.root.destroy()

    def test_segments_and_margins(self):
        left, right, centers = self.mt.segmented_text_geometry(0, 108, 5, 16)
        self.assertEqual((left, right), (14, 94))
        for index, center in enumerate(centers):
            self.assertEqual(self.mt.segmented_text_index(0, 108, center, 5, 16), index)
        for x in [0, 13.9, 94, 108]:
            self.assertIsNone(self.mt.segmented_text_index(0, 108, x, 5, 16))

    def test_style_precedence(self):
        self.mt.col_options[0] = {'font_style': 'bold'}
        self.mt.row_options[0] = {'font_style': 'italic'}
        self.mt.cell_options[(0, 0)] = {'font_style': 'normal'}
        self.assertEqual(self.mt.get_cell_font(0, 0)[2], 'normal')
        self.mt.cell_options.clear()
        self.assertEqual(self.mt.get_cell_font(0, 0)[2], 'italic')
        self.mt.row_options.clear()
        self.assertEqual(self.mt.get_cell_font(0, 0)[2], 'bold')

    def test_missing_family_preserves_style_and_size(self):
        self.mt.col_options[0] = {'font_family': '__MISSING_FAMILY_294D7__', 'font_style': 'italic'}
        effective = self.mt.get_cell_font(0, 0)
        base = self.mt.PAR.ops.table_font
        self.assertEqual(tuple(effective), (base[0], base[1], 'italic'))
        self.assertEqual(self.mt.font_report('__MISSING_FAMILY_294D7__')['status'], 'substituted')
        self.sheet.refresh()
        self.root.update_idletasks()

if __name__ == '__main__':
    unittest.main()
