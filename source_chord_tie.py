
"""Recover an upper chord tie from its source image and a native lower tie."""
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile
import csv
import hashlib
import json
import xml.etree.ElementTree as ET

import cv2
import numpy as np


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def dig(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def coordinates(head):
    box = head.find('bounds')
    return [int(box.get(k)) for k in 'xywh']


def staff_lines(staff, x):
    lines = []
    for line in staff.findall('lines/line'):
        points = sorted((float(p.get('x')),float(p.get('y'))) for p in line.findall('point'))
        positions, heights = zip(*points, strict=True)
        lines.append(np.interp(x,positions,heights))
    return np.array(lines)


def arc_witness(image, staff, lower_curve, upper, lower):
    sample = np.linspace(.13,.87,51)
    weights = np.array([(1-sample)**3,3*(1-sample)**2*sample,3*(1-sample)*sample**2,sample**3]).T
    x = weights @ np.array([float(lower_curve.get(key)) for key in ('x1','ctrlx1','ctrlx2','x2')])
    lower_y = weights @ np.array([float(lower_curve.get(key)) for key in ('y1','ctrly1','ctrly2','y2')])
    head_y = lambda box: box[1]+box[3]/2
    upper_y = np.linspace(*map(head_y,upper),len(x))
    beneath_y = np.linspace(*map(head_y,lower),len(x))
    mirrored = upper_y+beneath_y-lower_y
    gap = float(np.median(np.diff(staff_lines(staff,x),axis=0)))
    if not 10 <= gap <= 30 or np.mean(mirrored[[0,-1]])-mirrored[len(x)//2] < .27*gap:
        return None

    def ink_support(xs, ys):
        cols = np.rint(xs).astype(int)
        centers = np.rint(ys).astype(int)
        all_rows = centers[None,:]+np.array([-1,0,1])[:,None]
        lines = staff_lines(staff,cols)
        clear = (np.abs(all_rows[None,:,:]-lines[:,None,:])>2.5).all(axis=0)
        if (cols.min()<0 or cols.max()>=image.shape[1] or
            all_rows.min()<0 or all_rows.max()>=image.shape[0]):
            return None
        pixels = image[all_rows,cols]
        return [[float(piece.mean()) for piece in np.array_split(((pixels<t)&clear).any(axis=0),5)]
                for t in (160,208)]

    evidence = []
    for dx in range(-round(gap*.39),round(gap*.39)+1):
        for dy in range(-round(gap*.28),round(gap*.28)+1):
            xs, ys = x+dx, mirrored+dy
            arcs = ink_support(xs,ys)
            if arcs is None or min(map(min,arcs))<.82:
                continue
            straight = ink_support(xs,np.linspace(ys[0],ys[-1],len(xs)))
            if straight is None or min(straight[1][1:4])>.38:
                continue
            quality = min(map(min,arcs))
            evidence.append((quality,abs(dx)+abs(dy),dict(
                overlap_by_band=arcs,straight_edge_overlap=straight,translation=[dx,dy],
                arc_extent=[round(float(xs.min())),round(float(ys.min())),
                            round(float(xs.max())),round(float(ys.max()))],
                staff_space=gap)))
    return min(evidence,key=lambda hit:(-hit[0],hit[1]))[2] if evidence else None


def chord_neighbors(notes, positions):
    if positions != list(range(positions[0],positions[-1]+1)) or len(positions)!=2:
        return None
    start = positions[0]
    selection=notes[start:start+4]
    if len(selection)!=4 or [n.find('chord') is not None for n in selection]!=[False,True,False,True]:
        return None
    pitch = lambda note: tuple(note.findtext('pitch/'+k) for k in ('step','alter','octave'))
    low,high,next_low,next_high=selection
    if (pitch(low)!=pitch(next_low) or pitch(high)!=pitch(next_high)
        or not all(n.findtext('type')=='eighth' for n in selection[:2])
        or not all(n.findtext('type')=='half' for n in selection[2:])
        or low.find("tie[@type='start']") is None or next_low.find("tie[@type='stop']") is None
        or any(n.find('tie') is not None or n.find('notations/tied') is not None for n in (high,next_high))):
        return None
    return selection


def add_tie(note, mode):
    tie=ET.Element('tie',type=mode)
    at=list(note).index(note.find('duration'))+1
    note.insert(at,tie)
    notation=note.find('notations')
    if notation is None:
        notation=ET.Element('notations')
        index=next((i for i,n in enumerate(note) if n.tag in ('lyric','play','listen')),len(note))
        note.insert(index,notation)
    notation.insert(0,ET.Element('tied',type=mode,**({'orientation':'over'} if mode=='start' else {})))


def locate_native(book, ledger, change):
    with ledger.open(encoding='utf8') as stream:
        rows = [row for row in csv.DictReader(stream,delimiter='\t')
                if row['repeat_copy']=='false' and row['part']==change['part']
                and row['measure']==change['measure']
                and int(row['note_index']) in range(change['indices'][0],change['indices'][0]+4)]
    rows.sort(key=lambda r:int(r['note_index']))
    systems={(int(r['unit']),r['system'],r['staff'],r['source_sha256']) for r in rows}
    if len(rows)!=4 or len(systems)!=1:
        return None
    unit,system_id,staff_id,source_sha=next(iter(systems))
    with ZipFile(book) as zip_file:
        system=ET.fromstring(zip_file.read(f'sheet#{unit}/sheet#{unit}.xml')).find(f"page/system[@id='{system_id}']")
    staff=system.find(f"part/staff[@id='{staff_id}']")
    if staff is None or len(staff.findall('lines/line'))!=5:
        return None
    heads=[system.find(f"sig/inters/head[@id='{row['head']}']") for row in rows]
    if any(head is None for head in heads):
        return None
    low,high,next_low,next_high=map(coordinates,heads)
    if high[1]>=low[1] or next_high[1]>=next_low[1]:
        return None
    links={}
    for relation in system.findall('sig/relations/relation'):
        link=relation.find('slur-head')
        if link is not None:
            links.setdefault(relation.get('source'),{}).setdefault(link.get('side'),[]).append(relation.get('target'))
    pair=dict(LEFT=[rows[0]['head']],RIGHT=[rows[2]['head']])
    anchors=[slur for slur in system.findall('sig/inters/slur')
             if slur.get('tie')=='true' and slur.get('above')=='false' and links.get(slur.get('id'))==pair]
    if len(anchors)!=1 or anchors[0].find('curve') is None:
        return None
    return unit,source_sha,staff,anchors[0].find('curve'),(high,next_high),(low,next_low)


def repair(root, book, ledger, source, changes):
    fixed=deepcopy(root)
    applied=[]
    for change in changes:
        part=fixed.find(f"part[@id='{change['part']}']")
        measure=part.findall('measure')[change['physical_measure']-1]
        if measure.get('number')!=change['measure']:
            continue
        selection=chord_neighbors(measure.findall('note'),change['indices'])
        located=locate_native(book,ledger,change)
        if not selection or not located:
            continue
        unit,sha,staff,curve,upper,lower=located
        page=source/f'unit-{unit:03d}.png'
        if dig(page)!=sha:
            raise ValueError('Source page has changed after exporting note addresses')
        image=cv2.imdecode(np.fromfile(page,np.uint8),0)
        if image is None:
            raise ValueError('Source raster is unreadable')
        proof=arc_witness(image,staff,curve,upper,lower)
        if not proof:
            continue
        add_tie(selection[1],'start')
        add_tie(selection[3],'stop')
        applied.append(dict(change=change, proof=proof,
                            anchored_unprinted_head_indices=[change['indices'][1],change['indices'][1]+2]))
    return (fixed if applied else root),applied

