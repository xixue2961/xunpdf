"""Restore a broken piano beam group and an independently visible bass rest."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_duration import _dot_at_threshold
from source_parallel_rest_gap import graph
from source_parallel_rest_reflow import bitmap_match, insert_rest
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch
from source_vocal_terminal_head import head_mask, staff_lines


def lane_candidates(ledger, notes):
    lanes = {'1': defaultdict(list), '2': defaultdict(list)}
    if len(ledger) != len(notes):
        return None
    for i, (r, ev) in enumerate(zip(ledger, notes)):
        n = ev['node']
        staff = r['staff_number']
        if (int(r['note_index']) != i or staff not in lanes or n.findtext('staff') != staff
                or any(n.find(k) is not None for k in ('dot', 'time-modification', 'lyric', 'grace', 'tie', 'cue'))):
            return None
        lanes[staff][r['chord']].append((r,ev))
    upper, lower = (sorted(staff.values(),key=lambda b:int(b[0][0]['x'])) for staff in lanes.values())
    if len(upper)!=14 or [len(b) for b in lower]!=[3,3,1,3,1,1] or any(len(b)!=1 for b in upper):
        return None
    u = [b[0] for b in upper]
    first = [e['node'] for _,e in u]
    if any(n.find('pitch') is None or n.find('chord') is not None for n in first):
        return None
    if (any(r['shape']!='NOTEHEAD_BLACK' for r,_ in u)
            or [e['duration'] for _,e in u] != [F(1,4)]*6+[F(1),F(1),F(1,2),F(1,2)]+[F(1,4)]*4):
        return None
    if [e['start'] for _,e in u] != [F(i,4) for i in range(6)]+[F(3,2),F(2),F(5,2),F(3)]+[F(i,4) for i in range(12,16)]:
        return None
    if any(e['node'].findtext('voice') is None for b in lower for _,e in b):
        return None
    if [b[0][1]['start'] for b in lower] != [0,1,F(3,2),2,F(5,2),3]:
        return None
    if [b[0][1]['duration'] for b in lower] != [F(1,2)]*5+[1]:
        return None
    if [b[0][1]['node'].find('rest') is not None for b in lower] != [False,False,True,False,True,True]:
        return None
    if [pitch(first[i]) for i in (0,6)]!=[pitch(first[0])]*2:
        return None
    a,b=pitch(first[0]),pitch(first[1])
    if a[:2]!=b[:2] or not (a[2] and b[2] and int(a[2])-int(b[2])==1):
        return None
    if [pitch(e['node']) for _,e in lower[0]] != [pitch(e['node']) for _,e in lower[1]]:
        return None
    if any(abs(int(u[i][0]['x'])-int(lower[j][0][0]['x']))>3
           for i,j in ((0,0),(4,1),(6,2),(7,3),(8,4),(10,5))):
        return None
    return u,lower


def thick_runs(image,u,start,stop,scale):
    counts=[]
    for level in (105,155):
        selected=np.flatnonzero((image[start:stop,u-1:u+2]<level).sum(axis=1)>=2)
        bands=np.split(selected,np.flatnonzero(np.diff(selected)>1)+1)
        widths=[len(b) for b in bands if len(b)>=round(scale*.35)]
        if any(t>round(scale*.76) for t in widths):
            return None
        counts.append(widths)
    return counts if len(counts[0])==len(counts[1]) else None


def verify_ink(accepted,original,ledger,upper,lower,image):
    ctl={n.get('id'):n for n in accepted.findall('sig/inters/*') if n.find('bounds') is not None}
    raw={n.get('id'):n for n in original.findall('sig/inters/*') if n.find('bounds') is not None}
    if any(not native_anchor(r,ctl) or not native_anchor(r,raw) for r in ledger):
        return None
    links=original.findall('sig/relations/relation')
    stems={}
    for r,_ in upper[4:10]:
        got=[rel.get('target') for rel in links if rel[0].tag=='chord-stem' and rel.get('source')==r['chord']]
        if len(got)!=1 or got[0] not in raw:
            return None
        stems[r['chord']]=got[0]

    def beam_ids(r):
        stem=stems[r['chord']]
        return {q.get('source') for q in links if q[0].tag=='beam-stem' and q.get('target')==stem}
    uh=[r for r,_ in upper]
    paired=beam_ids(uh[4])
    if len(paired)!=2 or any(x not in raw for x in paired) or beam_ids(uh[5])!=paired or beam_ids(uh[6]):
        return None
    core=beam_ids(uh[8])
    if len(core)!=1 or any(x not in raw for x in core) or beam_ids(uh[9])!=core or beam_ids(uh[7]):
        return None
    x=int(uh[6]['x'])
    staff=original.find(f".//staff[@id='{uh[6]['staff']}']")
    base=staff_lines(staff,x) if staff is not None else None
    if not base:
        return None
    _,space=base
    refx,refy,w,h=(int(uh[1][k]) for k in ('x','y','w','h'))
    dx=int(uh[7]['x'])
    wanted=round((x+dx)/2)
    if not 3.5*space<=dx-x<=4.8*space or not (17<=w<=21 and 14<=h<=18):
        return None
    matches=[]
    for goal in range(wanted-2,wanted+3):
        for y in range(refy-2,refy+3):
            got=[]
            for level in (105,155):
                pair=head_mask(image,goal,y,w,h,level)
                ref=head_mask(image,refx,refy,w,h,level)
                if pair is None or ref is None:
                    break
                a,am=pair; b,bm=ref
                keep=am&bm
                if keep.sum()<8:
                    break
                ca,cb=a[keep],b[keep]
                got.append(float((ca&cb).sum()/max(1,(ca|cb).sum())))
            if len(got)==2:
                matches.append((min(got),goal,y,got))
    if not matches:
        return None
    quality,xnew,ynew,scores=max(matches)
    if quality<.91:
        return None
    if any(n.get('staff')==uh[6]['staff'] and n.tag=='head' and abs(coordinate(n)[0]-xnew)<5
           for n in [*raw.values(),*ctl.values()]):
        return None
    bands=sorted(coordinate(raw[i]) for i in paired)
    ybeam=min(v[1] for v in bands)
    top, bottom=ybeam-round(.18*space),ybeam+round(space*1.48)
    beam_spots=[round((coordinate(raw[stems[uh[6]['chord']]])[0]+xnew+w)/2),xnew+w-5]
    continuous=[thick_runs(image,s,top,bottom,space) for s in beam_spots]
    if any(r is None or len(r[0])!=2 for r in continuous):
        return None
    stem=image[bottom+1:ynew,xnew+w-3:xnew+w+4]<155
    if stem.shape[0]<3*space or (stem.mean(axis=0)>=.95).sum()<2:
        return None
    mixed=[]
    for index,wanted_count in ((7,1),(8,2)):
        targets=[coordinate(raw[stems[uh[i]['chord']]]) for i in (index,index+1)]
        mid=round((targets[0][0]+targets[1][0])/2)
        max_y=max(t[1]+t[3] for t in targets)
        sample=thick_runs(image,mid,max_y-round(2.15*space),max_y+round(.37*space),space)
        if not sample or len(sample[0])!=wanted_count:
            return None
        mixed.append(sample)
    left_staff=lower[0][0][0]['staff']
    rest_x=int(uh[2]['x'])
    rests=[n for n in raw.values() if n.get('staff')==left_staff and n.get('shape')=='EIGHTH_REST'
           and abs(coordinate(n)[0]-rest_x)<=2 and n.get('id') not in ctl]
    donors=[raw[b[0][0]['head']] for b in (lower[2],lower[4])]
    if len(rests)!=1 or not (rest_proof:=bitmap_match(image,rests[0],donors)):
        return None
    ydot=ynew+h+round(.44*space)
    center=xnew+round(.54*w)
    bounds=center-6,ydot-4,center+7,ydot+6
    raster=Image.fromarray(image)
    dots=[_dot_at_threshold(raster,bounds,t,space) for t in (125,165)]
    if not all(dots) or max(abs(a-b) for a,b in zip(*dots))>2:
        return None
    wedges=[n for n in raw.values() if n.tag=='wedge' and n.get('shape')=='CRESCENDO'
            and int(uh[8]['x'])<coordinate(n)[0]<int(uh[9]['x'])
            and 0<=coordinate(n)[0]+coordinate(n)[2]-int(uh[-1]['x'])<=2*space]
    if len(wedges)!=1:
        return None
    return dict(new_head=[xnew,ynew,w,h],head_scores=scores,continuous_beams=continuous,mixed_beams=mixed,
                rest=coordinate(rests[0]),rest_scores=rest_proof,staccato=dots,native_wedge=coordinate(wedges[0]))


def set_rhythm(note, duration, typ, beams):
    note.find('duration').text=str(duration)
    note.find('type').text=typ
    for beam in note.findall('beam'):
        note.remove(beam)
    i=next((i for i,e in enumerate(note) if e.tag in ('notations','lyric','play','listen')),len(note))
    for j,text in enumerate(beams,1):
        b=E.Element('beam',number=str(j))
        b.text=text
        note.insert(i+j-1,b)


def project(measure, upper, lower, directions, div, evidence):
    uh=[e['node'] for _,e in upper]
    basss=[e['node'] for b in lower for _,e in b]
    if any(e.tag not in ('note','backup','forward','attributes','print','direction','barline') for e in measure):
        return False
    if any(b.get('location','right')!='right' for b in measure.findall('barline')):
        return False
    if any(n.find('accidental') is not None for n in (uh[0],uh[1],uh[6])):
        return False
    if len(directions)!=2 or len(measure.findall('direction'))!=2:
        return False
    a,b=directions
    if [(v['staff'],v['start']) for v in directions]!=[('1',F(5,2)),('1',F(15,4))]:
        return False
    left,right=(v['node'].find('direction-type/wedge') for v in directions)
    if ((left is None or right is None or left.get('type')!='crescendo' or right.get('type')!='stop')
         or left.get('number')!=right.get('number') or any(v['node'].find('offset') is not None for v in directions)):
        return False
    piece=deepcopy(uh[1])
    if len(piece.findall('notations/articulations'))!=1 or [v.tag for v in piece.findall('notations/articulations/*')]!=['staccato']:
        return False
    x0,x1=(float(uh[i].get('default-x')) for i in (6,7))
    piece.set('default-x',str(round((x0+x1)/2)))
    voice=uh[0].findtext('voice')
    bass_voice=basss[0].findtext('voice')
    if not voice or not bass_voice or any(n.find('voice') is None for n in (*uh,*basss)):
        return False

    for n in uh:
        n.find('voice').text=voice
    for n in basss:
        n.find('voice').text=bass_voice
    for n,beams in zip((uh[5],uh[6],piece),
                       (('continue','continue'),('continue','continue'),('end','end'))):
        set_rhythm(n,div//4,'16th',beams)
    set_rhythm(uh[7],div//2,'eighth',('begin',))
    set_rhythm(uh[8],div//4,'16th',('continue','begin'))
    set_rhythm(uh[9],div//4,'16th',('end','end'))
    rest=insert_rest(bass_voice,'2',div//2,uh[2].get('default-x'))
    tail=measure.findall('barline')
    for element in list(measure):
        measure.remove(element)
        if element.tag in ('attributes','print'):
            measure.append(element)
    measure.extend([*uh[:7],piece,uh[7],a['node'],*uh[8:-1],b['node'],uh[-1]])
    backup=E.SubElement(measure,'backup')
    E.SubElement(backup,'duration').text=str(4*div)
    measure.extend([*(e['node'] for _,e in lower[0]),rest,*basss[len(lower[0]):]])
    measure.extend(tail)
    return True


def recover(root,job):
    job=Path(job)
    meta=json.loads((job/'source-native-notation.json').read_text('utf8'))
    accepted,control=(Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if not meta.get('accepted_native_sha256') or sha(accepted)!=meta['accepted_native_sha256']:
        raise ValueError('piano beam bridge archive hash mismatch')
    meters=job/'source-tuplet-evidence/meter/source-meters.json'
    rasters={str(r['unit']):r['image_sha256'] for r in json.loads(meters.read_text('utf8'))['source_images']}
    by_bar=defaultdict(list)
    for r in rows(control.with_name('note-ledger.tsv')):
        if r['repeat_copy']=='false' and r['implicit']=='false':
            by_bar[r['part'],r['measure']].append(r)
    result=deepcopy(root)
    notes,directions=_score_events(result)
    ev,dirs=defaultdict(list),defaultdict(list)
    for key,data in notes.items():
        ev[key[:2]].append(data)
    for data in directions:
        dirs[data['part'],data['measure']].append(data)
    changes=[]
    active,img=None,None
    with ZipFile(accepted) as good,ZipFile(job/'raw/input.omr') as original:
        for part in result.findall('part'):
            if not any(s.text=='2' for s in part.findall('measure/attributes/staves')):
                continue
            pid=part.get('id')
            context=dict(inherited_context(part))
            div=0
            for measure in part.findall('measure'):
                if val:=measure.findtext('attributes/divisions'):
                    div=int(val)
                bar=measure.get('number')
                if not div or div%4 or context[bar][1]!=('4','4'):
                    continue
                ledger=sorted(by_bar[pid,bar],key=lambda r:int(r['note_index']))
                if not (lanes:=lane_candidates(ledger,ev[pid,bar])):
                    continue
                group={(r['unit'],r['system']) for r in ledger}
                if len(group)!=1:
                    continue
                unit,system=group.pop()
                if active!=unit:
                    image=job/f'unit-{int(unit):03d}.png'
                    if sha(image)!=rasters.get(unit):
                        raise ValueError('piano beam bridge raster hash mismatch')
                    with Image.open(image) as f:
                        img=f.convert('L')
                    active=unit
                evidence=verify_ink(graph(good,unit,system),graph(original,unit,system),ledger,*lanes,np.asarray(img))
                if evidence and project(measure,*lanes,dirs[pid,bar],div,evidence):
                    changes.append(dict(part=pid,measure=bar,unit=unit,evidence=evidence))
    return (result if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)