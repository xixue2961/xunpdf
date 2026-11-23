
"""Restore wide, three-stroke octave tremolos in an independent bass lane."""

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import engrave, inherited_context
from source_three_stroke_tremolo_ink_v3 import find_strokes
from source_vocal_breath_column import rows
from source_vocal_split_clock import native_anchor, pitch
from source_whole_tremolo_heads import isolated_ink, similarity


def print_evidence(image, staff, heads):
    lines=[[(float(p.get('x')),float(p.get('y'))) for p in line.findall('point')]
           for line in staff.findall('lines/line')]
    if len(lines)!=5 or any(len(line)<2 for line in lines):
        return None
    xs=[]
    geometry=[]
    masks=[]
    for row,node in heads:
        x,y,w,h=(int(row[k]) for k in ('x','y','w','h'))
        if not (25<=w<=36 and 14<=h<=22):
            return None
        x+=w//2
        y+=h//2
        heights=[float(np.interp(x,*zip(*line))) for line in lines]
        space=(heights[-1]-heights[0])/4
        if not 11<=space<=24 or any(abs(b-a-space)>1.5 for a,b in zip(heights,heights[1:])):
            return None
        dy=y-heights[2]
        staff_step=round(2*dy/space)
        if abs(staff_step*space/2-dy)>.17*space or node.get('pitch')!=str(staff_step):
            return None
        mask=isolated_ink(image,x,y,staff_step,space)
        if mask is None:
            return None
        masks.append(mask)
        xs.append(x)
        geometry.append((staff_step,space))
    dx=xs[1]-xs[0]
    step,space=geometry[0]
    if not 11*space<dx<=17.5*space or step-geometry[1][0]!=7 or abs(space-geometry[1][1])>1:
        return None
    shape=similarity(*masks)
    if shape<.78:
        return None
    middle=sum(xs)/2
    results=[]
    for span in (8.4*space, 10.2*space):
        witness=find_strokes(image,staff,middle-span/2,middle+span/2,-1)
        if not witness:
            return None
        results.append(witness)
    for a,b in zip(results[0]['thresholds'].values(),results[1]['thresholds'].values()):
        if abs(a['slope']-b['slope'])>.07 or max(abs(k-v) for k,v in zip(a['centers'],b['centers']))>.20*space:
            return None
    return dict(written_head_similarity=shape,printed_support=results,head_centers=xs)


def lanes(measure, current, key):
    components=[v for v in measure if v.tag in ('note','backup','forward')]
    if len(components)<5:
        return None
    a,backup,forward,b=components[-4:]
    if [c.tag for c in (a,backup,forward,b)]!=['note','backup','forward','note'] or components[-5].tag!='backup':
        return None
    if [n for n in measure.findall('note') if n.findtext('staff','1')=='2']!=[a,b]:
        return None
    forbidden=('notations','beam','tie','chord','stem','cue','lyric','grace','dot','time-modification','accidental')
    if any(n.findtext('type')!='whole' or n.find('pitch') is None or any(n.find(k) is not None for k in forbidden) for n in (a,b)):
        return None
    upper=a.findtext('voice')
    lower=b.findtext('voice')
    if (upper==lower or not upper or forward.findtext('voice')!=lower or forward.findtext('staff')!='2'
            or (duration:=int(a.findtext('duration'))) %2 or b.findtext('duration')!=str(duration)
            or backup.findtext('duration')!=str(duration)):
        return None
    if pitch(a)[:2]!=pitch(b)[:2] or int(pitch(a)[2])+1!=int(pitch(b)[2]):
        return None
    ns=measure.findall('note')
    entries=[(*key,ns.index(n)) for n in (a,b)]
    events=[current[k] for k in entries]
    if [c['duration'] for c in events]!=[4,4] or events[0]['start']!=0 or events[1]['start'] not in (2,2.5):
        return None
    tids={n.findtext('staff','1') for n in measure.findall('direction')}
    if '2' in tids:
        return None
    return a,b,backup,forward,duration,entries


def recover(root, job):
    job=Path(job)
    descriptor=job/'source-native-notation.json'
    raw=job/'raw/input.omr'
    empty=dict(state='no_changes',changes=[])
    if not descriptor.is_file() or not raw.is_file():
        return root,dict(empty,state='insufficient_evidence')
    meta=json.loads(descriptor.read_text('utf8'))
    archive,native=(Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(archive)!=meta.get('accepted_native_sha256'):
        raise ValueError('wide octave tremolo: native book hash mismatch')
    index=defaultdict(list)
    for r in rows(native.with_name('note-ledger.tsv')):
        index[r['part'],r['measure'],int(r['note_index'])].append(r)
    fixed=deepcopy(root)
    current,_=_score_events(fixed)
    base,_=_score_events(score(native))
    changes=[]
    with ZipFile(archive) as verified,ZipFile(raw) as original:
        for part in fixed.findall('part'):
            if not any(n.text=='2' for n in part.findall('measure/attributes/staves')):
                continue
            for measure,(_,(_,meter)) in zip(part.findall('measure'),inherited_context(part)):
                key=(part.get('id'),measure.get('number'))
                if meter!=('4','4') or (plan:=lanes(measure,current,key)) is None:
                    continue
                a,b,backup,forward,duration,entries=plan
                if any(len(index[k])!=1 or k not in base or _music_signature(current[k])!=_music_signature(base[k])
                       or current[k]['node'].get('default-x')!=base[k]['node'].get('default-x') for k in entries):
                    continue
                candidates=[index[k][0] for k in entries]
                scope={(r['unit'],r['system'],r['staff']) for r in candidates}
                if len(scope)!=1 or any(r['shape']!='WHOLE_NOTE' for r in candidates):
                    continue
                unit,system,staff=scope.pop()
                g=graph(verified,unit,system)
                m=graph(original,unit,system)
                if g is None or m is None:
                    continue
                nodes,other=with_bounds(g),with_bounds(m)
                if any(not native_anchor(r,nodes) or not native_anchor(r,other) for r in candidates):
                    continue
                p=job/f'unit-{int(unit):03d}.png'
                if {r['source_sha256'] for r in candidates}!={sha(p)}:
                    raise ValueError('wide octave tremolo: raster hash mismatch')
                with Image.open(p) as im:
                    image=np.asarray(im.convert('L'))
                st=g.find(f'.//staff[@id="{staff}"]')
                witness=print_evidence(image,st,[(r,nodes[r['head']]) for r in candidates])
                if not witness:
                    continue
                ia=list(measure).index(a)
                first=engrave(a,None,duration//2,True,'start')
                second=engrave(b,None,duration//2,True,'stop')
                second.find('voice').text=first.findtext('voice')
                for node in (a,backup,forward,b):
                    measure.remove(node)
                measure.insert(ia,first)
                measure.insert(ia+1,second)
                changes.append(dict(part=key[0],measure=key[1],unit=unit,system=system,staff=staff,
                    source_heads=[r['head'] for r in candidates],outcome=witness))
    return (fixed,dict(empty,state='restored',changes=changes)) if changes else (root,empty)

