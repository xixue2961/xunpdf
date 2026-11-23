
"""Reconnect a visible terminal piano beam and its incorrectly split voice."""

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def beam_sample(image, first, second, space, expected):
    x,y,w,_=first
    u,v,q,_=second
    x+=w/2
    u+=q/2
    if not 1.65*space<=u-x<=6*space or abs(y-v)>1.2*space:
        return None
    scans=[]
    for threshold in (105,155):
        span=[]
        for t in np.linspace(.12,.88,9):
            cx=round(x+t*(u-x))
            top=round(y+t*(v-y))
            l=top-round(.36*space)
            r=top+round(1.48*space)
            if l<0 or r>=image.shape[0] or not 0<=cx<image.shape[1]:
                return None
            dark=np.flatnonzero(image[l:r,cx]<threshold)
            bands=np.split(dark,np.flatnonzero(np.diff(dark)>1)+1)
            thick=[(int(b[0])+l-top,len(b)) for b in bands if len(b)>=round(.35*space)]
            if len(thick)!=expected:
                return None
            if any(abs(offset)>.34*space or width>.84*space for offset,width in thick):
                return None
            span.append(thick)
        scans.append(span)
    for a,b in zip(*scans):
        if a and any(abs(i-j)>4 for i,j in zip(a[0],b[0])):
            return None
    return scans


def chord_stem(g,nodes,r):
    hits=[nodes[q.get('target')] for q in g.findall('sig/relations/relation')
          if q.get('source')==r['chord'] and q.get('target') in nodes and q.find('chord-stem') is not None]
    if len(hits)!=1 or hits[0].get('shape')!='STEM' or hits[0].get('staff')!=r['staff']:
        return None
    return hits[0]


def event_groups(measure, ledger, current, native):
    data=defaultdict(list)
    part,bar=ledger[0]['part'],ledger[0]['measure']
    notes=measure.findall('note')
    if len(ledger)!=len(notes) or sorted(int(r['note_index']) for r in ledger)!=list(range(len(notes))):
        return None
    for r in ledger:
        idx=int(r['note_index'])
        k=part,bar,idx
        if k not in current or k not in native:
            return None
        event=current[k]
        node=event['node']
        if (_music_signature(event)!=_music_signature(native[k]) or node.find('pitch') is None or
                node.findtext('staff','1')!=r['staff_number'] or node is not notes[idx]
                or node.get('default-x')!=native[k]['node'].get('default-x')):
            return None
        data[r['staff_number'],node.findtext('voice','1'),event['start']].append((r,event))
    if {staff for staff,_,_ in data}!={'1','2'}:
        return None
    ordered=defaultdict(list)
    for (staff,voice,start), entries in data.items():
        if len({r['chord'] for r,_ in entries})!=1:
            return None
        ordered[staff,voice].append((start,entries))
    for seq in ordered.values():
        seq.sort(key=lambda pair:pair[0])
    return ordered


def inspect_bar(measure, lanes):
    upper={v:seq for (staff,v),seq in lanes.items() if staff=='1'}
    lower=[seq for (staff,_),seq in lanes.items() if staff=='2']
    if len(upper)!=2 or len(lower)!=1 or len(measure.findall('forward'))!=1 or len(measure.findall('backup'))!=2:
        return None
    if measure.findall('direction') or measure.findall('harmony') or measure.findall('figured-bass'):
        return None
    ordered=sorted(upper.items(),key=lambda item:len(item[1]))
    (insert_voice,added),(main_voice,main)=ordered
    if len(added)!=1 or len(main)<3 or added[0][0]!=3:
        return None
    if [q[0] for q in main[-2:]]!=[2,3] or main[0][0]!=0:
        return None
    a,b=map(lambda q:q[1],main[-2:])
    c=added[0][1]
    if not (1<=len(a)==len(c)<=5 and 1<=len(b)<=5):
        return None
    if [pitch(e['node']) for _,e in a]!=[pitch(e['node']) for _,e in c]:
        return None
    if any(r['shape']!='NOTEHEAD_BLACK' or e['duration']!=1 or e['node'].findtext('type')!='quarter'
           or any(e['node'].find(k) is not None for k in ('beam','dot','tie','notations/tied','grace','cue','lyric','time-modification'))
           for arm in (a,b,c) for r,e in arm):
        return None
    if [e['node'].find('chord') is not None for _,e in a]!=[False]+[True]*(len(a)-1):
        return None
    bass=lower[0]
    at_three=[arm for t,arm in bass if t==3]
    if (len(at_three)!=1 or bass[0][0]!=0 or bass[-1][0]+bass[-1][1][0][1]['duration']!=4
            or not all(t+events[0][1]['duration']==u for (t,events),(u,_) in zip(bass,bass[1:]))):
        return None
    bass_start=at_three[0][0][0]
    if abs(int(c[0][0]['x'])-int(bass_start['x']))>3:
        return None
    division=int(a[0][1]['node'].findtext('duration'))
    backups=measure.findall('backup')
    forward=measure.find('forward')
    if (division%2 or [n.findtext('duration') for n in backups]!=[str(4*division)]*2
            or forward.findtext('duration')!=str(3*division) or forward.findtext('voice')!=insert_voice
            or forward.findtext('staff')!='1'):
        return None
    main_nodes=[e['node'] for _,chord in main for _,e in chord]
    foreign=[e['node'] for _,e in c]
    lower_nodes=[e['node'] for _,chord in bass for _,e in chord]
    components=[x for x in measure if x.tag in ('note','backup','forward')]
    if components!=[*main_nodes,backups[0],forward,*foreign,backups[1],*lower_nodes]:
        return None
    return a,b,c,main_voice,division,backups[0],forward


def recover(root,job):
    job=Path(job)
    meta=job/'source-native-notation.json'
    raw=job/'raw/input.omr'
    report=dict(state='no_changes',changes=[])
    if not meta.is_file() or not raw.is_file():
        return root,dict(report,state='insufficient_evidence')
    info=json.loads(meta.read_text('utf8'))
    book,native=(Path(info[key]) for key in ('accepted_native_book','accepted_native_xml'))
    if sha(book)!=info.get('accepted_native_sha256'):
        raise ValueError('terminal beam: source book hash mismatch')
    piano={p.get('id') for p in root.findall('part-list/score-part') if p.findtext('part-name','').casefold()=='piano'}
    ledger=defaultdict(list)
    for r in rows(native.with_name('note-ledger.tsv')):
        if r['part'] in piano:
            ledger[r['part'],r['measure']].append(r)
    result=deepcopy(root)
    current,_=_score_events(result)
    baseline,_=_score_events(score(native))
    with ZipFile(raw) as source, ZipFile(book) as control:
        for part in result.findall('part'):
            pid=part.get('id')
            if pid not in piano:
                continue
            for measure,(_,(_,meter)) in zip(part.findall('measure'),inherited_context(part)):
                if meter!=('4','4') or len(measure.findall('forward'))!=1:
                    continue
                rows_here=ledger[pid,measure.get('number')]
                scope={(r['unit'],r['system']) for r in rows_here}
                if len(scope)!=1:
                    continue
                groups=event_groups(measure,rows_here,current,baseline)
                if groups is None or (plan:=inspect_bar(measure,groups)) is None:
                    continue
                a,b,c,voice,division,backup,forward=plan
                unit,system=scope.pop()
                src,ctl=graph(source,unit,system),graph(control,unit,system)
                if src is None or ctl is None:
                    continue
                src_nodes,ctl_nodes=with_bounds(src),with_bounds(ctl)
                if any(not native_anchor(r,src_nodes) or not native_anchor(r,ctl_nodes) for r in rows_here):
                    continue
                stems=[]
                for (r,_) in (a[0],b[0],c[0]):
                    x,y=chord_stem(src,src_nodes,r),chord_stem(ctl,ctl_nodes,r)
                    if x is None or y is None or max(abs(u-v) for u,v in zip(coordinate(x),coordinate(y)))>1:
                        break
                    stems.append(coordinate(x))
                if len(stems)!=3:
                    continue
                staff=src.find(f'.//staff[@id="{a[0][0]["staff"]}"]')
                lines=[float(n.find('point').get('y')) for n in staff.findall('lines/line')]
                if len(lines)!=5 or not 10<=(space:=(lines[-1]-lines[0])/4)<=24:
                    continue
                original=job/f'unit-{int(unit):03d}.png'
                if {r['source_sha256'] for r in rows_here}!={sha(original)}:
                    raise ValueError('terminal beam: source raster hash mismatch')
                with Image.open(original) as im:
                    image=np.asarray(im.convert('L'))
                joined=beam_sample(image,stems[0],stems[1],space,1)
                separated=beam_sample(image,stems[1],stems[2],space,0)
                if not joined or not separated:
                    continue
                for labels,events in (('begin',a),('end',b)):
                    for _,v in events:
                        n=v['node']
                        n.find('type').text='eighth'
                        n.find('duration').text=str(division//2)
                    head=events[0][1]['node']
                    beam=ET.Element('beam',number='1')
                    beam.text=labels
                    head.insert(next((i for i,n in enumerate(head) if n.tag in ('notations','lyric','play','listen')),len(head)),beam)
                for _,e in c:
                    e['node'].find('voice').text=voice
                measure.remove(backup)
                measure.remove(forward)
                report['changes'].append(dict(part=pid,measure=measure.get('number'),unit=unit,system=system,
                   stem_boxes=stems,single_printed_beam=joined,separate_following_stem=separated,
                   right_notes=[[int(r['note_index']) for r,e in arm] for arm in (a,b,c)]))
    return (result,dict(report,state='restored')) if report['changes'] else (root,report)

