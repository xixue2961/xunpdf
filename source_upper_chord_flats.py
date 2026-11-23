
"""Recover visually corroborated missing flats on exposed upper chord heads."""

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import with_bounds
from source_partial_chord_ties import center, note_address, recovery_groups
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def attached_accidentals(graph, nodes):
    attached=defaultdict(list)
    for rel in graph.findall('sig/relations/relation'):
        if rel.find('alter-head') is not None and rel.get('source') in nodes:
            attached[rel.get('target')].append(nodes[rel.get('source')])
    return attached


def flat_match(image, row, donor_row, donor, space):
    x,y,w,h=coordinate(donor)
    height,width=image.shape
    if not (.65*space<=w<=1.2*space and 2.15*space<=h<=3.1*space):
        return None
    w,h=round(.95*space),round(2.68*space)
    x_left=max(1,round(int(row['x'])-2.35*space))
    x_right=min(width-w-1,round(int(row['x'])-.90*space))
    expected_y=round(y+center(row)-center(donor_row))
    template=image[y:y+h,x:x+w]
    if template.shape!=(h,w):
        return None
    samples=[]
    for cut in (105,135,165):
        t=template<cut
        if t.sum()<3*w:
            return None
        samples.append((cut,t))
    def similarity(a,b):
        fits=[]
        for cut,t in samples:
            q=image[b:b+h,a:a+w]<cut
            keep=(t.sum(1)<.85*w)&(q.sum(1)<.85*w)
            s,r=t[keep],q[keep]
            union=(s|r).sum()
            fits.append(float((s&r).sum()/union) if union>=3*w else 0)
        return min(fits)
    matches=[]
    for a in range(x_left,x_right+1):
        for b in range(max(1,expected_y-4),min(height-h,expected_y+5)):
            q=similarity(a,b)
            if q>=.885:
                matches.append((q,a,b,a+w,b+h))
    if not matches:
        return None
    best=max(matches)
    return best if all(abs(hit[1]-best[1])<=2 and abs(hit[2]-best[2])<=2 for hit in matches) else None


def set_flat(note, written):
    p=note.find('pitch')
    alteration=p.find('alter')
    if alteration is None:
        alteration=ET.Element('alter')
        p.insert(list(p).index(p.find('octave')),alteration)
    alteration.text='-1'
    if written:
        n=ET.Element('accidental')
        n.text='flat'
        tags={'time-modification','stem','notehead','notehead-text','staff','beam','notations','lyric','play','listen'}
        note.insert(next((i for i,c in enumerate(note) if c.tag in tags),len(note)),n)


def recover(root,job):
    job=Path(job)
    proof=job/'source-native-notation.json'
    raw=job/'raw/input.omr'
    report=dict(state='no_changes',changes=[])
    if not proof.is_file() or not raw.is_file():
        return root,dict(report,state='insufficient_evidence')
    parts={p.get('id') for p in root.findall('part-list/score-part') if p.findtext('part-name','').casefold()=='piano'}
    if not parts:
        return root,report
    meta=json.loads(proof.read_text('utf8'))
    accepted,native=(Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(accepted)!=meta.get('accepted_native_sha256'):
        raise ValueError('upper chord flats: native book hash mismatch')
    groups=defaultdict(lambda:defaultdict(list))
    for row in rows(native.with_name('note-ledger.tsv')):
        if row['part'] in parts and row['shape']=='NOTEHEAD_BLACK':
            groups[row['unit']][row['system']].append(row)
    fixed=deepcopy(root)
    current,_=_score_events(fixed)
    base,_=_score_events(score(native))
    lanes_by_pitch=defaultdict(list)
    for key,event in current.items():
        n=event['node']
        step,_,octave=pitch(n)
        if key[0] in parts and step and n.find('grace') is None:
            lanes_by_pitch[key[:2],n.findtext('staff','1'),step,octave].append((key,event))
    with ZipFile(raw) as original,ZipFile(accepted) as verified:
        for unit,systems in groups.items():
            name=f'sheet#{unit}/sheet#{unit}.xml'
            raw_page=ET.fromstring(original.read(name))
            page=ET.fromstring(verified.read(name))
            image=None
            for system,ledger in systems.items():
                g=raw_page.find(f'page/system[@id="{system}"]')
                native_graph=page.find(f'page/system[@id="{system}"]')
                if g is None or native_graph is None:
                    continue
                raw_nodes,native_nodes=with_bounds(g),with_bounds(native_graph)
                signs=attached_accidentals(g,raw_nodes)
                confirmed=attached_accidentals(native_graph,native_nodes)
                strokes=defaultdict(list)
                for r in ledger:
                    event=current.get(note_address(r))
                    marks=signs[r['head']]
                    if (event and event['node'].findtext('accidental')=='flat' and len(marks)==1
                            and marks[0].get('shape')=='FLAT' and len(confirmed[r['head']])==1
                            and confirmed[r['head']][0].get('id')==marks[0].get('id')
                            and native_anchor(r,raw_nodes) and native_anchor(r,native_nodes)):
                        strokes[r['part'],r['measure'],r['staff']].append((r,marks[0]))
                anchors=recovery_groups(ledger,current,base,raw_nodes,native_nodes)
                staffs={s.get('id'):s for s in g.findall('.//staff')}
                for (part,bar,staff,voice),attacks in anchors.items():
                    donors=strokes[part,bar,staff]
                    st=staffs.get(staff)
                    if st is None or len(donors)<2:
                        continue
                    lines=[float(n.find('point').get('y')) for n in st.findall('lines/line')]
                    if len(lines)!=5 or not 10<=(space:=(lines[-1]-lines[0])/4)<=24:
                        continue
                    for time,chord in sorted(attacks,key=lambda c:c[0]):
                        r,e=min(chord,key=lambda hit:center(hit[0]))
                        node=e['node']
                        if (center(r)>lines[0]-.55*space or node.find('accidental') is not None
                                or signs[r['head']] or confirmed[r['head']]
                                or node.find("tie[@type='stop']") is not None
                                or pitch(node)[1] not in ('','0')
                                or not any(len(signs[s['head']])==1 and other['node'].find('accidental') is not None
                                           for s,other in chord if s is not r)):
                            continue
                        if image is None:
                            path=job/f'unit-{int(unit):03d}.png'
                            if {r['source_sha256'] for seq in systems.values() for r in seq}!={sha(path)}:
                                raise ValueError('upper chord flats: source raster hash mismatch')
                            with Image.open(path) as im:
                                image=np.asarray(im.convert('L'))
                        collected=[]
                        for donor_row,mark in donors:
                            hit=flat_match(image,r,donor_row,mark,space)
                            if hit is not None:
                                collected.append((hit,mark.get('id')))
                        if len({d for _,d in collected})<2:
                            continue
                        hit=max(d for d,_ in collected)
                        if any(abs(d[1]-hit[1])>.42*space or abs(d[2]-hit[2])>.42*space for d,_ in collected):
                            continue
                        collected=[(d,ref) for d,ref in collected if abs(d[1]-hit[1])<=2 and abs(d[2]-hit[2])<=2]
                        if len({ref for _,ref in collected})<2:
                            continue
                        _,x,y,w,h=hit
                        if any(
                            min(w,dx+dw)-max(x,dx)>space*.28 and min(h,dy+dh)-max(y,dy)>space*.5
                            for n in raw_nodes.values() if n.tag=='alter'
                            for dx,dy,dw,dh in [coordinate(n)]
                        ):
                            continue
                        step,_,octave=pitch(node)
                        peers=sorted(lanes_by_pitch[(part,bar),node.findtext('staff','1'),step,octave],
                                     key=lambda z:z[1]['start'])
                        following=[]
                        for key,event in peers:
                            other=event['node']
                            if event['start']<=time:
                                continue
                            if other.find('accidental') is not None:
                                break
                            if (other.findtext('voice','1')!=voice or key not in base or
                                    _music_signature(event)!=_music_signature(base[key])):
                                following=None
                                break
                            following.append((key,other))
                        if following is None:
                            continue
                        set_flat(node,True)
                        for _,other in following:
                            set_flat(other,False)
                        report['changes'].append(dict(part=part,measure=bar,unit=unit,system=system,
                            head=r['head'],pitch=[step,'-1',octave],matched_sources=collected,box=hit[1:],
                            note_indices=[int(r['note_index'])]+[key[2] for key,_ in following]))
    return (fixed,dict(report,state='restored')) if report['changes'] else (root,report)

