from pathlib import Path
from itertools import product
from zipfile import ZipFile
import csv
import json
import sys
from xml.etree import ElementTree as ET

import numpy as np

def edge_arc(image, staff, head, direction, expected_sign=None):
    x,y,width,height=(float(head[k]) for k in "xywh")
    lines = [np.asarray([(float(pt.get("x")),float(pt.get("y"))) for pt in line.findall("point")])
             for line in staff.findall("lines/line")]
    if len(lines)!=5 or any(not len(line) for line in lines):
        return None
    space = float(np.median(np.diff([np.interp(x,line[:,0],line[:,1]) for line in lines])))
    side = 1 if direction=="out" else -1
    near = x+width if side==1 else x
    dist = (0.18,1.95) if side==1 else (0.18,1.34)
    u = np.linspace(0,1,40)
    xs=np.rint(near + side*space*(dist[0]+u*(dist[1]-dist[0]))).astype(int)
    staff_y=np.array([np.interp(xs,line[:,0],line[:,1]) for line in lines])
    if xs.min()<0 or xs.max()>=image.shape[1]:
        return None
    center = y+height/2
    radius = np.array([-1,0,1])[:,None]
    def coverage(path,threshold):
        ys=np.rint(path).astype(int)[None,:]+radius
        if ys.min()<0 or ys.max()>=image.shape[0]:
            return np.zeros(5)
        allowed=(abs(ys[None,:,:]-staff_y[:,None,:])>1.5).all(axis=0)
        present=((image[ys,xs]<threshold)&allowed).any(axis=0)
        return np.array([v.mean() for v in np.array_split(present,5)])
    best = {}
    allowed_signs = (-1, 1) if expected_sign is None else (expected_sign,)
    for sign,near_lift,far_lift,arch in product(allowed_signs,(-.12,-.05,.02,.09,.16,.23,.30),(-.12,-.05,.02,.09,.16,.23,.30),
                                               (.23,.29,.35,.41,.47)):
        baseline=center+sign*space*(near_lift*(1-u)+far_lift*u)
        arc=baseline+sign*space*arch*4*u*(1-u)
        a,b=coverage(arc,160),coverage(arc,208)
        flat=coverage(baseline,208)
        rating=min(a.min(),b.min())
        if rating<.75 or sum(flat[1:4]<=.5)<2:
            continue
        key=(rating, a.mean()+b.mean(), -abs(near_lift-far_lift))
        if sign not in best or key>best[sign][0]:
            best[sign]=(key,dict(direction=direction,curve_sign=sign,model=[near_lift,far_lift,arch],
                 columns=[int(xs.min()),int(xs.max())],threshold_supports=[a.tolist(),b.tolist()],
                 flat_support=flat.tolist(),centerline=[round(float(v),2) for v in arc],space=space))
    if len(best)!=1:
        return None
    return next(iter(best.values()))[1]


