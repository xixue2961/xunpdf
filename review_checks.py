"""只读比较旧/新识别结果并做原型GUI截图；不宣称逐音验收。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from xunpdf import ROOT, audit, read_score, save_json, normalize_export, apply_confirmed_start_meter


def summarize(path):
    root = read_score(path)
    report = audit(root)
    return dict(path=str(path), parts=report['parts'], measures=report['measure_counts'],
                counts=report['counts'], issues=dict(Counter(i['kind'] for i in report['issues'])),
                meters=[(t['measure'],t['time']) for t in report['time_signatures'] if t['part']=='P1'])


def compare():
    rows=[]
    for job in sorted((ROOT/'jobs').iterdir()):
        manifest=json.loads((job/'manifest.json').read_text(encoding='utf-8'))
        for raw in (job/'raw').rglob('*.mxl') if (job/'raw').exists() else []:
            raw_summary=summarize(raw)
            raw_summary.update(job=job.name, options=manifest['options'])
            if manifest.get('output'):
                raw_summary['export']=summarize(manifest['output'])
            rows.append(raw_summary)
    save_json(ROOT/'review'/'comparisons.json',rows)
    for r in rows:
        print(json.dumps(r,ensure_ascii=False))


def screenshot():
    import tkinter as tk
    from PIL import ImageGrab
    import gui
    original=tk.Tk
    def factory(*args,**kwargs):
        win=original(*args,**kwargs)
        def capture():
            win.update_idletasks()
            box=(win.winfo_rootx(),win.winfo_rooty(),win.winfo_rootx()+win.winfo_width(),win.winfo_rooty()+win.winfo_height())
            ImageGrab.grab(bbox=box).save(ROOT/'review'/'gui-preview.png')
            win.destroy()
        win.after(1800,capture)
        return win
    tk.Tk=factory
    gui.launch()


if __name__=='__main__':
    (ROOT/'review').mkdir(exist_ok=True)
    ap=argparse.ArgumentParser()
    ap.add_argument('--screenshot',action='store_true')
    args=ap.parse_args()
    screenshot() if args.screenshot else compare()
