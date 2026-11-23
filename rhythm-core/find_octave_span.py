"""Locate repeated octave dashes and the original text crop after voice reconstruction."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
import zipfile

import cv2
import numpy as np


def box(n):
    return tuple(int(n.find('bounds').get(k)) for k in 'xywh')


def runs(mask):
    marks=np.flatnonzero(mask)
    return [(int(v[0]),int(v[-1]+1)) for v in np.split(marks,np.flatnonzero(np.diff(marks)>1)+1) if len(v)]


def dash_tracks(gray, x0, x1, lo_y, hi_y, space, threshold):
    hits={}
    for cy in range(lo_y,hi_y):
        stripe=gray[cy-4:cy+5,x0:x1] < threshold
        dark=runs(stripe.sum(axis=0)>=3)
        lanes=[]
        for a,b in dark:
            if not .16*space <= b-a <= .8*space:
                continue
            if not lanes or not .85*space <= a+x0-lanes[-1][-1][0] <= 1.28*space:
                lanes.append([])
            lanes[-1].append((a+x0,b+x0))
        for lane in lanes:
            if len(lane)<9:
                continue
            centers=tuple(a for a,_ in lane)
            if lane[-1][1]-lane[0][0]>=8*space:
                hits.setdefault(centers,[]).append((cy,lane))
    candidates=[]
    for centers,rows in hits.items():
        if len(rows)<2:
            continue
        middle,lane=rows[len(rows)//2]
        candidates.append((middle,lane))
    return candidates


def octave_witness(gray, ends, upper, space):
    x0,x1=ends
    top=min(box(chord)[1] for chord in upper)
    lo_y=max(5,round(top-4*space))
    hi_y=round(top-.3*space)
    possible=defaultdict(list)
    for cy,normal in dash_tracks(gray,x0,x1,lo_y,hi_y,space,160):
        confirmed={}
        for level in (128,208):
            for other_y,dashes in dash_tracks(gray,x0,x1,max(lo_y,cy-2),min(hi_y,cy+3),space,level):
                if len(dashes)==len(normal) and all(abs(a[0]-b[0])<=3 for a,b in zip(dashes,normal)):
                    confirmed[level]=[other_y,dashes]
                    break
        if len(confirmed)!=2:
            continue
        first_dash=normal[0][0]
        line_end=normal[-1][1]
        candidates=[]
        for chord in upper:
            x,y,width,height=box(chord)
            center=x+width/2
            if 2.75*space<=first_dash-center<=3.75*space:
                candidates.append((chord,center))
        if len(candidates)!=1:
            continue
        chord,center=candidates[0]
        covered=[n for n in upper if center <= box(n)[0]+box(n)[2]/2 < line_end]
        if len(covered)<2 or covered[-1] is not upper[-1]:
            continue
        left=round(center-.1*space)
        crop=(left,round(cy-.67*space),first_dash-2,round(cy+1.42*space))
        tile=gray[crop[1]:crop[3],crop[0]:crop[2]]
        if tile.size==0 or np.count_nonzero(tile<128)<.2*space*space:
            continue
        key=(chord.get('id'),covered[-1].get('id'),first_dash,line_end)
        possible[key].append(dict(first_chord=key[0],last_chord=key[1],normal_dashes=normal,
                                  other_thresholds=confirmed,center_y=cy,label_clip_xyxy=crop))
    if len(possible)!=1:
        return None
    matching=next(iter(possible.values()))
    return matching[len(matching)//2]


def main(project):
    out=project/'review/sea463'
    metadata=json.loads((project/'jobs/20261118-112933-18c3c5/source-piano-context.json').read_text(encoding='utf8'))['native_recovery']
    source=Path(metadata['source_job'])
    witness,=json.loads((project/'review/sea459/inner-head-proof.json').read_text(encoding='utf8'))
    setup=witness['witness']
    unit=setup['unit']
    original=source/f'unit-{unit:03d}.png'
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    assert digest==setup['source_sha256']
    gray=cv2.imdecode(np.fromfile(original,np.uint8),0)
    book=project/'review/sea461-r02/apply/export/refined.omr'
    with zipfile.ZipFile(book) as b:
        doc=ET.fromstring(b.read(f'sheet#{unit}/sheet#{unit}.xml'))
    system=doc.find(f"page/system[@id='{setup['system']}']")
    stack=system.find(f"stack[@id='{setup['measure']}']")
    nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
    part=next(p for p in system.findall('part') if p.find(f"staff[@id='{setup['staff']}']") is not None)
    measure=part.find(f"measure[@id='{setup['measure']}']")
    staff_atoms=[nodes[k] for k in measure.findtext('head-chords','').split() if nodes[k].get('staff')==setup['staff']]
    offset=set()
    for row in witness['notes']:
        upper=nodes[row['outer_chord']]
        dx=row['stem_x']
        offset.add(min(staff_atoms,key=lambda ch: abs(dx-box(ch)[0])).get('id'))
    upper=sorted([n for n in staff_atoms if n.get('id') not in offset],key=lambda n:box(n)[0])
    assert len(upper)==7, [(n.get('id'),box(n)) for n in staff_atoms]
    space=int(doc.find('scale/interline').get('main'))
    ends=(int(stack.get('left')),int(stack.get('right')))
    proof=octave_witness(gray,ends,upper,space)
    assert proof is not None
    blank=gray.copy()
    cy=proof['center_y']
    for a,b in proof['normal_dashes'][2:6]:blank[cy-6:cy+7,a-1:b+2]=255
    assert octave_witness(blank,ends,upper,space) is None
    x,y,right,bottom=proof['label_clip_xyxy']
    status,payload=cv2.imencode('.png',gray[y:bottom,x:right])
    assert status
    with (out/'ottava-label-original.png').open('xb') as f:f.write(payload.tobytes())
    proof.update(source_sha256=digest,unit=unit,system=setup['system'],score_staff=setup['staff'],
                 sparse_line_erasure_rejected=True,source_text_read=None)
    with (out/'ottava-geometry-proof.json').open('x',encoding='utf8') as f:json.dump(proof,f,indent=2)
    print('Anchors',proof['first_chord'],proof['last_chord'],'text crop',proof['label_clip_xyxy'])


if __name__=='__main__':
    main(Path(sys.argv[1]))
