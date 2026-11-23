"""Distinguish printed piano tail ties, duplicate arc heads and repeated attacks."""
from collections import defaultdict, Counter
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json
import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_stemless_tremolo_projection import inherited_context,diatonic
from source_piano_portato_ties import tenuto_ink
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate,pitch,native_anchor
from source_tremolo_false_clef import whole_position,treble_top
from source_whole_tremolo_heads import isolated_ink,verify


def connection(edges,first,last,tie,side=None):
    return [n for n,terms in edges.items() if terms=={'LEFT':first,'RIGHT':last} and n.get('tie')=='true'
            and (side is None or n.get('above')==side) and not n.get('left-extension') and not n.get('right-extension')]


def marks(n):
    written=[v.get('type') for v in n.findall('tie')]
    drawn=[v.get('type') for v in n.findall('notations/tied')]
    return written if written==drawn else None


def set_mark(n,kind,source=None):
    for q in n.findall('tie'):n.remove(q)
    for no in n.findall('notations'):
        for q in no.findall('tied'):no.remove(q)
        if not len(no) and not no.attrib:n.remove(no)
    if kind is None:return
    n.insert(list(n).index(n.find('duration'))+1,E.Element('tie',type=kind))
    no=n.find('notations')
    if no is None:
        no=E.Element('notations'); n.insert(next((i for i,p in enumerate(n) if p.tag in ('lyric','play','listen')),len(n)),no)
    no.append(deepcopy(source) if source is not None else E.Element('tied',type=kind))


def hollow_score(image,row,donors):
    x,y,w,h=(int(row[k]) for k in ('x','y','w','h'))
    if not 18<=w<=22 or not 14<=h<=18:return []
    out=[]
    for threshold in (110,165):
        best=0
        for r in donors:
            if r is row:continue
            u,v=int(r['x']),int(r['y'])
            b=image[v:v+16,u+2:u+15]<threshold
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    a=image[y+dy:y+dy+16,x+dx+2:x+dx+15]<threshold
                    if a.shape!=b.shape:continue
                    keep=(a.sum(1)<11)&(b.sum(1)<11)
                    c,d=a[keep],b[keep]
                    if keep.sum()<8:continue
                    best=max(best,float((c&d).sum())/max(1,(c|d).sum()))
        out.append(best)
    return out


def cross_curve(image,first,xend,sign,space):
    x,y,w,h=(int(first[k]) for k in ('x','y','w','h')); center=y+h/2
    lo=round(x+w+.72*space); hi=round(xend-.43*space)
    if not space*4<=hi-lo<=space*12:return
    samples=[]
    for threshold in (110,165):
        hits=[]
        for t in np.linspace(0,1,10):
            u=round(lo+(hi-lo)*float(t))
            dark=[(v-center)*sign for v in range(round(center-1.5*space),round(center+1.5*space)+1)
                  if (v-center)*sign>1 and image[v,u]<threshold]
            if not dark:return
            hits.append(float(min(dark)))
        peak=max(hits)
        if peak-min(hits[:2])<4 or peak-min(hits[-2:])<4 or max(abs(a-b) for a,b in zip(hits,hits[1:]))>5:return
        samples.append(hits)
    if max(abs(a-b) for a,b in zip(*samples))<=2:return samples


def ends(graph,nodes):
    ar=defaultdict(dict)
    for r in graph.findall('sig/relations/relation'):
        end=r.find('slur-head')
        if end is not None and r.get('source') in nodes:
            ar[nodes[r.get('source')]][end.get('side')]=r.get('target')
    return ar


def take(ev,pid,bar,staff,start):
    return [(k[2],v) for k,v in ev.items() if k[:2]==(pid,bar) and v['start']==start and
            v['node'].findtext('staff','1')==staff and v['node'].find('pitch') is not None]


def recover(root,job):
    job=Path(job); cfg=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    digest=cfg.get('accepted_native_sha256') or (cfg.get('source_book_sha256') if cfg.get('source_book')==str(book) else None)
    if not digest or sha(book)!=digest:raise ValueError('tail tie archive hash mismatch')
    by_bar=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):by_bar[r['part'],r['measure']].append(r)
    clone=deepcopy(root); ev,_=_score_events(clone); changes=[]
    pngs={str(v['unit']):v['image_sha256'] for v in json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))['source_images']}
    data=None
    with ZipFile(book) as archive:
        for p in clone.findall('part'):
            if not any(s.text=='2' for s in p.findall('measure/attributes/staves')):continue
            pid=p.get('id');ctx=dict(inherited_context(p)); ms=p.findall('measure')
            active={}
            for before,after in zip(ms,ms[1:]):
                for d in before.findall('attributes/clef'):active[d.get('number','1')]=d
                bar,nxt=before.get('number'),after.get('number')
                if ctx[bar][1]!=('4','4') or ctx[bar]!=ctx[nxt]:continue
                treble=active.get('1')
                if treble is None or [treble.findtext(k) for k in ('sign','line','clef-octave-change')]!=['G','2',None]:continue
                a1=take(ev,pid,bar,'1',F(2));a2=take(ev,pid,bar,'2',F(2))
                b1=take(ev,pid,nxt,'1',F(0));b2=take(ev,pid,nxt,'2',F(0))
                if list(map(len,(a1,a2,b1,b2)))!=[4,3,2,2]:continue
                if any(e['duration']!=2 or e['node'].findtext('type')!='half' for q in (a1,a2) for _,e in q):continue
                if any(e['duration']!=4 or e['node'].findtext('type')!='whole' for q in (b1,b2) for _,e in q):continue
                rows0={int(r['note_index']):r for r in by_bar[pid,bar]}
                a=a1+a2
                if any(i not in rows0 for i,_ in a):continue
                x={int(rows0[i]['x']) for i,_ in a}
                first=min(x)
                true=[(i,v) for i,v in a if abs(int(rows0[i]['x'])-first)<=2 and len([r for r,e in a if r==i])==1]
                # An on-column spurious staff fragment can coexist with
                # shifted heads; only classify onscreen pixel evidence below.
                if len(true)!=5:continue
                units={(r['unit'],r['system']) for r in by_bar[pid,bar]+by_bar[pid,nxt]}
                if len(units)!=1:continue
                u,s=units.pop()
                graph=E.fromstring(archive.read(f"sheet#{u}/sheet#{u}.xml")).find(f"page/system[@id='{s}']")
                nodes={n.get('id'):n for n in graph.findall('sig/inters/*') if n.find('bounds') is not None}
                slurs=ends(graph,nodes)
                if any(rows0[i]['shape']!='NOTEHEAD_VOID' or not native_anchor(rows0[i],nodes) for i,_ in a):continue
                png=job/f'unit-{int(u):03d}.png'
                if sha(png)!=pngs.get(u):raise ValueError('tail tie raster hash mismatch')
                with Image.open(png) as f:pil=f.convert('L')
                image=np.asarray(pil)
                donors=[rows0[i] for i,_ in true]
                metrics={i:hollow_score(image,rows0[i],donors) for i,_ in a}
                if any(len(q)!=2 for q in metrics.values()):continue
                high={i for i,q in metrics.items() if min(q)>=.84}
                low={i for i,q in metrics.items() if max(q)<=.64}
                if len(high)!=4 or len(low)!=3 or high&low:continue
                if sum(i in high for i,_ in a1)!=2 or sum(i in high for i,_ in a2)!=2:continue
                if not all(abs(int(rows0[i]['x'])-first)<=2 for i in high):continue
                # No third chord member or conflicting expression is discarded.
                drops=[(i,v['node']) for i,v in a if i in low]
                if any(n.find('chord') is None or any(k.tag not in ('chord','pitch','duration','tie','voice','type','stem','staff','notations') for k in n)
                       or any(k.tag!='tied' for k in n.findall('notations/*')) for _,n in drops):continue
                bass_target=[]
                for arm in (a1,a2):
                    bass_target.append(sorted([(i,v['node']) for i,v in arm if i in high],key=lambda pair:diatonic(pair[1])))
                after_rows=[r for r in by_bar[pid,nxt] if r['shape']=='WHOLE_NOTE' and native_anchor(r,nodes)]
                if len(after_rows)!=3 or max(int(r['x']) for r in after_rows)-min(int(r['x']) for r in after_rows)>2:continue
                all_longs=[]; all_removes=[]; valid=True
                for staff,real,starts,targets in zip(('1','2'),bass_target,(a1,a2),(b1,b2)):
                    target=sorted([v['node'] for _,v in targets],key=diatonic)
                    if [pitch(n) for _,n in real]!=[pitch(n) for n in target]:valid=False;break
                    physical=[r for r in after_rows if r['staff_number']==staff]
                    if len(physical)!=(1 if staff=='1' else 2):valid=False;break
                    true_ids={rows0[i]['head'] for i,n in real}
                    staff_id=rows0[real[0][0]]['staff']; staff_el=graph.find(f".//staff[@id='{staff_id}']")
                    ys=[float(l.find('point').get('y')) for l in staff_el.findall('lines/line')]
                    if len(ys)!=5:valid=False;break
                    space=(ys[-1]-ys[0])/4
                    if not 11<=space<=17:valid=False;break
                    # Verify both real cross-boundary ties separately.
                    for ix,((i,n),other) in enumerate(zip(real,target)):
                        r=rows0[i]; y=int(r['y'])+int(r['h'])/2; above=ix==1
                        direction=-1 if above else 1
                        pts=[v for v in physical if abs(int(v['y'])+int(v['h'])/2-y)<=2]
                        src=[q['head'] for z,_ in starts if (q:=rows0[z]) and abs(int(q['y'])+int(q['h'])/2-y)<=3 and z in low]
                        old_end=marks(other); old_start=marks(n)
                        if old_end is None or old_start is None:valid=False;break
                        if len(pts)==1:
                            tail=pts[0]
                            tied=[node for head in [r['head'],*src] for node in connection(slurs,head,tail['head'],True,'true' if above else 'false')]
                            if len(tied)!=1 or old_end!=['stop']:valid=False;break
                            ls=slurs[tied[0]]['LEFT']
                            if ls in true_ids:
                                if old_start!=['start']:valid=False;break
                            else:
                                ghost=[v['node'] for z,v in starts if rows0[z]['head']==ls and z in low and pitch(v['node'])==pitch(n) and marks(v['node'])==['start']]
                                if len(ghost)!=1 or old_start:valid=False;break
                            xend=int(tail['x'])
                        elif not pts and staff=='1' and not above and not old_end and not old_start:
                            if data is None:data=verify(job)
                            sites=[q for q in data['certified_sites'] if str(q['unit'])==u and q['system']==s and q['staff'][0]==staff_id and q['head_shape']=='WHOLE_NOTE']
                            templates=[isolated_ink(image,*v['center'],v['step'],space) for q in sites for col in q['heads'][0]['columns'] for v in col]
                            step=treble_top()-diatonic(n)
                            xcenter=median(int(t['x'])+int(t['w'])/2 for t in physical)
                            if not templates or whole_position(image,[t for t in templates if t is not None],xcenter,step,ys[0],space) is None:
                                valid=False;break
                            xend=min(int(t['x']) for t in physical)
                        else:valid=False;break
                        path=cross_curve(image,r,xend,direction,space)
                        if not path:valid=False;break
                        all_longs.append((n,other,'over' if above else 'under',path))
                    if not valid:break
                    # Very short horizontal markings on each repeated attack
                    # are not ties. Require same flat ink at both x positions.
                    repeated=[take(ev,pid,bar,staff,t) for t in (F(3,2),F(7,4))]
                    if list(map(len,repeated))!=[2,2]:valid=False;break
                    start=[(q,v['node']) for q,v in repeated[0] if marks(v['node'])==['start']]
                    stop=[(q,v['node']) for q,v in repeated[1] if marks(v['node'])==['stop']]
                    if len(start)!=1 or len(stop)!=1:valid=False;break
                    (i,n),(k,knot)=start[0],stop[0]
                    ri,rk=rows0.get(i),rows0.get(k)
                    if not ri or not rk or pitch(n)!=pitch(knot) or any(not native_anchor(q,nodes) for q in (ri,rk)):
                        valid=False;break
                    link=connection(slurs,ri['head'],rk['head'],True,'false')
                    if len(link)!=1:valid=False;break
                    xx,yy,ww,hh=coordinate(link[0]); dx=int(rk['x'])-int(ri['x'])
                    if not (.65*space<=ww<=1.5*space and 1<=hh<=.24*space and
                            abs(xx+ww/2-int(rk['x'])-int(rk['w'])/2)<=.24*space and
                            .35*space<=yy-int(rk['y'])-int(rk['h'])<=1.5*space and dx>=1.7*space):
                        valid=False;break
                    if not all(tenuto_ink(pil,xx-z,yy,ww,hh,space) for z in (0,dx)):valid=False;break
                    if any(v['duration']!=F(1,4) for q in repeated for _,v in q):valid=False;break
                    all_removes.extend((n,knot))
                if not valid or len(all_longs)!=4 or len(all_removes)!=4:continue
                for n in all_removes:set_mark(n,None)
                for n,other,side,_ in all_longs:
                    if not marks(n):set_mark(n,'start',E.Element('tied',type='start',orientation=side))
                    if not marks(other):set_mark(other,'stop')
                for _,n in drops:before.remove(n)
                changes.append(dict(part=pid,measure=bar,following_measure=nxt,hollow_match_scores=metrics,
                                    removed_false_heads=len(drops),removed_short_tie_pairs=2,
                                    paper_cross_bar_ties=[dict(pitch=pitch(n),paths=z) for n,_,_,z in all_longs]))
    return (clone if changes else root),dict(state='restored' if changes else 'no_verified_tail_ties',changes=changes)
