"""Recover a complete shared grand-staff clock from ledger and retained raw rests."""
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

from source_native_notation import sha,score
from source_lyric_roles import _score_events
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import pitch,native_anchor,coordinate
from source_parallel_rest_gap import graph,with_bounds,rests


def rhythm(element):
    return element.findtext('type'),element.find('rest') is not None,element.find('chord') is not None


def bitmap_match(page,target,donors):
    x,y,w,h=coordinate(target)
    if not 13<=w<=18 or not 27<=h<=34:return None
    wins=[]
    for threshold,minimum in ((110,.68),(155,.79)):
        refset=[coordinate(n) for n in donors]
        results=[]
        r=page[y:y+h,x:x+16]<threshold
        for u,v,s,t in refset:
            if not 13<=s<=18 or abs(h-t)>2 or abs(y-v)>2:continue
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    z=page[v+dy:v+dy+h,u+dx:u+dx+16]<threshold
                    if z.shape!=r.shape:continue
                    mask=(r.sum(1)<14)&(z.sum(1)<14)
                    if mask.sum()<.65*h:continue
                    b,c=r[mask],z[mask]; union=(b|c).sum()
                    if union<45:continue
                    results.append((float((b&c).sum())/union,dx,dy))
        if not results or max(results)[0]<minimum:return None
        wins.append(max(results))
    return wins


def same_column_rest_ink(page,target,reference,levels,minimum):
    x,y,w,h=coordinate(target)
    u,v,s,t=coordinate(reference)
    if not (13<=w<=18 and 13<=s<=18 and 27<=h<=34 and abs(w-s)<=1
            and abs(t-h)<=1 and abs(y-v)<=1 and abs(x-u)>=25):
        return None
    first=page[y:y+h,x:x+16]
    second=page[v:v+h,u:u+16]
    if first.shape!=second.shape or first.shape!=(h,16):
        return None
    scores=[]
    for level,limit in zip(levels,minimum):
        a=first<level
        b=second<level
        use=(a.sum(1)<14)&(b.sum(1)<14)
        if use.sum()<.62*h:
            return None
        a,b=a[use],b[use]
        total=(a|b).sum()
        if total<45:
            return None
        similarity=float((a&b).sum()/total)
        if similarity<limit:
            return None
        scores.append(similarity)
    return scores


def repeated_bitmap_match(page,target,donors,peers):
    # Require two extremely close printed copies as well as a retained rest.
    for peer in peers:
        copies=same_column_rest_ink(page,target,peer,(110,155,175),(.95,.95,.95))
        if not copies:
            continue
        for source in donors:
            fits=[same_column_rest_ink(page,n,source,(110,130,175),(.75,.82,.82)) for n in (target,peer)]
            if all(fits):
                return dict(peer=peer.get('id'),retained=source.get('id'),same_print=copies,retained_fit=fits)
    return None


def events_for(ev,raw,led):
    out=defaultdict(list)
    for key,v in ev:
        i=key[2]
        if i not in led or key not in raw:return {}
        row=led[i]; ref=raw[key]; n=v['node']
        if pitch(ref['node'])!=pitch(n) or rhythm(ref['node'])!=rhythm(n) or ref['duration']!=v['duration']:return {}
        if row['staff_number']!=n.findtext('staff','1'):return {}
        out[row['staff_number'],row['chord']].append((row,v))
    return out


def insert_rest(voice,staff,duration,col):
    note=E.Element('note',{'default-x':str(col)})
    E.SubElement(note,'rest');E.SubElement(note,'duration').text=str(duration)
    E.SubElement(note,'voice').text=voice;E.SubElement(note,'type').text='eighth';E.SubElement(note,'staff').text=staff
    return note


def clocks(left,right):
    anchors=0
    for x,t,*_ in left:
        for y,u,*_ in right:
            if abs(x-y)<=3 and t==u:
                anchors+=1
            elif (abs(x-y)<=3 and t!=u) or (abs(x-y)>6 and t==u) or (x<y-4 and t>=u) or (y<x-4 and u>=t):
                return 0
    return anchors


def recover(root,job):
    job=Path(job); receipt=job/'source-native-notation.json'
    if not receipt.is_file() or not (job/'raw/input.omr').is_file():
        return root,dict(state='source_unavailable',changes=[])
    cfg=json.loads(receipt.read_text('utf8')); book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected=cfg.get('accepted_native_sha256') or (cfg.get('source_book_sha256') if cfg.get('source_book')==str(book) else None)
    if not expected or sha(book)!=expected:raise ValueError('rest reflow archive mismatch')
    meter=json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))
    pixels={str(v['unit']):v['image_sha256'] for v in meter['source_images']}
    raw,_=_score_events(score(ctl)); out=deepcopy(root); events,dirs=_score_events(out)
    ledger=defaultdict(list); bybar=defaultdict(list); resources={}; pages={}
    for r in rows(ctl.with_name('note-ledger.tsv')):ledger[r['part'],r['measure']].append(r)
    for key,v in events.items():bybar[key[:2]].append((key,v))
    changes=[]
    with ZipFile(book) as accepted,ZipFile(job/'raw/input.omr') as originals:
        for part in out.findall('part'):
            if not any(a.text=='2' for a in part.findall('measure/attributes/staves')):continue
            pid=part.get('id');context=dict(inherited_context(part))
            for m in part.findall('measure'):
                bar=m.get('number');key=(pid,bar)
                # This pattern has an empty/short nominal lane or a
                # spurious additional voice created around missing rests.
                if context[bar][1]!=('4','4') or not m.findall('backup') or m.get('implicit')=='yes':continue
                expression=[v for v in dirs if (v['part'],v['measure'])==key]
                # Only already-timed, staff-specific dynamics can survive
                # the lane rewrite without reinterpreting any expression.
                if any(d['staff'] not in ('1','2') or d['node'].find('offset') is not None or
                       len(d['node'].findall('direction-type/*'))!=1 or d['node'].find('direction-type/dynamics') is None or
                       d['node'].find('sound') is not None and set(d['node'].find('sound').attrib)-{'dynamics'}
                       for d in expression):continue
                music=[n for n in m if n.tag in ('note','backup','forward','direction')]
                if not music or list(m)[list(m).index(music[0]):list(m).index(music[-1])+1]!=music:continue
                rr=ledger[key]; ev=bybar[key]
                where={(r['unit'],r['system']) for r in rr}
                if len(where)!=1 or len(rr)!=len(ev):continue
                led={int(r['note_index']):r for r in rr}
                if len(led)!=len(rr):continue
                pairs=events_for(ev,raw,led)
                if not pairs or {lane for lane,ch in pairs}!={'1','2'}:continue
                bystaff=[[q for (staff,_),q in pairs.items() if staff==k] for k in ('1','2')]
                if any(len(q)<5 for q in bystaff):continue
                if all(len({v['node'].findtext('voice','1') for g in arm for _,v in g})==1 and
                       max(v['start']+v['duration'] for g in arm for _,v in g)==4 for arm in bystaff):continue
                if any(v['node'].find(x) is not None for _,v in ev for x in ('tie','notations/tied','lyric','grace','notations/ornaments')):continue
                if any(v['duration']<=0 or int(v['node'].findtext('duration'))<=0 for _,v in ev):continue
                units={F(v['node'].findtext('duration'))/v['duration'] for _,v in ev}
                if len(units)!=1:continue
                div=units.pop()
                if div.denominator!=1 or int(div)%2:continue
                div=int(div);u,s=where.pop()
                if (u,s) not in resources:
                    a,b=graph(accepted,u,s),graph(originals,u,s);resources[u,s]=(a,b,with_bounds(a))
                a,b,nodes=resources[u,s]
                ids={group[0][0]['chord'] for arm in bystaff for group in arm if group[0][1]['node'].find('pitch') is not None}
                own=[(p,q) for p in a.findall('part') if len(p.findall('staff'))==2
                     for q in p.findall('measure') if ids<=set(q.findtext('head-chords','').split())]
                if len(own)!=1 or any(not native_anchor(r,nodes) for r in rr):continue
                pno,source=own[0]; staffs=[n.get('id') for n in pno.findall('staff')]
                raw_measures=[q for p in b.findall('part') if [n.get('id') for n in p.findall('staff')]==staffs
                              for q in p.findall('measure') if q.get('id')==source.get('id') and ids<=set(q.findtext('head-chords','').split())]
                if len(raw_measures)!=1:continue
                stack=[q for q in a.findall('stack') if q.get('id')==source.get('id') and q.get('special')!='CAUTIONARY']
                if len(stack)!=1 or stack[0].get('expected')!='1' or stack[0].get('duration')!='1':continue
                pos=(int(stack[0].get('left')),int(stack[0].get('right')))
                initial=rests(b,raw_measures[0],set(staffs))
                prepared=[]; additions=[]
                for k,staff,groups in zip(('1','2'),staffs,bystaff):
                    groups=sorted(groups,key=lambda q:median(int(r['x']) for r,_ in q))
                    if any({r['staff'] for r,_ in g}!={staff} or len({v['duration'] for _,v in g})!=1 or
                           [v['node'].find('chord') is not None for _,v in g]!=[False]+[True]*(len(g)-1) for g in groups):break
                    retained=[g[0][0] for g in groups if g[0][0]['shape']=='EIGHTH_REST']
                    staff_el=pno.find(f"staff[@id='{staff}']")
                    staff_y=[float(n.find('point').get('y')) for n in staff_el.findall('lines/line')]
                    if len(staff_y)!=5:break
                    top,bottom=staff_y[0],staff_y[-1]; space=(bottom-top)/4
                    if space<=0:break
                    proposed=[];off_staff=[];safe=True
                    for n in initial[staff]:
                        x,y,w,h=coordinate(n)
                        if top<=y+h/2<=bottom:
                            proposed.append(n)
                            continue
                        # An early OCR box wholly above the staff can be
                        # ignored only in a vertically aligned pitched column
                        # and when it lacks the dimensions of an eighth rest.
                        if y+h < top-space*.45 and (w>20 or h<25) and any(
                                abs(x-median(int(r['x']) for r,_ in g))<=3 and
                                g[0][1]['node'].find('pitch') is not None for g in groups):
                            off_staff.append(n.get('id'))
                        else:
                            safe=False;break
                    if not safe or (not retained and proposed):break
                    rr0={n.get('id'):n for n in proposed}
                    if any(r['head'] not in rr0 for r in retained):break
                    rest_ids={r['head'] for r in retained}
                    missing=[n for n in proposed if n.get('id') not in rest_ids]
                    if len(missing)>3:break
                    present=[(median(int(r['x']) for r,_ in g),g[0][1]['duration'],g,None) for g in groups]
                    all_item=present+[(coordinate(n)[0],F(1,2),[],n) for n in missing]
                    all_item.sort(key=lambda q:q[0])
                    if sum(q[1] for q in all_item)!=4 or any(not pos[0]<q[0]<pos[1] for q in all_item) or any(y-x<12 for (x,*_), (y,*_) in zip(all_item,all_item[1:])):break
                    t=F(0); sequence=[]
                    for x,d,g,n in all_item:
                        sequence.append((x,t,d,g,n)); t+=d
                    prepared.append(sequence)
                    additions.extend((k,n,[rr0[r['head']] for r in retained]) for n in missing)
                if len(prepared)!=2 or not additions or clocks(*prepared)<4:continue
                if any(d['start'] not in {q[1] for q in prepared[int(d['staff'])-1]} for d in expression):continue
                if u not in pages:
                    png=job/f'unit-{int(u):03d}.png'
                    if sha(png)!=pixels.get(u):raise ValueError('rest reflow raster mismatch')
                    with Image.open(png) as pimg:pages[u]=np.asarray(pimg.convert('L'))
                image=pages[u]; evidence=[]
                for k,n,refs in additions:
                    proof=bitmap_match(image,n,refs)
                    if proof is None:
                        peers=[other for staff,other,_ in additions if staff==k and other is not n]
                        proof=repeated_bitmap_match(image,n,refs,peers)
                        if proof is None:break
                    evidence.append(dict(staff=k,id=n.get('id'),bounds=coordinate(n),fits=proof))
                if len(evidence)!=len(additions):continue
                rendered=[]
                for k,sequence in zip(('1','2'),prepared):
                    primary=next(t['node'].findtext('voice','1') for x,o,d,g,n in sequence for r,t in g)
                    lane=[]
                    for x,on,d,g,n in sequence:
                        lane.extend(t['node'] for t in expression if t['staff']==k and t['start']==on)
                        if g:
                            for r,v in g:
                                node=v['node']
                                voice=node.find('voice')
                                if voice is None:raise ValueError('missing explicit voice in selected score')
                                voice.text=primary;lane.append(node)
                        else:
                            base=[(q[0],q[3][0][1]['node'].get('default-x')) for q in sequence if q[3]]
                            near=min(base,key=lambda q:abs(q[0]-x))
                            xpos=round(float(near[1])+.71*(x-near[0]))
                            lane.append(insert_rest(primary,k,div//2,xpos))
                    rendered.append(lane)
                k0=list(m).index(music[0])
                for n in music:m.remove(n)
                gap=E.Element('backup');E.SubElement(gap,'duration').text=str(4*div)
                for i,n in enumerate(rendered[0]+[gap]+rendered[1],k0):m.insert(i,n)
                changes.append(dict(part=pid,measure=bar,shared_anchors=clocks(*prepared),inserted_rest_evidence=evidence,
                                    normalized_voices=True))
    return (out if changes else root),dict(state='restored' if changes else 'no_source_verified_reflow',changes=changes)


