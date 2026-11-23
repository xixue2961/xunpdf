"""Align a split piano bass lane only when both printed beam groups agree."""
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
from source_native_notation import sha
from source_lyric_roles import _score_events
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import pitch,native_anchor,coordinate


def horizontal_layers(image,stems,space):
    """Count thick ink rows between adjacent actual stems, excluding thin staff lines."""
    output=[]
    for first,second in zip(stems,stems[1:]):
        x,y,w,h=first; u,v,_,_=second
        l,r=x+w+3,u-3
        if r-l<space or abs(y-v)>1.0: return None
        start=max(0,min(y,v)-round(space*.32))
        patch=image[start:max(y,v)+round(space*1.62),l:r]
        agreement=[]
        for level in (110,165):
            thick=[]; runs=[]
            for j,on in enumerate((patch<level).mean(axis=1)>=.85):
                if on:thick.append(start+j)
                elif thick:runs.append(thick);thick=[]
            if thick:runs.append(thick)
            beam=[(q[0],q[-1]) for q in runs if len(q)>=round(space*.35)]
            if any(not round(space*.38)<=r-l+1<=round(space*.80) for l,r in beam):return None
            if len(beam)>2 or not beam:return None
            if not (min(y,v)-2<=beam[0][0]<=max(y,v)+3):return None
            if len(beam)==2 and not 1<=beam[1][0]-beam[0][1]-1<=round(space*.35):return None
            agreement.append(beam)
        if len(agreement[0])!=len(agreement[1]) or any(abs(i-j)>2 for q,r in zip(*agreement) for i,j in zip(q,r)):return None
        output.append(agreement)
    return output


def chord_groups(events,pid,bar,staff,by_index):
    grouped=defaultdict(list)
    for k,v in events.items():
        if k[:2]!=(pid,bar) or v['node'].findtext('staff','1')!=staff:continue
        r=by_index.get(k[2])
        if r is None or r['staff_number']!=staff:return []
        grouped[r['chord']].append((r,v))
    groups=sorted(grouped.values(),key=lambda g:median(int(r['x']) for r,_ in g))
    if [len(g) for g in groups]!=[2,2,2,1,1]:return []
    for i,g in enumerate(groups):
        want=['NOTEHEAD_BLACK','NOTEHEAD_BLACK'] if i<3 else [['QUARTER_REST'],['HALF_REST']][i-3]
        if [r['shape'] for r,_ in g]!=want:return []
        if any((v['node'].find('rest') is not None)!=(i>=3) or
               (v['node'].find('chord') is not None)!=(j>0) for j,(_,v) in enumerate(g)):return []
    return groups


def lyrics_or_ties(n):
    return any(n.find(t) is not None for t in ('tie','notations/tied','lyric','grace','time-modification','dot','notations/ornaments'))


def recover(root,job):
    job=Path(job); cfg=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected=cfg.get('accepted_native_sha256') or (cfg.get('source_book_sha256') if cfg.get('source_book')==str(book) else None)
    if not expected or sha(book)!=expected:raise ValueError('parallel rhythm archive hash mismatch')
    ledger=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):
        ledger[(r['part'],r['measure'])].append(r)
    copied=deepcopy(root); ev,_=_score_events(copied)
    images={str(x['unit']):x['image_sha256'] for x in json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))['source_images']}
    changes=[]
    with ZipFile(book) as z:
        for p in copied.findall('part'):
            if not any(n.text=='2' for n in p.findall('measure/attributes/staves')):continue
            ctx=dict(inherited_context(p));pid=p.get('id')
            for m in p.findall('measure'):
                bar=m.get('number')
                backups=m.findall('backup'); forwards=m.findall('forward')
                if ctx[bar][1]!=('4','4') or len(backups)!=2 or len(forwards)!=1 or m.findall('direction'):continue
                rs=ledger[pid,bar]
                if len(rs)!=16 or len({(r['unit'],r['system']) for r in rs})!=1:continue
                ids={int(r['note_index']):r for r in rs}
                if len(ids)!=16:continue
                groups=[chord_groups(ev,pid,bar,k,ids) for k in ('1','2')]
                if any(not v for v in groups):continue
                upper,lower=groups
                if any(lyrics_or_ties(v['node']) for g in groups for q in g for _,v in q):continue
                print_x=lambda q:median(int(r['x']) for r,_ in q)
                if any(abs(print_x(a)-print_x(b))>2 for a,b in zip(upper,lower)):continue
                nums=[[[(v['start'],v['duration']) for _,v in q] for q in g] for g in groups]
                wanted=[(F(0),F(1,4)),(F(1,4),F(1,4)),(F(1,2),F(1,2)),(F(1),F(1)),(F(2),F(2))]
                legacy=[(F(0),F(1,2)),(F(1,2),F(1,2)),(F(1,2),F(1)),(F(1),F(1)),(F(2),F(2))]
                if nums!=[[[v]*len(q) for v,q in zip(clock,g)] for clock,g in zip((wanted,legacy),groups)]:continue
                low_notes=[[v['node'] for _,v in q] for q in lower]
                if any([pitch(n) for n in t]!=[pitch(n) for n in low_notes[0]] for t in low_notes[1:3]):continue
                voices=[{n.findtext('voice','1') for n in q} for q in low_notes]
                if any(len(v)!=1 for v in voices) or voices[2]==voices[0] or any(voices[i]!=voices[0] for i in (1,3,4)):continue
                div=int(low_notes[3][0].findtext('duration'))
                if div%4 or [b.findtext('duration') for b in backups]!=[str(4*div)]*2 or forwards[0].findtext('duration')!=str(div//2):continue
                if forwards[0].findtext('staff')!='2' or forwards[0].findtext('voice')!=next(iter(voices[2])):continue
                written=m.findall('note')
                before=[v['node'] for q in upper for _,v in q]+[n for i in (0,1,3,4,2) for n in low_notes[i]]
                if written!=before:continue
                if [x.tag for x in m if x.tag in ('note','backup','forward')]!=['note']*8+['backup']+['note']*6+['backup','forward','note','note']:continue
                u,s=rs[0]['unit'],rs[0]['system']
                g=E.fromstring(z.read(f"sheet#{u}/sheet#{u}.xml")).find(f"page/system[@id='{s}']")
                ns={n.get('id'):n for n in g.findall('sig/inters/*') if n.find('bounds') is not None}
                if any(not native_anchor(r,ns) for r in rs):continue
                rels=g.findall('sig/relations/relation')
                def box(ch):
                    found=[ns[r.get('target')] for r in rels if r.get('source')==ch and r.find('chord-stem') is not None]
                    return coordinate(found[0]) if len(found)==1 else None
                pn=job/f'unit-{int(u):03d}.png'
                if sha(pn)!=images.get(u):raise ValueError('parallel rhythm raster hash mismatch')
                with Image.open(pn) as im:image=np.asarray(im.convert('L'))
                proofs=[]
                for arm in groups:
                    native_staff=g.find(f".//staff[@id='{arm[0][0][0]['staff']}']")
                    lines=[float(n.find('point').get('y')) for n in native_staff.findall('lines/line')]
                    if len(lines)!=5:break
                    boxes=[box(q[0][0]['chord']) for q in arm[:3]]
                    if any(b is None for b in boxes):break
                    proof=horizontal_layers(image,boxes,(lines[-1]-lines[0])/4)
                    if proof is None or [len(v[0]) for v in proof]!=[2,1]:break
                    proofs.append(proof)
                if len(proofs)!=2:continue
                for i,chord in enumerate(low_notes[:3]):
                    for n in chord:
                        n.find('voice').text=next(iter(voices[0]))
                        n.find('duration').text=str(div//(4 if i<2 else 2));n.find('type').text='16th' if i<2 else 'eighth'
                        for old in n.findall('beam'):n.remove(old)
                    note=chord[0]
                    for beam in [v for v in upper[i][0][1]['node'].findall('beam')]:
                        x=next((list(note).index(k) for k in note if k.tag=='notations'),len(note))
                        note.insert(x,deepcopy(beam))
                third=low_notes[2]
                for n in [backups[-1],forwards[0],*third]:m.remove(n)
                place=list(m).index(low_notes[3][0])
                for i,n in enumerate(third,place):m.insert(i,n)
                changes.append(dict(part=pid,measure=bar,source_page=int(u),both_staff_ink=proofs,lower_clock=['1/4','1/4','1/2','1','2']))
    return (copied if changes else root),dict(state='restored' if changes else 'no_verified_parallel_beams',changes=changes)
