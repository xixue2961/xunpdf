"""Retire emitted false singleton rest endings only when the native image shows beams."""
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import copy
import csv
import hashlib
import json
import sys

import cv2
import numpy as np
from source_false_multiple_rest import continuous_ink


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stem_contacts(marker, chords, stems, nodes):
    first,last=marker.findall('median/*')
    x0,y0,x1,y1=[float(p.get(axis)) for p in (first,last) for axis in 'xy']
    h=float(marker.get('height'))
    if x1<=x0 or not 8<=h<=16:
        return []
    hits=[]
    for chord in chords:
        if chord not in stems:
            continue
        stem=nodes[stems[chord]]
        p1,p2=stem.findall('median/*')
        x=float(p1.get('x'))
        if not x0-5<=x<=x1+5:
            continue
        y=y0+(x-x0)*(y1-y0)/(x1-x0)
        ends=sorted(float(p.get('y')) for p in (p1,p2))
        nearest=min(abs(y-end) for end in ends)
        direction=1 if abs(y-ends[0])<abs(y-ends[1]) else -1
        if nearest<=h*.8 and ends[1]-ends[0]>=2*h:
            hits.append((x,chord,direction))
    hits.sort()
    if not 2<=len(hits)<=6 or len({v[2] for v in hits})!=1 or abs(hits[0][0]-x0)>5 or abs(hits[-1][0]-x1)>5:
        return []
    return hits


def scan(book, source, ledger):
    keys=defaultdict(set)
    with Path(ledger).open(encoding='utf8',newline='') as f:
        for row in csv.DictReader(f,delimiter='\t'):
            if row['repeat_copy']=='false':
                key=(int(row['unit']),row['system'],row['staff'],row['chord'])
                keys[key].add((row['part'],row['measure'],row['source_sha256']))
    with ZipFile(book) as archive:
        for name in archive.namelist():
            if not (name.startswith('sheet#') and name.endswith('.xml')):
                continue
            unit=int(name.split('/')[0][6:])
            path=source/f'unit-{unit:03d}.png'
            image=None
            for system in E.fromstring(archive.read(name)).findall('page/system'):
                system_id=system.get('id')
                staff_parts={s.get('id'):(s,p) for p in system.findall('part') for s in p.findall('staff')}
                nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
                edges=system.findall('sig/relations/relation')
                in_relation={e.get(a) for e in edges for a in ('source','target')}
                stems={e.get('source'):e.get('target') for e in edges if e.find('chord-stem') is not None}
                for stack in system.findall("stack[@special='MULTI_REST']"):
                    stack_id=stack.get('id')
                    lo,hi=int(stack.get('left')),int(stack.get('right'))
                    marks=[n for n in nodes.values() if n.tag in ('multiple-rest','measure-count')
                           and n.find('bounds') is not None and lo<=int(n.find('bounds').get('x'))<hi]
                    if not marks or any(n.tag!='multiple-rest' or n.get('abnormal')!='true'
                                        or n.get('id') in in_relation for n in marks):
                        continue
                    all_labels=set()
                    evidence=[]
                    for marker in marks:
                        staff_id=marker.get('staff')
                        if staff_id not in staff_parts:
                            break
                        staff,part=staff_parts[staff_id]
                        measure=part.find(f"measure[@id='{stack_id}']")
                        if measure is None:
                            break
                        chords=[c for c in measure.findtext('head-chords','').split()
                                if nodes[c].get('staff')==staff_id]
                        hits=stem_contacts(marker,chords,stems,nodes)
                        if not hits:
                            break
                        if image is None:
                            source_sha=sha(path)
                            image=cv2.imdecode(np.fromfile(path,np.uint8),0)
                        pixels=continuous_ink(image,marker,staff,[v[0] for v in hits])
                        if not pixels:
                            break
                        labels={label for _,c,_ in hits for label in keys[(unit,system_id,staff_id,c)]}
                        if not labels or any(digest!=source_sha for _,_,digest in labels):
                            break
                        all_labels.update((p,m) for p,m,_ in labels)
                        evidence.append(dict(marker=marker.get('id'),staff=staff_id,
                                             chord_stems=hits,ink=pixels))
                    else:
                        if len({bar for _,bar in all_labels})==1:
                            yield dict(unit=unit,system=system_id,stack=stack_id,source_sha256=source_sha,
                                       labels=sorted(all_labels),markers=evidence)


def phantom_ending(measure):
    children=list(measure)
    if len(children)<2:
        return ()
    attr,note=children[-2:]
    if attr.tag!='attributes' or [n.tag for n in attr]!=['measure-style']:
        return ()
    style,=attr
    if style.attrib or [n.tag for n in style]!=['multiple-rest']:
        return ()
    multi,=style
    if multi.attrib or (multi.text or '').strip()!='1':
        return ()
    if note.tag!='note' or note.attrib!={'print-object':'no'} or [n.tag for n in note]!=['rest','duration']:
        return ()
    if note.find("rest[@measure='yes']") is None:
        return ()
    return attr,note


def apply(root, proofs):
    fixed=copy.deepcopy(root)
    parts={p.get('id'):p.findall('measure') for p in fixed.findall('part')}
    changes=[]
    for proof in proofs:
        anchors=[]
        for part,number in proof['labels']:
            positions=[i for i,m in enumerate(parts.get(part,())) if m.get('number')==number and phantom_ending(m)]
            if len(positions)!=1:
                break
            anchors.append(positions[0])
        else:
            if not anchors or len(set(anchors))!=1:
                continue
            index=anchors[0]
            if any(len(bars)<=index for bars in parts.values()):
                continue
            group=[bars[index] for bars in parts.values()]
            if not any(m.find('note/pitch') is not None for m in group):
                continue
            extras=[phantom_ending(m) for m in group]
            if not all(extras):
                continue
            if len({m.get('number') for m in group})!=1:
                continue
            for m,tail in zip(group,extras):
                for child in tail:
                    m.remove(child)
            changes.append(dict(physical_measure=index+1,parts=list(parts),source=proof))
    return fixed,changes


if __name__=='__main__':
    book,source,ledger,xml,dest=map(Path,sys.argv[1:])
    before={p:sha(p) for p in (book,ledger,xml)}
    proofs=list(scan(book,source,ledger))
    fixed,changes=apply(E.parse(xml).getroot(),proofs)
    assert all(sha(p)==digest for p,digest in before.items())
    dest.mkdir()
    with (dest/'score.musicxml').open('xb') as f:
        E.ElementTree(fixed).write(f,encoding='UTF-8',xml_declaration=True)
    with (dest/'proof.json').open('x',encoding='utf8') as f:
        json.dump(dict(image_proofs=proofs,changes=changes),f,ensure_ascii=False,indent=2)
    print('removed at physical measures',[item['physical_measure'] for item in changes],flush=True)
