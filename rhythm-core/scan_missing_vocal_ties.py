"""Locate paired original-ink tie candidates missing in a native score export."""
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np



def points(left,right,page):
    x1,x2=[int(row['x'])+int(row['w'])/2 for row in (left,right)]
    height=(int(left['h'])+int(right['h']))/2
    span=x2-x1
    if not 1.6*height<=span<=3.5*height:
        return None
    floor=max(int(row['y'])+int(row['h']) for row in (left,right))
    bounds=(round(x1-.20*height),round(floor+.10*height),round(x2+.20*height),round(floor+.95*height))
    x,y,end,bottom=bounds
    crop=page[y:bottom,x:end]
    if not crop.size:
        return None
    traces=[]
    for threshold in (128,160,208):
        mask=(crop<threshold).astype(np.uint8)
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        candidates=[]
        for label,(bx,by,w,h,area) in enumerate(stats[1:],1):
            if not (.72*span<=w<=1.2*span and .2*height<=h<=.85*height and area/w<=.4*height):
                continue
            if h>.63*height and (w<2.5*height or area/w>.39*height):
                continue
            if abs(x+bx-x1)>.35*height or abs(x+bx+w-x2)>.55*height:
                continue
            profile=[]
            for col in range(bx,bx+w):
                ys=np.flatnonzero(labels[:,col]==label)
                if not len(ys):
                    break
                profile.append(float(np.median(ys)))
            if len(profile)!=w:
                continue
            tip=max(2,w//8)
            a,b,mid=np.median(profile[:tip]),np.median(profile[-tip:]),np.median(profile[w//3:2*w//3])
            if min(mid-a,mid-b)<.14*height or abs(a-b)>.18*height:
                continue
            if any(abs(a-b)>3 for a,b in zip(profile,profile[1:])):
                continue
            candidates.append((int(x+bx),int(y+by),int(w),int(h)))
        if len(candidates)!=1:
            return None
        traces.append(candidates[0])
    if any(max(abs(a-b) for a,b in zip(traces[1],p))>2 for p in traces):
        return None
    return dict(crop=bounds,source_curve_bounds=traces[1],threshold_bounds=traces)


def scans(book,ledger,source):
    from source_tempo_context import _score
    from source_lyric_roles import _score_events
    tree=_score(book)
    events,_=_score_events(tree)
    vocal={p.get('id') for p in tree.findall('part') if not p.findall('measure/attributes/staves')}
    names=defaultdict(int)
    for p in tree.findall('part'):
        for m in p.findall('measure'):names[p.get('id'),m.get('number')]+=1
    per_unit=defaultdict(list)
    with ledger.open(encoding='utf8') as f:
        rows={(v['part'],v['measure'],int(v['note_index'])):v for v in csv.DictReader(f,delimiter='\t')
              if v['repeat_copy']=='false' and v['part'] in vocal and v['head']!='0' and names[v['part'],v['measure']]==1}
    for (part,m,i),left in rows.items():
        next_key=(part,m,i+1)
        right=rows.get(next_key)
        if not right or any(left[k]!=right[k] for k in ('unit','system','staff','source_sha256')):
            continue
        if left['shape']!=right['shape'] or left['shape']!='NOTEHEAD_BLACK':
            continue
        a,b=(events.get(k) for k in ((part,m,i),next_key))
        if not a or not b or a['start']+a['duration']!=b['start'] or a['node'].findtext('voice')!=b['node'].findtext('voice'):
            continue
        if a['node'].find('pitch') is None or b['node'].find('pitch') is None:
            continue
        pitch=lambda n:tuple(n.findtext('pitch/'+tag,'0') for tag in ('step','alter','octave'))
        if (b['node'].findtext('lyric/text') or '').strip():
            continue
        if pitch(a['node'])!=pitch(b['node']) or any(v['node'].find('tie') is not None or v['node'].find('notations/slur') is not None for v in (a,b)):
            continue
        per_unit[int(left['unit'])].append((left,right,pitch(a['node'])))
    samples=[]
    for u,records in per_unit.items():
        page=source/f'unit-{u:03d}.png'
        digest=hashlib.sha256(page.read_bytes()).hexdigest()
        pixels=cv2.imdecode(np.fromfile(page,np.uint8),0)
        for a,b,pitch in records:
            assert digest==a['source_sha256']
            source_points=points(a,b,pixels)
            if source_points:
                samples.append(dict(part=a['part'],measure=a['measure'],unit=u,system=a['system'],staff=a['staff'],
                                    first=a['head'],last=b['head'],pitch=pitch,source_sha256=digest,**source_points))
    return samples


if __name__=='__main__':
    book, xml, ledger, source, app, output = map(Path, sys.argv[1:])
    sys.path.insert(0,str(app.resolve()))
    results=scans(xml,ledger,source)
    data=dict(source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),
              source_xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest(),
              source_ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),proposals=results)
    with output.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    print('SOURCE TIE PROPOSALS',[(v['part'],v['measure']) for v in results],flush=True)
