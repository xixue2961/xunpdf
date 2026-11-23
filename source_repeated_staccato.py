"""Restore a missing printed dot in an isolated three-chord piano beam group."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_duration import _dot_at_threshold
from source_parallel_rest_gap import graph
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch
from source_vocal_terminal_head import staff_lines


def rhythmic_group(batch):
    sizes={len(g) for g in batch}
    if sizes not in ({2},{3}) or any(r['shape']!='NOTEHEAD_BLACK' for g in batch for r,_ in g):
        return False
    leads=[v[0][1] for v in batch]
    if [v['duration'] for v in leads]!=[F(1,4),F(1,4),F(1,2)]:
        return False
    if [v['start'] for v in leads]!=[leads[0]['start']+F(n,4) for n in range(3)]:
        return False
    notes=[v['node'] for v in leads]
    if [n.findtext("beam[@number='1']") for n in notes]!=['begin','continue','end']:
        return False
    if len({q.findtext('voice') for q in notes})!=1:
        return False
    for item in batch:
        events=[v for _,v in item]
        left,right=events[0],events[-1]
        if left['node'].find('chord') is not None or any(v['node'].find('chord') is None for v in events[1:]):
            return False
        if any((left['start'],left['duration'])!=(v['start'],v['duration']) for v in events[1:]):
            return False
        a,b=(pitch(v['node']) for v in (left,right))
        if a[:2]!=b[:2] or not (a[2] and b[2] and int(b[2])-int(a[2])==1):
            return False
    return True


def head_edge(group,above):
    return (median(int(r['x'])+int(r['w'])/2 for r,_ in group),
            min(int(r['y']) for r,_ in group) if above else max(int(r['y'])+int(r['h']) for r,_ in group))


def isolated_dot(frame,x,y,space):
    box=(round(x)-6,round(y)-5,round(x)+7,round(y)+6)
    if box[0]<0 or box[1]<0 or box[2]>frame.width or box[3]>frame.height:
        return None
    votes=[_dot_at_threshold(frame,box,t,space*.88) for t in (125,165)]
    if not all(votes) or max(abs(a-b) for a,b in zip(*votes))>1.25:
        return None
    cx,cy=(round(median(v[i] for v in votes)) for i in (0,1))
    if abs(cx-x)>1.9 or abs(cy-y)>1.9:
        return None
    return cx,cy


def component_at_center(patch,threshold):
    points=set(zip(*np.where(patch<threshold)))
    if not points:
        return None
    nearest=min(points,key=lambda p:(abs(p[0]-4)+abs(p[1]-4),p))
    if abs(nearest[0]-4)+abs(nearest[1]-4)>2:
        return None
    group=[nearest]
    points.remove(nearest)
    for y,x in group:
        near={(j,i) for j in range(y-1,y+2) for i in range(x-1,x+2)}&points
        points.difference_update(near)
        group.extend(near)
    ys,xs=zip(*group)
    if not 10<=len(group)<=33 or not 3<=max(xs)-min(xs)+1<=7 or not 3<=max(ys)-min(ys)+1<=7:
        return None
    return set(group)


def similarity(mask,other):
    scores=[]
    for t in (125,165):
        a,b=component_at_center(mask,t),component_at_center(other,t)
        if a is None or b is None:
            return None
        fits=[]
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                moved={(y+dy,x+dx) for y,x in b}
                fits.append(len(a&moved)/len(a|moved))
        scores.append(max(fits))
    return scores if min(scores)>=.71 else None


def inspect(batch,missing,links,staff,frame,bitmap):
    source=[]
    mark=None
    centers=[]
    side=None
    for i,g in enumerate(batch):
        note=g[0][1]['node']
        values=note.findall('notations/articulations/staccato')
        if i==missing:
            continue
        if len(values)!=1 or values[0].get('placement') not in ('above','below'):
            return None
        if side is not None and values[0].get('placement')!=side:
            return None
        side=values[0].get('placement')
        above=side=='above'
        matches=[q for q in links[g[0][0]['chord']] if q.get('shape')=='STACCATO' and
                 float(q.get('grade','0'))>=.68 and float(q.get('ctx-grade','0'))>=.86]
        if len(matches)!=1:
            return None
        r=matches[0]
        x,y,w,h=coordinate(r)
        if not 5<=w<=8 or not 4<=h<=8:
            return None
        px,py=head_edge(g,above)
        p=staff_lines(staff,px)
        if not p or abs(x+w/2-px)>3 or not .23*p[1]<=abs(y+h/2-py)<=1.05*p[1]:
            return None
        at=isolated_dot(frame,x+w/2,y+h/2,p[1])
        if not at:
            return None
        cx,cy=at
        centers.append([cx-px,cy-py])
        source.append((cx,cy,r.get('id')))
        mark=values[0]
    if len(source)!=2 or not mark.attrib.get('default-y'):
        return None
    if any(abs(a-b)>1.5 for a,b in zip(*centers)):
        return None
    gx,edge=head_edge(batch[missing],side=='above')
    data=staff_lines(staff,gx)
    if not data:
        return None
    _,space=data
    delta=[median(v[i] for v in centers) for i in (0,1)]
    wanted=isolated_dot(frame,gx+delta[0],edge+delta[1],space)
    if not wanted:
        return None
    wx,wy=wanted
    icon=bitmap[wy-4:wy+5,wx-4:wx+5]
    fits=[]
    for x,y,ident in source:
        ref=bitmap[y-4:y+5,x-4:x+5]
        match=similarity(icon,ref)
        if not match:
            return None
        fits.append(dict(native_donor=ident,center=[x,y],scores=match))
    dx,dy,_=source[-1]
    new_y=float(mark.get('default-y'))-10*(wy-dy)/space
    return dict(center=[wx,wy],proof=fits,side=side,default_y=round(new_y))


def recover(root,job):
    job=Path(job)
    receipt=job/'source-native-notation.json'
    raster_index=job/'source-tuplet-evidence/meter/source-meters.json'
    if not receipt.is_file() or not raster_index.is_file():
        return root,dict(state='insufficient_evidence',changes=[])
    config=json.loads(receipt.read_text('utf8'))
    original,xml=(Path(config[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(original)!=config.get('accepted_native_sha256'):
        raise ValueError('staccato native archive hash mismatch')
    rasters={str(s['unit']):s['image_sha256'] for s in json.loads(raster_index.read_text('utf8'))['source_images']}
    ledger=defaultdict(list)
    for r in rows(xml.with_name('note-ledger.tsv')):
        if r['implicit']=='false' and r['repeat_copy']=='false':
            ledger[r['part'],r['measure'],r['staff_number']].append(r)
    clone=deepcopy(root)
    ev,_=_score_events(clone)
    piano={p.get('id') for p in clone.findall('part') if p.find("measure/attributes/staves[.='2']") is not None}
    changes=[]; gcache={}; previous=None
    with ZipFile(original) as book:
        for (part,bar,staff_number),source in ledger.items():
            if part not in piano or staff_number not in ('1','2'):
                continue
            group=defaultdict(list)
            for r in sorted(source,key=lambda a:int(a['note_index'])):
                v=ev.get((part,bar,int(r['note_index'])))
                if v is None or v['node'].findtext('staff')!=staff_number:
                    group.clear()
                    break
                group[r['chord']].append((r,v))
            lane=list(group.values())
            for i in range(len(lane)-2):
                batch=lane[i:i+3]
                if not rhythmic_group(batch):
                    continue
                missing=[j for j,g in enumerate(batch) if g[0][1]['node'].find('notations/articulations/staccato') is None]
                if len(missing)!=1:
                    continue
                if not any(q.tag in ('tenuto','accent') for q in batch[0][0][1]['node'].findall('notations/articulations/*')):
                    continue
                m=missing[0]
                site={(r['unit'],r['system'],r['staff']) for batch_item in batch for r,_ in batch_item}
                if len(site)!=1:
                    continue
                unit,system,staff=site.pop()
                if (unit,system) not in gcache:
                    g=graph(book,unit,system)
                    n={nd.get('id'):nd for nd in g.findall('sig/inters/*') if nd.find('bounds') is not None}
                    l=defaultdict(list)
                    for e in g.findall('sig/relations/relation'):
                        if e.find('chord-articulation') is not None and e.get('target') in n:
                            l[e.get('source')].append(n[e.get('target')])
                    gcache[unit,system]=(g,n,l)
                g,n,l=gcache[unit,system]
                if not all(native_anchor(r,n) for group_item in batch for r,_ in group_item):
                    continue
                direction=g.find(f".//staff[@id='{staff}']")
                if direction is None:
                    continue
                if previous!=unit:
                    path=job/f'unit-{int(unit):03d}.png'
                    if sha(path)!=rasters.get(unit):
                        raise ValueError('staccato page hash mismatch')
                    with Image.open(path) as im:
                        page=im.convert('L')
                    bitmap=np.asarray(page)
                    previous=unit
                proof=inspect(batch,m,l,direction,page,bitmap)
                if not proof:
                    continue
                r,event=batch[m][0]
                node=event['node']
                attr=node.find('notations')
                if attr is None:
                    attr=E.Element('notations')
                    node.insert(next((i for i,c in enumerate(node) if c.tag in ('lyric','play','listen')),len(node)),attr)
                arts=attr.find('articulations')
                if arts is None:
                    arts=E.SubElement(attr,'articulations')
                E.SubElement(arts,'staccato',placement=proof['side'],**{'default-y':str(proof['default_y'])})
                changes.append(dict(part=part,measure=bar,staff=staff_number,note_index=int(r['note_index']),
                                    start=str(event['start']),physical=proof))
    return (clone if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)