"""Restore a stemless upper tremolo over an independently proven tied lower rhythm."""
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
from source_native_notation import score,sha
from source_lyric_roles import _score_events
from source_three_stroke_tremolo_ink_v3 import bounds,find_strokes
from source_whole_tremolo_heads import inspect_wholes
from source_stemless_tremolo_projection import diatonic,engrave,inherited_context,signature,STEPS
from source_vocal_breath_column import rows
from source_vocal_split_clock import pitch,native_anchor


def graph(archive,unit,system):
    return E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml')).find(f"page/system[@id='{system}']")


def graph_parts(g):
    nodes={n.get('id'):n for n in g.findall('sig/inters/*') if n.get('id') is not None and n.find('bounds') is not None}
    edges=defaultdict(list); incoming=defaultdict(list)
    for r in g.findall('sig/relations/relation'):
        u,v=r.get('source'),r.get('target')
        if u in nodes and v in nodes and len(r)==1:
            edges[u,r[0].tag].append(v);incoming[v,r[0].tag].append(u)
    return nodes,edges,incoming


def tie_curves(image,first,last):
    result=[]
    for h,k,sign in zip(first,last,(-1,1)):
        cy=int(h['y'])+int(h['h'])/2
        x0=int(h['x'])+int(h['w']); x1=int(k['x']); gap=x1-x0
        span=round(int(h['h'])*1.50)
        if gap<6*int(h['h']) or int(h['y'])!=int(k['y']) or h['h']!=k['h']:return None
        pair=[]
        for threshold in (120,170):
            shape=[]
            for t in (.14,.23,.32,.41,.50,.59,.68,.77,.86,.92):
                x=round(x0+(x1-x0)*t)
                z=[(y-cy)*sign for y in range(round(cy)-span,round(cy)+span+1)
                   if (y-cy)*sign>=2 and image[y,x]<threshold]
                if not z:return None
                shape.append(max(z))
            peak=max(shape)
            if peak-min(shape[:2])<6 or peak-min(shape[-2:])<5 or max(abs(u-v) for u,v in zip(shape,shape[1:]))>=6:return None
            pair.append(shape)
        if max(abs(a-b) for a,b in zip(*pair))>2:return None
        result.append(pair)
    return result


def dot_witness(image,lead):
    x,y,w,h=(int(lead[k]) for k in ('x','y','w','h'))
    found=[]
    for t in (110,155):
        box=image[y-3:y+round(h*.7),x+w+3:x+w+round(w*.9)]
        active=set(zip(*np.where(box<t))); match=[]
        while active:
            work=[active.pop()]; group=[]
            while work:
                j,i=work.pop();group.append((j,i))
                nb={(j+dy,i+dx) for dy in (-1,0,1) for dx in (-1,0,1)}&active
                work.extend(nb);active.difference_update(nb)
            ys,xs=zip(*group)
            if 3<=max(ys)-min(ys)+1<=6 and 3<=max(xs)-min(xs)+1<=6 and 12<=len(group)<=32 and 0<min(xs)<=max(xs)<box.shape[1]-1 and 0<min(ys)<=max(ys)<box.shape[0]-1:
                match.append((min(xs),min(ys)))
        if not match:return False
        found.append(match)
    return any(abs(i-x)<=1 and abs(j-y)<=1 for i,j in found[0] for x,y in found[1])

def raw_flags(image,accepted,raw,left):
    an,ae,_=accepted; rn,re,ri=raw
    proof=[]
    for index in (0,1,3):
        ch=left[index][0]['chord']; stems=ae[ch,'chord-stem']
        if len(stems)!=1 or re[ch,'chord-stem']!=stems:return
        st=stems[0]
        if an[st].get('glyph')!=rn[st].get('glyph') or max(abs(a-b) for a,b in zip(bounds(an[st]),bounds(rn[st])))>1:return
        flags=[rn[k] for k in ri[st,'flag-stem']]
        shapes=[f.get('shape') for f in flags]
        if shapes!={0:[],1:['FLAG_1'],3:['FLAG_2']}[index]:return
        for f in flags:
            x,y,w,h=bounds(f); sx,sy,sw,sh=bounds(rn[st])
            if not (0<=x-(sx+sw)<=4 and 0<=y-sy<=8 and 8<=w<=18 and 25<=h<=48):return
            # Rule out an empty flag box or dark lines alone. The raw graph
            # must also independently connect this flag to the very same stem.
            band=image[y+round(h*.27):y+round(h*.78),x+1:x+w-1]
            for threshold in (110,155):
                mask=band<threshold
                body=(mask.sum(1)>1)&(mask.sum(1)<mask.shape[1]-.5)
                if body.sum()<5 or (mask.sum(0)>2).sum()<5:return
            proof.append(dict(shape=f.get('shape'),bounds=[x,y,w,h],glyph=f.get('glyph')))
    return proof


def lower_notes(before,div):
    fixed=deepcopy(before)
    for i,n in enumerate(fixed):
        dur,kind,tie=(3*div,'half','start') if i<2 else (div//2,'eighth','stop') if i<4 else (div//4,'16th',None)
        node=n.find('duration');node.text=str(dur);n.find('type').text=kind
        if i<2:n.insert(list(n).index(n.find('type'))+1,E.Element('dot'))
        if tie:
            n.insert(list(n).index(node)+1,E.Element('tie',type=tie))
            no=n.find('notations')
            if no is None:no=E.SubElement(n,'notations')
            E.SubElement(no,'tied',type=tie)
    return fixed


def recover(root,job):
    job=Path(job); receipt=job/'source-native-notation.json'; raw_path=job/'raw/input.omr'
    if not receipt.is_file() or not raw_path.is_file():
        return root,dict(state='source_unavailable',changes=[])
    config=json.loads(receipt.read_text('utf8'))
    book,ctl=(Path(config[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected=config.get('accepted_native_sha256') or (config.get('source_book_sha256') if config.get('source_book')==str(book) else None)
    if not expected or sha(book)!=expected:raise ValueError('mixed tremolo source hash mismatch')
    meta=json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))
    maps={(str(v['unit']),str(v['system'])):v['source_columns'] for v in meta['results']}
    images={str(v['unit']):v['image_sha256'] for v in meta['source_images']}
    native,_=_score_events(score(ctl)); output=deepcopy(root); events,_=_score_events(output)
    index=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):index[(r['unit'],r['system'],r['part'],r['measure'])].append(r)
    contexts={(part.get('id'),m):v for part in output.findall('part') for m,v in inherited_context(part)}
    lookup=lambda p,m:output.find(f"part[@id='{p}']/measure[@number='{m}']")
    changes=[]; pages={}; cache={}
    with ZipFile(book) as arch,ZipFile(raw_path) as raw:
        for (unit,sys,pid,bar),rs in index.items():
            fifths,meter=contexts.get((pid,bar),(None,None))
            if fifths is None or not -7<=fifths<=7 or meter!=('4','4'):continue
            upper=[r for r in rs if r['staff_number']=='1']; lower=[r for r in rs if r['staff_number']=='2']
            if len(upper)<2 or len(upper)>5 or len(lower)!=7 or len({r['chord'] for r in upper})!=1 or any(r['shape']!='WHOLE_NOTE' for r in upper):continue
            lower=sorted(lower,key=lambda r:int(r['note_index'])); upper=sorted(upper,key=lambda r:int(r['note_index']))
            if [h['shape'] for h in lower]!=['NOTEHEAD_VOID']*2+['NOTEHEAD_BLACK']*2+['ONE_16TH_REST']+['NOTEHEAD_BLACK']*2:continue
            a,b,c,d=lowgroups=(lower[:2],lower[2:4],lower[4:5],lower[5:])
            if any(len({r['chord'] for r in g})!=1 for g in lowgroups) or len({g[0]['chord'] for g in lowgroups})!=4:continue
            full=upper+lower; keys=[(pid,bar,int(r['note_index'])) for r in full]
            if any(k not in events or k not in native or signature(events[k])!=signature(native[k]) for k in keys):continue
            n=[events[k]['node'] for k in keys]
            nup,nlow=n[:len(upper)],n[len(upper):]
            measure=lookup(pid,bar)
            if measure is None or measure.get('implicit')=='yes' or measure.findall('forward') or measure.findall('note')!=n:continue
            forbidden=('tie','dot','time-modification','beam','grace','lyric','accidental','notations/tied','notations/ornaments')
            if any(note.find(z) is not None for note in n for z in forbidden):continue
            if [events[k]['duration'] for k in keys]!=[4]*len(upper)+[2,2,F(1,2),F(1,2),F(1,4),1,1]:continue
            if [events[k]['start'] for k in keys]!=[0]*len(upper)+[0,0,2,2,F(5,2),F(11,4),F(11,4)]:continue
            if len(measure.findall('backup'))!=1 or measure.findtext('backup/duration')!=nup[0].findtext('duration'):continue
            if any(list(measure).index(q[-1])-list(measure).index(q[0])!=len(q)-1 for q in (nup,nlow)):continue
            if [note.find('chord') is not None for note in nlow]!=[False,True,False,True,False,False,True] or nlow[4].find('rest') is None:continue
            if pair_pitch(nlow[:2]) is None or pair_pitch(nlow[:2])!=pair_pitch(nlow[2:4]) or pair_pitch(nlow[:2])!=pair_pitch(nlow[5:]):continue
            if len({n.findtext('voice','1') for n in nlow})!=1 or len({n.findtext('voice','1') for n in nup})!=1:continue
            x0=median(int(r['x'])+int(r['w'])/2 for r in upper)
            cols=[q for q in maps.get((unit,sys),[]) if q['x']<x0]
            if not cols:continue
            col=max(cols,key=lambda q:q['x'])
            if col['time']!='4/4' or col['conflicting_time_at_same_column'] or len({v['staff'] for v in col['votes'] if v['score']>=.85})<3:continue
            if (unit,sys) not in cache:
                g=graph(arch,unit,sys); r=graph(raw,unit,sys); cache[unit,sys]=(g,graph_parts(g),graph_parts(r))
            g,accepted,rawgraph=cache[unit,sys]; nodes,links,_=accepted
            if any(not native_anchor(r,nodes) for r in full):continue
            loid,hiid=a[0]['staff'],upper[0]['staff']
            can=[]
            for part in g.findall('part'):
                staffs=part.findall('staff')
                if [s.get('id') for s in staffs]!=[hiid,loid]:continue
                for m in part.findall('measure'):
                    chs=m.findtext('head-chords','').split()
                    if upper[0]['chord'] in chs and all(x[0]['chord'] in chs for x in (a,b,d)) and c[0]['chord'] in m.findtext('rest-chords','').split():can.append((staffs,m,chs))
            if len(can)!=1:continue
            staffs,m,chs=can[0]
            st=[s for s in g.findall('stack') if s.get('id')==m.get('id') and s.get('special')!='CAUTIONARY']
            if len(st)!=1 or st[0].get('expected')!='1':continue
            uu=[k for k in chs if nodes[k].get('staff')==hiid]
            if len(uu)!=2:continue
            members={q:[nodes[k] for k in links[q,'containment']] for q in uu}
            if any(len(v)!=len(upper) or any(n.get('shape')!='WHOLE_NOTE' for n in v) for v in members.values()):continue
            ordered=sorted(uu,key=lambda q:center(members[q]))
            if ordered[0]!=upper[0]['chord'] or len(links[ordered[1],'chord-stem']):continue
            x=[center(members[q]) for q in ordered]
            if not x0+35<x[1]<int(b[0]['x']) or any(int(st[0].get('left'))>v or v>int(st[0].get('right')) for v in x):continue
            if any(n.get('staff') in (hiid,loid) and n.tag=='alter' and int(st[0].get('left'))<=bounds(n)[0]<=int(st[0].get('right')) for n in nodes.values()):continue
            if unit not in pages:
                fn=job/f'unit-{int(unit):03d}.png'
                if sha(fn)!=images.get(unit):raise ValueError('mixed tremolo source raster hash mismatch')
                with Image.open(fn) as im:pages[unit]=np.asarray(im.convert('L'))
            image=pages[unit]
            strokes=find_strokes(image,staffs[0],*x,side=1)
            if strokes is None or strokes.get('overlaps_staff_lines'):continue
            confirmed=inspect_wholes(image,[staffs[0]],members,dict(chords=[ordered],columns=x))
            if len(confirmed)!=1:continue
            first,second=confirmed[0]['columns']
            if any(len(c)!=len(upper) or any(h['existing_head'] is None for h in c) for c in (first,second)):continue
            by_head={r['head']:nn for r,nn in zip(upper,nup)}
            if set(by_head)!={h['existing_head'] for h in first}:continue
            origin={diatonic(by_head[h['existing_head']])+h['step'] for h in first}
            if len(origin)!=1:continue
            scale={k:-1 for k in 'BEADGCF'[:-fifths]} if fifths<0 else {k:1 for k in 'FCGDAEB'[:fifths]}
            if any(int(q.findtext('pitch/alter','0'))!=scale.get(q.findtext('pitch/step'),0) for q in nup+nlow if q.find('pitch') is not None):continue
            af,bf=[sorted(v,key=lambda h:int(h['y'])) for v in (a,b)]
            ties=tie_curves(image,af,bf)
            flags=raw_flags(image,accepted,rawgraph,lowgroups)
            if not ties or not flags or not any(dot_witness(image,q) for q in af):continue
            top=int(nup[0].findtext('duration'))
            if top%16:continue
            div=top//4; reference=next(iter(origin))
            before=[engrave(q,None,2*div,i==0,'start' if i==0 else None) for i,q in enumerate(nup)]
            for i,h in enumerate(sorted(second,key=lambda a:-a['step'])):
                diatonic_step=reference-h['step']; step=STEPS[diatonic_step%7]
                before.append(engrave(nup[0],(step,diatonic_step//7,scale.get(step,0)),2*div,i==0,'stop' if i==0 else None))
            for originals,replacement in ((nup,before),(nlow,lower_notes(nlow,div))):
                i=list(measure).index(originals[0])
                for note in originals:measure.remove(note)
                for at,note in enumerate(replacement,i):measure.insert(at,note)
            changes.append(dict(part=pid,measure=bar,unit=int(unit),system=sys,upper_columns=x,
                                raw_flags=flags,lower_ties=ties,source_sha256=images[unit],strokes=3))
    return (output if changes else root),dict(state='restored' if changes else 'no_proven_mixed_tremolo',changes=changes)


def center(nodes):
    return median(bounds(n)[0]+bounds(n)[2]/2 for n in nodes)


def pair_pitch(notes):
    pairs=[pitch(n) for n in notes]
    return pairs if all(a[0] and a[2] for a in pairs) and pairs[0][:2]==pairs[1][:2] and abs(int(pairs[0][2])-int(pairs[1][2]))==1 else None


