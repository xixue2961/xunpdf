
"""Exclude a phantom pitched lane only when the raw score shows a repeated boxed sign."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from zipfile import ZipFile
import csv
import hashlib
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from source_long_tuplet_context import clocks
from source_vocal_candidates import phantom_lane
from source_lyric_roles import _score_events, _music_signature


def bounds(node):
    box=node.find('bounds')
    return tuple(int(box.get(k)) for k in 'xywh')


def staff_top(staff,x):
    line=staff.find('lines/line')
    points=sorted((float(p.get('x')),float(p.get('y'))) for p in line.findall('point'))
    xs,ys=zip(*points,strict=True)
    return float(np.interp(x,xs,ys))


def staff_space(staff):
    y=[np.median([float(p.get('y')) for p in line.findall('point')])
       for line in staff.findall('lines/line')]
    return float(np.median(np.diff(y))) if len(y)==5 else 0


def hand_frame(image, center, top, space, threshold):
    x0,x1=round(center-2.1*space),round(center+2.1*space)
    y0,y1=round(top-4.4*space),round(top+.20*space)
    if x0<0 or y0<0 or x1>image.shape[1] or y1>image.shape[0]:
        return []
    mask=(image[y0:y1,x0:x1]<threshold).astype('uint8')
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    matches=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if not (1.65*space<=w<=2.4*space and 2.85*space<=h<=3.65*space
            and abs(x0+x+w/2-center)<=.55*space and abs(y0+y+h-top)<=.58*space):
            continue
        frame=mask[y:y+h,x:x+w]
        offset=round(.28*space)
        bands=(frame[:offset,offset:-offset],frame[-offset:,offset:-offset],
               frame[offset:-offset,:offset].T,frame[offset:-offset,-offset:].T)
        closure=[float(b.any(axis=0).mean()) for b in bands]
        if min(closure)<.86:
            continue
        inner=frame[offset:-offset,offset:-offset]
        _,_,stats,_=cv2.connectedComponentsWithStats(inner)
        px,py,pw,ph,area=max(stats[1:],key=lambda a:a[-1],default=(0,0,0,0,0))
        if not (1.0*space<=pw<=1.52*space and 1.4*space<=ph<=2.35*space and .44<=area/(pw*ph)<=.78):
            continue
        symbol=inner[py:py+ph,px:px+pw]
        runs=(np.diff(np.pad(symbol.astype('int16'),((0,0),(1,1))),axis=1)==1).sum(axis=1)
        if (np.count_nonzero(runs[:round(.60*ph)]>=2)<4
            or (threshold==160 and np.count_nonzero(runs[:round(.60*ph)]>=3)<1)
            or np.count_nonzero(runs[round(.62*ph):]==1)<round(.22*ph)):
            continue
        silhouette=cv2.resize(symbol,(24,36),interpolation=cv2.INTER_NEAREST).astype(bool)
        matches.append(dict(box=(x+x0,y+y0,w,h),closure=closure,mask=silhouette))
    return matches


def similarity(a,b):
    return float(np.logical_and(a,b).sum()/np.logical_or(a,b).sum())


def frame_witness(image,system,staff,head,stem):
    x,y,w,h=bounds(head)
    center=x+w*.25
    reference=[]
    for threshold in (160,208):
        frames=hand_frame(image,center,staff_top(staff,center),staff_space(staff),threshold)
        if len(frames)!=1:
            return None
        match=frames[0]
        a,b,fw,fh=match['box']
        stem_x=float(stem.find('median/p1').get('x'))
        stem_start=float(stem.find('median/p1').get('y'))
        if (abs(stem_x-(a+fw-1))>4 or abs(stem_start-b)>4
            or abs(y+h-(b+fh))>4 or not a<=x<=a+fw-12):
            return None
        peers=[]
        for peer in system.findall('part/staff'):
            if peer.get('id')==staff.get('id'):
                continue
            gap=staff_space(peer)
            if not 10<=gap<=30:
                continue
            eligible=hand_frame(image,center,staff_top(peer,center),gap,threshold)
            if len(eligible)==1:
                other=eligible[0]
                score=similarity(match['mask'],other['mask'])
                if score>=.78:
                    peers.append(dict(staff=peer.get('id'),box=other['box'],overlap=score))
        if len(peers)<2:
            return None
        reference.append(dict(threshold=threshold,box=match['box'],frame_closure=match['closure'],peers=peers))
    return reference


def repair(root,native,book,ledger,source):
    source_events,_=_score_events(native)
    actual_events,_=_score_events(root)
    by_note=None
    result=deepcopy(root)
    pages={}
    sheets={}
    changes=[]
    with ZipFile(book) as archive:
        for part in result.findall('part'):
            if part.find('.//lyric') is None:
                continue
            measures=part.findall('measure')
            numbers=Counter(m.get('number') for m in measures)
            for index,(bar,(divisions,capacity)) in enumerate(zip(measures,clocks(part),strict=True),1):
                if numbers[bar.get('number')]!=1:
                    continue
                match=phantom_lane(bar,divisions,capacity)
                if match is None:
                    continue
                nodes, note_index=match
                key=(part.get('id'),bar.get('number'),note_index)
                if by_note is None:
                    by_note=defaultdict(list)
                    with ledger.open(encoding='utf8') as stream:
                        for row in csv.DictReader(stream,delimiter='\t'):
                            if row['repeat_copy']=='false':
                                by_note[row['part'],row['measure'],int(row['note_index'])].append(row)
                rows=by_note[key]
                if (len(rows)!=1 or key not in source_events or key not in actual_events
                    or _music_signature(source_events[key])!=_music_signature(actual_events[key])
                    or source_events[key]['node'].get('default-x')!=actual_events[key]['node'].get('default-x')):
                    continue
                row=rows[0]
                unit=int(row['unit'])
                if unit not in sheets:
                    sheets[unit]=ET.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
                system=sheets[unit].find(f"page/system[@id='{row['system']}']")
                staff=system.find(f"part/staff[@id='{row['staff']}']")
                head=system.find(f"sig/inters/head[@id='{row['head']}']")
                if (staff is None or head is None or head.get('shape')!='NOTEHEAD_VOID'
                    or float(head.get('grade','1'))>.40 or not 10<=staff_space(staff)<=30):
                    continue
                links=[rel.get('target') for rel in system.findall('sig/relations/relation')
                       if rel.get('source')==row['head'] and rel.find('head-stem') is not None]
                if len(links)!=1:
                    continue
                stem=system.find(f"sig/inters/stem[@id='{links[0]}']")
                if stem is None or stem.find('median/p1') is None:
                    continue
                if unit not in pages:
                    data=(source/f'unit-{unit:03d}.png').read_bytes()
                    pages[unit]=(hashlib.sha256(data).hexdigest(),cv2.imdecode(np.frombuffer(data,np.uint8),0))
                digest,image=pages[unit]
                if row['source_sha256']!=digest or image is None:
                    continue
                witness=frame_witness(image,system,staff,head,stem)
                if witness is None:
                    continue
                for node in nodes:
                    bar.remove(node)
                changes.append(dict(part=key[0],measure=key[1],physical_measure=index,
                                    note_index=note_index,source_head=row['head'],
                                    source_stem=links[0],source_unit=unit,source_system=row['system'],
                                    source_staff=row['staff'],quarters_after=str(capacity),witness=witness))
    return (result if changes else root),changes

