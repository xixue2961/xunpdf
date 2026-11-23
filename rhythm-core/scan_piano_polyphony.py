"""Build piano repair plans with the offline image worker."""
import json
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile
import hashlib
import sys

import cv2
import numpy as np

from scan_whole_tremolos451 import scan as tremolos
from scan_upper_rest458 import scan as leading_rests
from scan_inner_heads459 import source_note_plan
from find_octave_span import box, octave_witness


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_graph(book, witness):
    unit, staff = witness['unit'], witness['staff']
    with zipfile.ZipFile(book) as archive:
        sheet=ET.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
    system=sheet.find(f"page/system[@id='{witness['system']}']")
    stack=system.find(f"stack[@id='{witness['measure']}']")
    part=next(p for p in system.findall('part') if p.find(f"staff[@id='{staff}']") is not None)
    measure=part.find(f"measure[@id='{witness['measure']}']")
    nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
    upper={k:nodes[k] for k in measure.findtext('head-chords','').split() if nodes[k].get('staff')==staff}
    members=defaultdict(list)
    for rel in system.findall('sig/relations/*'):
        if rel.get('source') in upper and rel.find('containment') is not None:
            members[rel.get('source')].append(nodes[rel.get('target')])
    return upper,members,int(sheet.find('scale/interline').get('main')),(int(stack.get('left')),int(stack.get('right')))


def img(src, unit):
    return cv2.imdecode(np.fromfile(src/f'unit-{int(unit):03d}.png',np.uint8),0)


def select(book, source, kind, previous=None):
    result=dict(book_sha256=sha(book))
    if kind=='tremolo':
        result['findings']=tremolos(book,source)
    elif kind=='voice':
        rests=leading_rests(book,source)
        result['candidates']=len(rests)
        if len(rests)==1 and len(rests[0]['templates'])==1:
            w=rests[0]
            upper,groups,space,_=native_graph(book,w)
            candidates=source_note_plan(img(source,w['unit']),[h for heads in groups.values() for h in heads],groups,space)
            if len(candidates)==3:
                result.update(witness=w,outer_ids=list(upper),notes=candidates)
    elif kind=='ottava':
        w=previous['witness']
        upper,_,space,ends=native_graph(book,w)
        outer=sorted((upper[k] for k in previous['outer_ids']),key=lambda n:box(n)[0])
        result['span']=octave_witness(img(source,w['unit']),ends,outer,space)
    else:
        raise ValueError('unknown piano pixel scan')
    return result


if __name__=='__main__':
    kind=sys.argv[1]
    book,source,out=map(Path,sys.argv[2:5])
    previous=json.loads(Path(sys.argv[5]).read_text(encoding='utf8')) if len(sys.argv)>5 else None
    result=select(book,source,kind,previous)
    with out.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
