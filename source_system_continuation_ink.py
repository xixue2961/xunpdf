from pathlib import Path
from itertools import product
import numpy as np

def continuation(image, staff, row, direction, far_edge=None, requested_sign=None):
    """Verify a boundary continuation from original curved ink and a real head."""
    x,y,w,h = [float(row[k]) for k in "xywh"]
    lines = [np.array([(float(pt.get("x")),float(pt.get("y"))) for pt in line.findall("point")])
             for line in staff.findall("lines/line")]
    if len(lines)!=5 or any(not len(line) for line in lines):
        return None
    staff_at=lambda xx:np.array([np.interp(xx,l[:,0],l[:,1]) for l in lines])
    space=float(np.median(np.diff(staff_at(x),axis=0)))
    center=y+h/2
    u=np.linspace(0,1,37)
    side=1 if direction=="out" else -1
    start=x+w if side==1 else x
    radius=np.array([-1,0,1])[:,None]
    if far_edge is None and direction=="out":
        return None
    ranges=(zip((-.77,-.42,-.08,.18), (3.8,3.46,3.12,2.86)) if side==1
            else product((-.22,.08,.23),(1.40,1.55,1.68)))
    best={}
    for first,last in ranges:
        xend=min(start+last*space,far_edge-.34*space) if side==1 else start-last*space
        cols=np.rint(start+side*first*space+u*(xend-start-side*first*space)).astype(int)
        staff_rows=staff_at(cols)
        if cols.min()<0 or cols.max()>=image.shape[1]:
            continue
        for sign,near,far,arch in product((requested_sign,) if requested_sign is not None else (-1,1),
                                          (-.12,.03,.18,.33,.48,.63,.78,.93),
                                          (-.12,.03,.18,.33,.48,.63,.78,.93),
                                          (.21,.33,.45,.57,.69)):
            baseline=center+sign*space*(near*(1-u)+far*u)
            curve=baseline+sign*space*arch*4*u*(1-u)
            def witness(ys,threshold):
                all_y=np.rint(ys).astype(int)[None,:]+radius
                if all_y.min()<0 or all_y.max()>=image.shape[0]:
                    return np.zeros(5)
                valid=(abs(all_y[None,:,:]-staff_rows[:,None,:])>1.4).all(axis=0)
                return np.array([bit.mean() for bit in np.array_split(((image[all_y,cols]<threshold)&valid).any(axis=0),5)])
            lo,hi=witness(curve,160),witness(curve,208)
            score=min(lo.min(),hi.min())
            if score < .70:
                continue
            flat=witness(baseline,208)
            if sum(flat[1:4]<.4)<2:
                continue
            val=(score,(lo+hi).mean(),-abs(near-far))
            if sign not in best or val>best[sign][0]:
                best[sign]=(val,dict(sign=sign,range=[int(cols[0]),int(cols[-1])],
                    parameters=[near,far,arch],stripes=[list(lo),list(hi)],
                    xs=cols.tolist(),ys=curve.tolist(),staff_space=space))
    if len(best)!=1:
        return None
    return next(iter(best.values()))[1]

