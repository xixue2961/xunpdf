
"""Distinguish an octave-shift dash from an articulation in an original image."""
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from statistics import median
from zipfile import ZipFile
import csv
import hashlib
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from source_lyric_roles import _music_signature, _score_events


def bounds(node):
    box=node.find('bounds')
    return [int(box.get(k)) for k in 'xywh']


def staff_spacing(staff):
    if staff is None:
        return 0
    ys=[median(float(p.get('y')) for p in line.findall('point')) for line in staff.findall('lines/line')]
    return median(b-a for a,b in zip(ys,ys[1:])) if len(ys)==5 else 0


def components(image, rectangle, threshold):
    left,top,right,bottom=rectangle
    if left<0 or top<0 or right>image.shape[1] or bottom>image.shape[0] or left>=right or top>=bottom:
        return []
    binary=(image[top:bottom,left:right]<threshold).astype('uint8')
    _,_,stats,_=cv2.connectedComponentsWithStats(binary)
    return [[int(x+left),int(y+top),int(w),int(h),int(area)] for x,y,w,h,area in stats[1:]]


def dash_train(image,line,mark,space,threshold):
    left,y0,length,_=bounds(line)
    if length<5*space:
        return []
    start,end=line.find('line/p1'),line.find('line/p2')
    if start is None or end is None or abs(float(start.get('y'))-float(end.get('y')))>.28*space:
        return []
    radius=round(.35*space)
    hits=[]
    for x,y,w,h,area in components(image,(left,y0-radius,left+length,y0+radius+1),threshold):
        if (.39*space<=w<=.79*space and .12*space<=h<=.36*space
            and abs(y+h/2-y0)<=.13*space and area>=.68*w*h):
            hits.append([x,y,w,h])
    runs=[[]]
    for h in sorted(hits):
        if runs[-1]:
            prev=runs[-1][-1]
            step=h[0]+h[2]/2-prev[0]-prev[2]/2
            if not .86*space<=step<=1.34*space:
                runs.append([])
        runs[-1].append(h)
    for chain in runs:
        if len(chain)<5:
            continue
        steps=[b[0]+b[2]/2-a[0]-a[2]/2 for a,b in zip(chain,chain[1:])]
        if max(steps)-min(steps)>.16*space:
            continue
        mx,my,mw,mh=bounds(mark)
        if sum(abs(x+w/2-(mx+mw/2))<.18*space and abs(y+h/2-(my+mh/2))<.16*space
               for x,y,w,h in chain)==1:
            return chain
    return []


def tenuto_witness(image,chord,nearby,space,threshold):
    samples=[]
    for neighbor,mark in nearby:
        hx,hy,*_=bounds(neighbor)
        x,y,w,h=bounds(mark)
        hits=[c for c in components(image,(x-1,y-1,x+w+1,y+h+1),threshold)
              if c[2]>=1.19*space and .10*space<=c[3]<=.38*space and c[4]>=.73*c[2]*c[3]]
        if len(hits)!=1:
            return None
        px,py,pw,ph,_=hits[0]
        samples.append([px-hx,py-hy,pw,ph])
    dx,dy,width,height=[median(values) for values in zip(*samples,strict=True)]
    x,y,*_=bounds(chord)
    left,top=round(x+dx),round(y+dy)
    sx,sy=round(space*.21),round(space*.16)
    candidates=components(image,(left-sx,top-sy,left+round(width)+sx,top+round(height)+sy),threshold)
    hits=[c for c in candidates if abs(c[0]-left)<=sx and abs(c[1]-top)<=sy
          and abs(c[2]-width)<=.17*space and abs(c[3]-height)<=.12*space
          and c[4]>=.78*c[2]*c[3]]
    return hits[0] if len(hits)==1 else None


def read_ink(image,line,dot,chord,nearby,space):
    evidence=[]
    for threshold in (160,208):
        dash=dash_train(image,line,dot,space,threshold)
        tenuto=tenuto_witness(image,chord,nearby,space,threshold)
        if not dash or tenuto is None or tenuto[1]-bounds(dot)[1]<space:
            return []
        evidence.append(dict(threshold=threshold,dash_boxes=dash,tenuto_box=tenuto))
    if any(abs(a-b)>2 for a,b in zip(evidence[0]['tenuto_box'][:4],evidence[1]['tenuto_box'][:4])):
        return []
    return evidence


def directions(bar,note):
    staff=note.findtext('staff','1')
    active={}
    for child in bar:
        if child is note:
            break
        if child.tag!='direction' or child.findtext('staff','1')!=staff:
            continue
        for shift in child.findall('direction-type/octave-shift'):
            number=shift.get('number','1')
            if shift.get('type')=='stop':
                active.pop(number,None)
            elif shift.get('type')=='down' and shift.get('size','8')=='8' and shift.get('default-y'):
                active[number]=shift
    return list(active.values())


def notation_graph(system):
    nodes={v.get('id'):v for v in system.findall('sig/inters/*')}
    articulation=defaultdict(list)
    links=defaultdict(dict)
    neighbors=defaultdict(dict)
    for rel in system.findall('sig/relations/relation'):
        a,b=rel.get('source'),rel.get('target')
        shift=rel.find('octave-shift-chord')
        if shift is not None:
            links[a].setdefault(shift.get('side'),[]).append(b)
        elif rel.find('chord-articulation') is not None and b in nodes:
            articulation[a].append(nodes[b])
        elif rel.find('next-in-voice') is not None:
            neighbors[a].setdefault('next',[]).append(b)
            neighbors[b].setdefault('prev',[]).append(a)
    return nodes,articulation,links,neighbors


def source_evidence(system,chord_id):
    nodes,articulations,links,neighbors=notation_graph(system)
    chord=nodes.get(chord_id)
    if chord is None:
        return None
    staff=system.find(f"part/staff[@id='{chord.get('staff')}']")
    space=staff_spacing(staff)
    if not 11<=space<=29:
        return None
    marks=[n for n in articulations[chord_id] if n.get('shape')=='STACCATO']
    if len(marks)!=1:
        return None
    nearby=[]
    for side in ('prev','next'):
        ids=neighbors[chord_id].get(side,[])
        if len(ids)!=1:
            return None
        neighbor=nodes[ids[0]]
        if neighbor.get('staff')!=chord.get('staff') or abs(bounds(neighbor)[0]-bounds(chord)[0])>6*space:
            return None
        tenutos=[a for a in articulations[ids[0]] if a.get('shape')=='TENUTO']
        if len(tenutos)!=1 or not 0<bounds(neighbor)[1]-bounds(tenutos[0])[1]<1.2*space:
            return None
        nearby.append((neighbor,tenutos[0]))
    lines=[]
    dx,dy,_,_=bounds(marks[0])
    for line in system.findall('sig/inters/octave-shift'):
        x,y,w,_=bounds(line)
        pairs=links[line.get('id')]
        if (line.get('staff')==chord.get('staff') and line.get('kind')=='ALTA'
            and x<dx<x+w and abs(dy-y)<=.24*space
            and all(len(pairs.get(side,[]))==1 for side in ('LEFT','RIGHT'))):
            lo,hi=(bounds(nodes[pairs[s][0]])[0] for s in ('LEFT','RIGHT'))
            if lo<bounds(chord)[0]<hi:
                lines.append(line)
    return (lines[0],marks[0],chord,nearby,space) if len(lines)==1 else None


def repair(root,native,book,source,ledger):
    native_events,_=_score_events(native)
    actual_events,_=_score_events(root)
    index=defaultdict(list)
    with ledger.open(encoding='utf8') as stream:
        for row in csv.DictReader(stream,delimiter='\t'):
            if row['repeat_copy']=='false':
                index[row['part'],row['measure'],int(row['note_index'])].append(row)
    candidate=deepcopy(root)
    report=[]
    pages={}
    systems={}
    with ZipFile(book) as archive:
        for part in candidate.findall('part'):
            measures=part.findall('measure')
            counts=Counter(m.get('number') for m in measures)
            for physical,bar in enumerate(measures,1):
                number=bar.get('number')
                if counts[number]!=1 or bar.find('direction/direction-type/octave-shift') is None:
                    continue
                for i,note in enumerate(bar.findall('note')):
                    mark=note.findall('notations/articulations/staccato')
                    shifts=directions(bar,note)
                    if len(mark)!=1 or len(shifts)!=1 or mark[0].get('placement')!='above':
                        continue
                    key=(part.get('id'),number,i)
                    rows=index[key]
                    if (len(rows)!=1 or key not in native_events or key not in actual_events
                        or _music_signature(actual_events[key])!=_music_signature(native_events[key])
                        or actual_events[key]['node'].get('default-x')!=native_events[key]['node'].get('default-x')):
                        continue
                    addr=rows[0]
                    unit=int(addr['unit'])
                    page=source/f'unit-{unit:03d}.png'
                    if unit not in pages:
                        data=page.read_bytes()
                        pages[unit]=(hashlib.sha256(data).hexdigest(),cv2.imdecode(np.frombuffer(data,np.uint8),0))
                    sha,image=pages[unit]
                    if sha!=addr['source_sha256'] or image is None:
                        continue
                    if unit not in systems:
                        systems[unit]=ET.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
                    system=systems[unit].find(f"page/system[@id='{addr['system']}']")
                    data=source_evidence(system,addr['chord'])
                    if data is None:
                        continue
                    evidence=read_ink(image,*data)
                    if not evidence:
                        continue
                    line,dot,_,_,space=data
                    art=next(a for a in note.findall('notations/articulations') if mark[0] in list(a))
                    if len(art.findall('tenuto'))>1:
                        continue
                    had=art.find('tenuto') is not None
                    if not had:
                        _,y,_,height,_=evidence[1]['tenuto_box']
                        ordinate=round(float(shifts[0].get('default-y'))-10*(y+height/2-bounds(line)[1])/space)
                        art.insert(list(art).index(mark[0]),ET.Element('tenuto',placement='above',**{'default-y':str(ordinate)}))
                    art.remove(mark[0])
                    report.append(dict(part=key[0],measure=number,physical_measure=physical,note_index=i,
                                       unit=unit,system=addr['system'],chord=addr['chord'],
                                       dot_id=dot.get('id'),octave_line_id=line.get('id'),retained_existing_tenuto=had,
                                       ink=evidence))
    return (candidate if report else root),report




def main():
    import argparse
    import json
    from source_tempo_context import _score

    parser=argparse.ArgumentParser()
    for name in ('input','control','book','source','ledger','work'):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    baseline=ET.parse(args.input).getroot()
    fixed,changes=repair(baseline,_score(args.control),args.book,args.source,args.ledger)
    if changes:
        ET.ElementTree(fixed).write(args.work/'output.musicxml',encoding='utf-8',xml_declaration=True)
    with (args.work/'changes.json').open('x',encoding='utf8') as stream:
        json.dump(dict(state='restored' if changes else 'no_supported_change',changes=changes),
                  stream,ensure_ascii=False,indent=2)


if __name__=='__main__':
    main()

