"""Live Tier-1 matrix: several families side by side plus engine diagnostics."""
from __future__ import annotations
import argparse, sys, tkinter as tk
from pathlib import Path


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--kit', required=True)
    ap.add_argument('--families', nargs='*', default=['Consolas','Liberation Mono','Cascadia Mono','Nunito'])
    ns=ap.parse_args()
    kit=Path(ns.kit).resolve(); sys.path.insert(0,str(kit))
    import kit_grid  # type: ignore
    families=list(ns.families)[:6]
    root=tk.Tk(); root.title('Tier-1 font family live matrix R2'); root.geometry('1500x600')
    root.grid_columnconfigure(0,weight=1); root.grid_rowconfigure(1,weight=1)
    info=tk.Text(root,height=max(5,len(families)+2),wrap='none')
    info.grid(row=0,column=0,sticky='ew',padx=8,pady=8)
    cols=tuple(f'c{i}' for i in range(len(families)))
    titles={c:f for c,f in zip(cols,families)}
    g=kit_grid.HistoryGrid(root,columns=cols,displaycolumns=cols,titles=titles,show='headings',height=8,
                           table_font=('Segoe UI',10),header_font=('Segoe UI Semibold',10),row_height=30)
    for c in cols: g.column(c,width=230,minwidth=100,anchor='w')
    samples=['Il1|0O  path/to.py  --flag=42','Український: Ґґ Єє Іі Її','alpha beta gamma delta epsilon zeta','0123456789  []{}() <> :=']
    for n,text in enumerate(samples,1): g.insert('',iid=f'r{n}',values=tuple(text for _ in cols))
    for c,f in zip(cols,families): g.set_column_font_family(c,f,redraw=False)
    g.grid(row=1,column=0,sticky='nsew',padx=8,pady=(0,8)); g.redraw(); root.update_idletasks()
    for c,f in zip(cols,families):
        rep=g.column_font_effective(c)
        info.insert('end',f"{f:28} status={rep.get('status'):11} effective={rep.get('effective')} pipe={rep.get('pipe_height')}/{rep.get('line_box')}  {rep.get('reason')}\n")
    info.configure(state='disabled')
    root.mainloop(); return 0
if __name__=='__main__': raise SystemExit(main())
