"""Restore a physically duplicated grand-staff wedge on its own staff."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor
from source_vocal_terminal_head import staff_lines


def matching_ink(image, ref, lower_mid, space):
    x,y,w,h=coordinate(ref)
    if not 14*space<=w<=46*space or not .85*space<=h<=2.5*space:
        return None
    original=image[y:y+h,x:x+w]
    if original.shape!=(h,w):
        return None
    candidates=[]
    for v in range(round(lower_mid+4.0*space),round(lower_mid+10*space)):
        for dx in (-1,0,1):
            q=image[v:v+h,x+dx:x+dx+w]
            if q.shape!=original.shape:
                continue
            scores=[]
            for t in (125,160,208):
                a,b=original<t,q<t
                scores.append(float((a&b).sum()/max(1,(a|b).sum())))
            candidates.append((min(scores),v,dx,scores))
    best=max(candidates,default=None)
    if not best or best[0]<.95 or any(v!=best[1] and s>=.91 for s,v,_,_ in candidates):
        return None
    _,v,dx,scores=best
    # Require separate dark arms on the broad side and a single apex on
    # the narrow side of this exact printed symbol.
    profile=[]
    for place in (.025,.24,.51,.76,.97):
        u=x+dx+round(w*place)
        ys=np.flatnonzero((image[v:v+h,u-4:u+5]<135).mean(axis=1)>=.60)
        if len(ys)<2:
            return None
        profile.append(int(ys[-1]-ys[0]))
    if profile[0]>4 or profile[-1]<.85*space or any(a>=b for a,b in zip(profile,profile[1:])):
        return None
    return dict(lower_box=[x+dx,v,w,h],scores=scores,opening=profile)


def wedge_directions(directions):
    entries=[(d,d['node'].find('direction-type/wedge')) for d in directions]
    pairs=[(d,q) for d,q in entries if q is not None]
    if len(pairs)!=2 or [d['staff'] for d,_ in pairs]!=['1','1']:
        return None
    (a,start),(b,stop)=pairs
    if (a['start']>=b['start'] or start.get('type')!='crescendo' or stop.get('type')!='stop'
            or start.get('number','1')!=stop.get('number','1')):
        return None
    if any(len(d['node'].findall('direction-type'))!=1 or any(d['node'].find(k) is not None
           for k in ('offset','sound')) for d in (a,b)):
        return None
    baselines=[]
    for staff in ('1','2'):
        usable=[q for d in directions if d['start']==a['start'] and d['staff']==staff
                if (q:=d['node'].find('direction-type/dynamics')) is not None]
        if len(usable)!=1 or not usable[0].get('default-y'):
            return None
        baselines.append(usable[0])
    if [q.tag for q in baselines[0]]!=[q.tag for q in baselines[1]] or not len(baselines[0]):
        return None
    if start.get('default-y') is None:
        return None
    delta=float(baselines[1].get('default-y'))-float(baselines[0].get('default-y'))
    if not -90<=delta<=-6:
        return None
    return a,b,delta


def available_numbers(part):
    used={int(n.get('number','1')) for n in part.findall('measure/direction/direction-type/wedge')}
    return [n for n in range(1,17) if n not in used]


def attach(bar, a, b, adjustment, number):
    for d in (a,b):
        ref=d['node']
        added=deepcopy(ref)
        kind=added.find('direction-type/wedge')
        where=kind.get('default-y')
        if where is not None:
            kind.set('default-y',f'{float(where)+adjustment:g}')
        kind.set('number',str(number))
        staff=added.find('staff')
        if staff is None:
            staff=E.SubElement(added,'staff')
        staff.text='2'
        bar.insert(list(bar).index(ref)+1,added)


def recover(root,job):
    job=Path(job)
    descriptor=job/'source-native-notation.json'
    images=job/'source-tuplet-evidence/meter/source-meters.json'
    if not descriptor.is_file() or not images.is_file():
        return root, dict(state='insufficient_evidence',changes=[])
    manifest=json.loads(descriptor.read_text('utf8'))
    archive,xml=(Path(manifest[key]) for key in ('accepted_native_book','accepted_native_xml'))
    if not manifest.get('accepted_native_sha256') or sha(archive)!=manifest['accepted_native_sha256']:
        raise ValueError('parallel wedge archive hash mismatch')
    rasters={str(i['unit']):i['image_sha256'] for i in json.loads(images.read_text('utf8'))['source_images']}
    by_bar=defaultdict(list)
    for r in rows(xml.with_name('note-ledger.tsv')):
        if r['repeat_copy']=='false' and r['implicit']=='false':
            by_bar[r['part'],r['measure']].append(r)
    clone=deepcopy(root)
    _,directions=_score_events(clone)
    by_dir=defaultdict(list)
    for d in directions:
        by_dir[d['part'],d['measure']].append(d)
    changes=[]
    current,image=None,None
    with ZipFile(archive) as native:
        for part in clone.findall('part'):
            if not any(n.text=='2' for n in part.findall('measure/attributes/staves')):
                continue
            pid=part.get('id')
            numbers=available_numbers(part)
            # Existing lower wedges in any overlapping bars need their own
            # endpoint analysis before a duplicated mark can be added.
            bars=part.findall('measure')
            lower_active=set()
            for bar in bars:
                measure=bar.get('number')
                dirs=by_dir[pid,measure]
                any_left=bool(lower_active)
                for d in dirs:
                    wedge=d['node'].find('direction-type/wedge')
                    if wedge is None or d['staff']!='2':
                        continue
                    any_left=True
                    no=wedge.get('number','1')
                    if wedge.get('type') in ('crescendo','diminuendo'):
                        lower_active.add(no)
                    elif wedge.get('type')=='stop':
                        lower_active.discard(no)
                pair=wedge_directions(dirs)
                if any_left or not pair or not numbers:
                    continue
                begin,end,delta=pair
                evidence=by_bar[pid,measure]
                where={(r['unit'],r['system']) for r in evidence}
                staffs={r['staff'] for r in evidence if r['staff_number']=='2'}
                upper={r['staff'] for r in evidence if r['staff_number']=='1'}
                if len(where)!=1 or len(staffs)!=1 or len(upper)!=1:
                    continue
                unit,system=where.pop()
                target,source=staffs.pop(),upper.pop()
                g=graph(native,unit,system)
                nodes={n.get('id'):n for n in g.findall('sig/inters/*') if n.find('bounds') is not None}
                left_staff=g.find(f".//staff[@id='{target}']")
                right_staff=g.find(f".//staff[@id='{source}']")
                if left_staff is None or right_staff is None:
                    continue
                right_rows=[r for r in evidence if r['staff']==source and native_anchor(r,nodes)]
                chords={r['chord'] for r in right_rows}
                links=defaultdict(dict)
                for r in g.findall('sig/relations/relation'):
                    edge=r.find('chord-wedge')
                    if edge is not None:
                        links[r.get('target')][edge.get('side')]=r.get('source')
                refs=[w for k,w in nodes.items() if w.tag=='wedge' and w.get('shape')=='CRESCENDO'
                      and set(links[k])=={'LEFT','RIGHT'} and set(links[k].values())<=chords]
                if len(refs)!=1:
                    continue
                ref=refs[0]
                x,y,w,h=coordinate(ref)
                at=x+w/2
                l=staff_lines(left_staff,at)
                u=staff_lines(right_staff,at)
                if not l or not u or abs(l[1]-u[1])>1.2 or not u[0]+3*u[1]<y<l[0]-2*l[1]:
                    continue
                if current!=unit:
                    page=job/f'unit-{int(unit):03d}.png'
                    if sha(page)!=rasters.get(unit):
                        raise ValueError('parallel wedge raster hash mismatch')
                    with Image.open(page) as im:
                        image=np.asarray(im.convert('L'))
                    current=unit
                proof=matching_ink(image,ref,*l)
                if not proof:
                    continue
                if any(wedge.tag=='wedge' and abs(coordinate(wedge)[0]-x)<l[1]
                       and abs(coordinate(wedge)[1]-proof['lower_box'][1])<l[1] for wedge in nodes.values()):
                    continue
                number=numbers.pop(0)
                attach(bar,begin,end,delta,number)
                changes.append(dict(part=pid,measure=measure,unit=unit,source_wedge=ref.get('id'),
                                    new_number=number,onsets=[str(begin['start']),str(end['start'])],physical_proof=proof))
    return (clone if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)