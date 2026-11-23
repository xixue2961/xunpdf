"""Project crossed omissions of two printed rests in a grand-staff tail."""
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


def graph(z,u,s):
    return E.fromstring(z.read(f"sheet#{u}/sheet#{u}.xml")).find(f"page/system[@id='{s}']")


def with_bounds(g):
    return {n.get('id'):n for n in g.findall('sig/inters/*') if n.find('bounds') is not None}


def rests(g,measure,staves):
    nodes=with_bounds(g)
    wanted=set(measure.findtext('rest-chords','').split())
    seen=defaultdict(list)
    for r in g.findall('sig/relations/relation'):
        if r.get('source') not in wanted or r.find('containment') is None:continue
        n=nodes.get(r.get('target'))
        if n is not None and n.get('staff') in staves and n.get('shape')=='EIGHTH_REST':
            seen[n.get('staff')].append(n)
    return seen


def mirror_rest_ink(image,a,b):
    x,y,w,h=coordinate(a);u,v,ww,hh=coordinate(b)
    if (w,h)!=(ww,hh) or not (12<=w<=21 and 25<=h<=38) or abs(y-v)>2:return None
    result=[]
    for threshold,limit in ((110,.70),(155,.82)):
        anchor=image[y:y+h,x:x+w]<threshold
        best=(0,0,0)
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                query=image[v+dy:v+dy+h,u+dx:u+dx+w]<threshold
                if anchor.shape!=query.shape:continue
                keep=(anchor.sum(1)<.86*w)&(query.sum(1)<.86*w)
                if keep.sum()<.65*h:continue
                r,t=anchor[keep],query[keep]
                if (r|t).sum()<40:continue
                fit=float((r&t).sum())/(r|t).sum()
                best=max(best,(fit,dx,dy))
        if best[0]<limit:return None
        result.append(best)
    if max(abs(a-b) for a,b in zip(result[0][1:],result[1][1:]))>1:return None
    return result


def at(events,pid,bar,st,t):
    return [v for k,v in events.items() if k[:2]==(pid,bar) and v['start']==t and v['node'].findtext('staff','1')==st]


def plain_rest(n):
    return (n.find('rest') is not None and n.findtext('type')=='eighth'
            and not n.find('rest').attrib and set(x.tag for x in n)<= {'rest','duration','voice','type','staff'})


def rest_copy(node,new_x):
    n=E.Element('note',{'default-x':str(new_x)})
    E.SubElement(n,'rest')
    for k in ('duration','voice','type','staff'):E.SubElement(n,k).text=node.findtext(k)
    return n


def recover(root,job):
    job=Path(job); receipt=job/'source-native-notation.json'
    if not receipt.is_file() or not (job/'raw/input.omr').is_file():return root,dict(state='source_unavailable',changes=[])
    cfg=json.loads(receipt.read_text('utf8'))
    book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    sha_book=cfg.get('accepted_native_sha256') or (cfg.get('source_book_sha256') if cfg.get('source_book')==str(book) else None)
    if not sha_book or sha(book)!=sha_book:raise ValueError('parallel rest source archive hash mismatch')
    mapping=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):mapping[r['part'],r['measure']].append(r)
    sources={str(v['unit']):v['image_sha256'] for v in json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))['source_images']}
    clone=deepcopy(root); ev,ds=_score_events(clone); changes=[]
    with ZipFile(book) as accepted,ZipFile(job/'raw/input.omr') as initial:
        for part in clone.findall('part'):
            if not any(n.text=='2' for n in part.findall('measure/attributes/staves')):continue
            pid=part.get('id');ctx=dict(inherited_context(part))
            for m in part.findall('measure'):
                bar=m.get('number');pp=(pid,bar)
                if ctx[bar][1]!=('4','4') or len(m.findall('backup'))!=1 or m.findall('forward') or m.get('implicit')=='yes':continue
                if any(d['start']>2 for d in ds if (d['part'],d['measure'])==pp):continue
                lanes=[]
                for staff in ('1','2'):
                    head,old,tail=(at(ev,pid,bar,staff,t) for t in (F(2),F(5,2),F(3)))
                    if not 1<=len(head)==len(tail)<=5 or len(old)!=1 or any(x['duration']!=F(1,2) for x in head+old+tail):break
                    if not plain_rest(old[0]['node']):break
                    end=max((v['start']+v['duration'] for k,v in ev.items() if k[:2]==pp and v['node'].findtext('staff','1')==staff),default=0)
                    if end!=F(7,2) or {v['node'].findtext('voice','1') for k,v in ev.items() if k[:2]==pp and v['node'].findtext('staff','1')==staff}!={old[0]['node'].findtext('voice','1')}:break
                    if [pitch(v['node']) for v in head]!=[pitch(v['node']) for v in tail]:break
                    if any(n['node'].find('pitch') is None or n['node'].findtext('type')!='eighth' for n in head+tail):break
                    if any(any(v['node'].find(k) is not None for k in ('tie','notations/tied','lyric','time-modification','dot','beam')) for v in tail):break
                    if list(m).index(old[0]['node'])+1!=list(m).index(tail[0]['node']):break
                    lanes.append((head,old[0],tail))
                if len(lanes)!=2:continue
                div=2*int(lanes[0][1]['node'].findtext('duration'))
                if div<=0 or m.findtext('backup/duration')!=str(7*div//2):continue
                r=mapping[pp]; systems={(row['unit'],row['system']) for row in r}
                if len(systems)!=1:continue
                u,s=systems.pop()
                led=[]
                for staff,(head,old,tail) in zip(('1','2'),lanes):
                    rr=[q for q in r if q['staff_number']==staff]
                    if not rr or len({q['staff'] for q in rr})!=1:break
                    rest=[q for q in rr if q['shape']=='EIGHTH_REST']
                    if len(rest)!=1:break
                    between=[q for q in rr if q['shape']=='NOTEHEAD_BLACK' and int(q['x'])>int(rest[0]['x'])]
                    pre=[q for q in rr if q['shape']=='NOTEHEAD_BLACK' and int(q['x'])<int(rest[0]['x'])]
                    if len(between)!=len(tail) or not pre:break
                    ix=max(int(q['x']) for q in pre)
                    h=[q for q in pre if abs(int(q['x'])-ix)<=2]
                    if len(h)!=len(head) or len({q['chord'] for q in h})!=1 or len({q['chord'] for q in between})!=1:break
                    led.append((h,rest[0],between))
                if len(led)!=2:continue
                heads,existing,tails=zip(*led)
                staves=[q['staff'] for q in existing]
                if len(set(staves))!=2 or abs(int(existing[0]['x'])-int(existing[1]['x']))<15:continue
                g1=graph(accepted,u,s);g0=graph(initial,u,s); nn=with_bounds(g1)
                if any(not native_anchor(q,nn) for h,r,t in led for q in [*h,r,*t]):continue
                part_nodes=[q for q in g1.findall('part') if [st.get('id') for st in q.findall('staff')]==staves]
                if len(part_nodes)!=1:continue
                native=[q for q in part_nodes[0].findall('measure') if all(q1[0]['chord'] in q.findtext('head-chords','').split() for q1 in tails)]
                if len(native)!=1:continue
                n=native[0];stack=[q for q in g1.findall('stack') if q.get('id')==n.get('id') and q.get('special')!='CAUTIONARY']
                if len(stack)!=1 or stack[0].get('expected')!='1' or stack[0].get('duration')!='1':continue
                raw=[q for t in g0.findall('part') if [s.get('id') for s in t.findall('staff')]==staves
                     for q in t.findall('measure') if q.get('id')==n.get('id') and all(q1[0]['chord'] in q.findtext('head-chords','').split() for q1 in tails)]
                if len(raw)!=1:continue
                reference_x=median(int(q['x']) for t in tails for q in t)
                notes_x=median(int(q['x']) for t in heads for q in t)
                if any(abs(int(q['x'])-reference_x)>2 for t in tails for q in t) or any(abs(int(q['x'])-notes_x)>2 for t in heads for q in t):continue
                boxes=rests(g0,raw[0],set(staves)); source=[]
                for st in staves:
                    q=sorted([w for w in boxes[st] if notes_x+15<coordinate(w)[0]<reference_x-14],key=lambda w:coordinate(w)[0])
                    if len(q)!=2 or coordinate(q[1])[0]-coordinate(q[0])[0]<18:break
                    source.append(q)
                if len(source)!=2 or any(abs(coordinate(a)[0]-coordinate(b)[0])>2 for a,b in zip(*source)):continue
                png=job/f'unit-{int(u):03d}.png'
                if sha(png)!=sources.get(u):raise ValueError('parallel rests image hash mismatch')
                with Image.open(png) as image:im=np.asarray(image.convert('L'))
                proof=[]
                for (_,rr,_),q in zip(led,source):
                    present=[k for k in q if k.get('id')==rr['head']]
                    if len(present)!=1:break
                    a=present[0]; b=next(t for t in q if t is not a); match=mirror_rest_ink(im,a,b)
                    if match is None:break
                    proof.append(dict(old_id=a.get('id'),inserted_id=b.get('id'),at=coordinate(b)[0],before_old=coordinate(b)[0]<coordinate(a)[0],match=match))
                if len(proof)!=2 or [e['before_old'] for e in proof] not in ([False,True],[True,False]):continue
                for (head,old,tail),rr,fix in zip(lanes,existing,proof):
                    element=old['node']; printed=float(element.get('default-x'))
                    gap=abs(fix['at']-int(rr['x'])); xt=round(printed+(-1 if fix['before_old'] else 1)*gap*.73)
                    place=list(m).index(element)+int(not fix['before_old'])
                    m.insert(place,rest_copy(element,xt))
                m.find('backup/duration').text=str(4*div)
                changes.append(dict(part=pid,measure=bar,source_page=int(u),rests_recovered=proof,end_onset='7/2'))
    return (clone if changes else root),dict(state='restored' if changes else 'no_verified_crossed_rest_gap',changes=changes)
