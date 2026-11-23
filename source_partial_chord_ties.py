
"""Recover independently printed ties in partially tied repeated piano chords."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import with_bounds
from source_parallel_tail_ties import connection, ends, marks, set_mark
from source_vocal_breath_column import rows
from source_vocal_split_clock import native_anchor, pitch


import numpy as np

def components(ink):
    alive = set(zip(*np.nonzero(ink)))
    while alive:
        todo=[alive.pop()]
        found=[]
        while todo:
            y,x=todo.pop()
            found.append((y,x))
            neighbors={(y+dy,x+dx) for dy in (-1,0,1) for dx in (-1,0,1)} & alive
            alive.difference_update(neighbors)
            todo.extend(neighbors)
        yield found

def trace_arc(image,left,right,center,ceiling,side,gap,minimum_span=1.55, minimum_curve=.165, minimum_lift=.115):
    height,width=image.shape
    top,bot=sorted((int(round(ceiling)),int(round(center))))
    if not 1<=left<right<width-1 or not 1<=top<bot<height-1:
        return None
    straight_l,straight_r=max(0,left-round(3*gap)),min(width,right+round(3*gap))
    results=[]
    for cutoff in (105,150):
        ink=image[top:bot+1,left:right]<cutoff
        ink[(image[top:bot+1,straight_l:straight_r]<cutoff).mean(1)>.86]=False
        fits=[]
        for points in components(ink):
            yy,xx=map(np.asarray,zip(*points))
            start,end=int(xx.min()),int(xx.max())
            span=end-start+1
            if not max(minimum_span*gap,.56*(right-left))<=span or len(points)<span*1.25:
                continue
            # Test the shape, leaving the narrow junctions at the endpoints out of the fit.
            margin=max(1,round(span*.05))
            columns=np.arange(start+margin,end-margin+1)
            if len(columns)<10:
                continue
            trace=[]
            for col in columns:
                members=yy[xx==col]
                if len(members)==0:
                    break
                trace.append(float(np.median(members)))
            if len(trace)!=len(columns):
                continue
            t=np.linspace(-1,1,len(columns))
            curve,slant,bias=np.polyfit(t,trace,2)
            rmse=float(np.sqrt(np.mean((np.polyval((curve,slant,bias),t)-trace)**2)))
            bowl=-side*curve
            if not minimum_curve*gap<=bowl<=.8*gap or abs(slant)>1.0*bowl or rmse>max(.8,.085*gap):
                continue
            portions=np.array_split(np.array(trace),5)
            edge=max(2,round(len(trace)*.10))
            lifts=[side*(np.median(portions[2])-np.median(seq)) for seq in (trace[:edge],trace[-edge:])]
            if min(lifts)<minimum_lift*gap:
                continue
            coords=(left+start,top+int(yy.min()),left+end+1,top+int(yy.max())+1)
            fits.append((coords,round(float(bowl),3),round(rmse,3),round(float(min(lifts)),3)))
        if len(fits)!=1:
            return None
        results.append(fits[0])
    return results if max(abs(x-y) for x,y in zip(results[0][0],results[1][0]))<=3 else None


def note_address(row):
    return row['part'],row['measure'],int(row['note_index'])


def center(row):
    return int(row['y'])+int(row['h'])/2


def printed_arc(image, start, stop, other_notes, gap):
    hx,hr=int(start['x'])+int(start['w']),int(stop['x'])
    if not 1.25*gap <= hr-hx <= 7.5*gap or abs(center(start)-center(stop))>.17*gap:
        return None
    y=(center(start)+center(stop))/2
    possible=[]
    for side in (-1,1):
        nearer=[side*(center(other)-y) for other in other_notes if side*(center(other)-y)>0]
        limit=min(1.8*gap,.65*min(nearer,default=3*gap))
        if limit<.36*gap:
            continue
        left,right,inner=hx,hr,y
        compact=hr-hx<2.3*gap
        if compact:
            if nearer:
                continue
            # A short outer tie may end inside the horizontal projection of each
            # head. Follow its ink only outside both head boxes vertically.
            left=round(int(start['x'])+int(start['w'])/2)
            right=round(int(stop['x'])+int(stop['w'])/2)
            inner=(min(int(start['y']),int(stop['y']))-.10*gap if side<0 else
                   max(int(start['y'])+int(start['h']),int(stop['y'])+int(stop['h']))+.10*gap)
        paths=trace_arc(image,left,right,inner,y+side*limit,side,gap)
        if not paths:
            continue
        a,_,b,_=paths[0][0]
        if a-left>.48*gap or right-b>(.55 if compact else 1.35)*gap:
            continue
        if compact and min(trace[3] for trace in paths)<.22*gap:
            continue
        possible.append((side,paths))
    return possible[0] if len(possible)==1 else None



def short_octave_under_arc(image,r,s,other_notes,pairs,raw,accepted,space):
    if len(pairs)!=2 or len(other_notes)!=1:
        return None
    high=other_notes[0]
    if not 3.35*space<=center(r)-center(high)<=3.65*space:
        return None
    upper=[(h,n,h2,n2) for h,n,h2,n2 in pairs if h is high]
    root,n,stop,end=upper[0]
    if marks(n)!=['start'] or marks(end)!=['stop']:
        return None
    if (len(connection(raw,root['head'],stop['head'],'true','true'))!=1 or
            len(connection(accepted,root['head'],stop['head'],'true','true'))!=1):
        return None
    x,y,w,h=(int(r[k]) for k in ('x','y','w','h'))
    u,v,q,j=(int(s[k]) for k in ('x','y','w','h'))
    if not 1.85*space <= u+q/2-x-w/2 <= 2.40*space or abs(center(r)-center(s))>.12*space:
        return None
    left=round(x+w/2+.16*space)
    right=round(u+q/2-.34*space)
    low=max(y+h,v+j)
    proof=trace_arc(image,left,right,low,low+.39*space,1,space,
                    minimum_span=1.0,minimum_curve=.09,minimum_lift=.08)
    return (1,proof) if proof else None


def recovery_groups(ledger, current, original, raw_nodes, accepted_nodes):
    # Duplicate display labels and repeated ledger addresses must never select a different printed head.
    unique=defaultdict(list)
    for row in ledger:
        unique[note_address(row)].append(row)
    groups=defaultdict(list)
    for key,addresses in unique.items():
        if len(addresses)!=1 or key not in current or key not in original:
            continue
        row=addresses[0]
        actual,base=current[key],original[key]
        node=actual['node']
        if (_music_signature(actual)!=_music_signature(base) or node.get('default-x')!=base['node'].get('default-x')
             or node.find('pitch') is None or any(node.find(name) is not None for name in ('grace','cue','lyric'))
             or not native_anchor(row,raw_nodes) or not native_anchor(row,accepted_nodes)):
            continue
        groups[(key[0],key[1],row['staff'],node.findtext('voice','1'),actual['start'])].append((row,actual))
    lanes=defaultdict(list)
    for key,members in groups.items():
        chords={r['chord'] for r,_ in members}
        distinct={pitch(e['node']) for _,e in members}
        events=[e for _,e in members]
        if (len(chords)!=1 or len(distinct)!=len(members) or not 2<=len(members)<=5
                or len({e['duration'] for e in events})!=1
                or sum(e['node'].find('chord') is None for e in events)!=1):
            continue
        lanes[key[:4]].append((key[4],members))
    return lanes


def matched_pairs(start,end):
    targets={pitch(e['node']):(r,e['node']) for r,e in end}
    if {pitch(e['node']) for _,e in start}!=set(targets):
        return []
    return [(r,e['node'],*targets[pitch(e['node'])]) for r,e in start]


def recover(root,job):
    job=Path(job)
    meta_path=job/'source-native-notation.json'
    raw_path=job/'raw/input.omr'
    report=dict(state='no_candidate',changes=[])
    if not meta_path.is_file() or not raw_path.is_file():
        return root,dict(report,state='insufficient_evidence')
    piano={p.get('id') for p in root.findall('part-list/score-part')
           if p.findtext('part-name','').strip().casefold()=='piano'}
    if not piano:
        return root,report
    info=json.loads(meta_path.read_text('utf8'))
    book,native=(Path(info[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if info.get('accepted_native_sha256')!=sha(book):
        raise ValueError('chord ties: native book evidence hash mismatch')
    scoped=defaultdict(lambda:defaultdict(list))
    for row in rows(native.with_name('note-ledger.tsv')):
        if row['part'] in piano and row['shape']=='NOTEHEAD_BLACK':
            scoped[row['unit']][row['system']].append(row)
    clone=deepcopy(root)
    current,_=_score_events(clone)
    original,_=_score_events(score(native))
    with ZipFile(raw_path) as raw, ZipFile(book) as accepted:
        for unit,systems in scoped.items():
            filename=f'sheet#{unit}/sheet#{unit}.xml'
            raw_page=ET.fromstring(raw.read(filename))
            page=ET.fromstring(accepted.read(filename))
            image=None
            for system,ledger in systems.items():
                a=raw_page.find(f'page/system[@id="{system}"]')
                b=page.find(f'page/system[@id="{system}"]')
                if a is None or b is None:
                    continue
                raw_nodes,accepted_nodes=with_bounds(a),with_bounds(b)
                raw_ties,accepted_ties=ends(a,raw_nodes),ends(b,accepted_nodes)
                if not raw_ties or not accepted_ties:
                    continue
                staves={s.get('id'):s for s in a.findall('.//staff')}
                lanes=recovery_groups(ledger,current,original,raw_nodes,accepted_nodes)
                for (part,bar,staff,_),attacks in lanes.items():
                    st=staves.get(staff)
                    if st is None:
                        continue
                    lines=[float(l.find('point').get('y')) for l in st.findall('lines/line')]
                    if len(lines)!=5 or not 10<=(gap:=(lines[-1]-lines[0])/4)<=24:
                        continue
                    attacks.sort(key=lambda group:group[0])
                    for (time,left),(following,right) in zip(attacks,attacks[1:]):
                        if time+left[0][1]['duration']!=following:
                            continue
                        pairs=matched_pairs(left,right)
                        if not pairs or not any(
                            marks(n)==['start'] and marks(other)==['stop']
                            and len(connection(raw_ties,r['head'],s['head'],'true'))==1
                            and len(connection(accepted_ties,r['head'],s['head'],'true'))==1
                            for r,n,s,other in pairs):
                            continue
                        missing=[(r,n,s,other) for r,n,s,other in pairs
                                 if marks(n)==marks(other)==[]
                                 and not any(x.find('notations/slur') is not None
                                             or x.find('notations/articulations/staccato') is not None
                                             for x in (n,other))]
                        if not missing:
                            continue
                        if image is None:
                            path=job/f'unit-{int(unit):03d}.png'
                            if {r['source_sha256'] for seq in systems.values() for r in seq}!={sha(path)}:
                                raise ValueError('chord ties: source raster evidence hash mismatch')
                            with Image.open(path) as im:
                                image=np.asarray(im.convert('L'))
                        for r,n,s,other in missing:
                            peer=[p for p,_,_,_ in pairs if p is not r]
                            arc=printed_arc(image,r,s,peer,gap) or short_octave_under_arc(
                                image,r,s,peer,pairs,raw_ties,accepted_ties,gap)
                            if arc is None:
                                continue
                            side,detail=arc
                            placement='above' if side<0 else 'below'
                            set_mark(n,'start',ET.Element('tied',type='start',placement=placement))
                            set_mark(other,'stop')
                            report['changes'].append(dict(part=part,measure=bar,staff=staff,
                                unit=unit,system=system,pitch=pitch(n),start=str(time),stop=str(following),
                                note_indices=[int(r['note_index']),int(s['note_index'])],
                                source_heads=[r['head'],s['head']],placement=placement,outline=detail))
    return (clone,dict(report,state='restored')) if report['changes'] else (root,report)

