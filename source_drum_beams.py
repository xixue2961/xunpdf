
"""Recover beam counts from original percussion stems and horizontal ink."""
from collections import defaultdict
from itertools import groupby
from statistics import median

import numpy as np

from source_drum_ink import cross_head


def cross_columns(image,left,right,top,space):
    observations={}
    for x in range(left,right,2):
        result=cross_head(image,x+.49*space,top,space)
        if result is None:
            continue
        cx,_=result['center']
        quality=min(min(v['ink']) for v in result['arms'])
        observations[cx]=max(quality,observations.get(cx,0))
    clusters=[]
    for x in sorted(observations):
        if not clusters or x-clusters[-1][-1]>space*.65:
            clusters.append([])
        clusters[-1].append(x)
    return [round(median([x for x in window if observations[x]==max(observations[k] for k in window)]))
            for window in clusters]


def down_cross_stem(image,x,top,space):
    """A cross in this window must have its own continuous downward stem."""
    y0=round(top+.65*space)
    y1=round(top+4.05*space)
    xs=np.arange(round(x-.72*space),round(x-.25*space)+1)
    tile=image[y0:y1,xs]
    a=(tile<160).mean(axis=0)
    b=(tile<208).mean(axis=0)
    strong=xs[(a>=.92)&(b>=.95)]
    if not len(strong) or strong.max()-strong.min()>.43*space:
        return None
    return float(median(strong))


def beam_bands(image,left,right,top,space):
    if not left['stem'] or not right['stem'] or left['side']!=right['side']:
        return []
    a,b=left['stem'],right['stem']
    x0,x1=round(a+.32*space),round(b-.32*space)
    if x1-x0<round(.7*space):
        return []
    down=left['side']=='down'
    if down:
        y0=round(max(left['y'],right['y'])+.46*space)
        y1=round(top+7.65*space)
    else:
        y0=round(top-4.1*space)
        y1=round(min(left['y'],right['y'])-.46*space)
    if y0>=y1 or min(y0,x0)<0 or x1>image.shape[1] or y1>image.shape[0]:
        return []
    proof=[]
    for threshold in (160,208):
        active=np.flatnonzero((image[y0:y1,x0:x1]<threshold).mean(axis=1)>=.94)+y0
        runs=[list(chunk) for _,chunk in groupby(enumerate(active),lambda i:i[1]-i[0])]
        picked=[]
        for chunk in runs:
            start,end=int(chunk[0][1]),int(chunk[-1][1])+1
            if not .31*space<=end-start<=.85*space:
                continue
            cy=(start+end)//2
            margins=round(2.2*space)
            if x0-margins<0 or x1+margins>image.shape[1]:
                continue
            side=image[start:end,np.r_[x0-margins:x0,x1:x1+margins]]
            if (side<threshold).mean(axis=1).min()>.95:
                continue
            full_stems=[]
            for slot in (left,right):
                yy=round(slot['y'])
                lo,hi=sorted((yy,cy))
                x=round(slot['stem'])
                region=image[lo:hi+1,x-2:x+3]
                full_stems.append(float((region<threshold).any(axis=1).mean()))
            if min(full_stems)<.84:
                continue
            picked.append((start,end))
        proof.append(picked)
    if len(proof[0])!=len(proof[1]) or not proof[0] or len(proof[0])>2:
        return []
    if any(abs(p-q)>2 for a,b in zip(*proof,strict=True) for p,q in zip(a,b,strict=True)):
        return []
    return proof


def draw_clock(events,image,top,space):
    evidence={}
    for i,(a,b) in enumerate(zip(events,events[1:])):
        bands=beam_bands(image,a,b,top,space)
        if bands:
            evidence[i]=bands
    clock=[]
    for i,e in enumerate(events):
        levels=max((len(evidence[k][0]) for k in (i-1,i) if k in evidence),default=0)
        if (not e['stem']) != (levels==0):
            return [],{}
        clock.append(levels)
    return clock,evidence


def note_beams(levels,connections,index):
    beams={}
    for beam in range(1,levels+1):
        before=index-1 in connections and len(connections[index-1][0])>=beam
        after=index in connections and len(connections[index][0])>=beam
        if not (before or after):
            raise ValueError('No paper beam endpoint for an emitted symbol')
        beams[beam]=('continue' if before and after else 'begin' if after else 'end')
    return beams

