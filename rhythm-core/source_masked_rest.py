"""Recover the original rest outline beside a known sustained-note stem."""
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json,sys
import cv2,numpy as np

def region(n):
    b=n.find('bounds')
    return tuple(int(b.get(a)) for a in 'xywh')

def match(image, template, obscured, stem, staff):
    sx,sy,sw,sh=region(template)
    gx,gy,_,_=region(obscured)
    stem_x=region(stem)[0]
    ys=[float(n.find('point').get('y')) for n in staff.findall('lines/line')]
    space=int(round(np.median(np.diff(ys))))
    report=[]
    for level in (128,160,208):
        ref=image[sy:sy+sh,sx:sx+sw]<level
        found=[]
        for px in range(gx-round(.45*space),gx+round(.21*space)):
            limit=min(sw,stem_x-px-1)
            if limit<.6*sw:
                continue
            for py in range(gy-round(1.36*space),gy-round(.35*space)):
                yy=np.arange(py,py+sh)
                # Far enough from the symbol to see the actual staff line width.
                line_vote=(image[yy,px-4*space:px-space]<level).mean(axis=1)>.78
                p=(image[py:py+sh,px:px+limit]<level)[~line_vote].ravel()
                q=ref[:, :limit][~line_vote].ravel()
                if not p.any() or not q.any() or int(q.sum())<120:
                    continue
                k=int((p&q).sum())
                score=(float(np.corrcoef(p,q)[0,1]),k/int(p.sum()),k/int(q.sum()))
                if min(score)>=.95:
                    found.append((px,py,score,int(q.sum())))
        if len(found)!=1:
            return None
        x,y,score,samples=found[0]
        report.append(dict(threshold=level,bounds=[x,y,sw,sh],visible_template_ink=samples,
                           correlation=score[0],precision=score[1],recall=score[2]))
    if len({tuple(p['bounds']) for p in report})!=1:
        return None
    return report


