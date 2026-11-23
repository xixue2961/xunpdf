"""Find three printed sloped tremolo strokes between stemless whole-note chords."""
from pathlib import Path
from zipfile import ZipFile
from collections import defaultdict
from xml.etree import ElementTree as E
from itertools import combinations
import hashlib,json,re,sys
import cv2
import numpy as np


def bounds(node):
    b=node.find('bounds')
    return tuple(int(b.get(k)) for k in ('x','y','w','h'))


def ink_group(image,staff,left,right):
    geometry=[]
    for line in staff.findall('lines/line'):
        points=np.array([(float(p.get('x')),float(p.get('y'))) for p in line.findall('point')])
        if not len(points):return None
        geometry.append(points)
    if len(geometry)!=5:return None
    ys0=[float(np.median(v[:,1])) for v in geometry]
    space=float(np.median(np.diff(ys0)))
    a,b=(v[0]+v[2]/2 for v in (left,right))
    delta=b-a
    if not 6*space<=delta<=24*space:return None
    center=(a+b)/2
    xs=np.arange(int(center-.21*delta),int(center+.24*delta))
    y0=max(0,int(ys0[0]-space));y1=min(image.shape[0],int(ys0[-1]+space))
    if len(xs)<3*space or xs.min()<0 or xs.max()>=image.shape[1] or y0>=y1:return None
    staff_lines=np.array([np.interp(xs,v[:,0],v[:,1]) for v in geometry])
    ys=np.arange(y0,y1);best={}
    for slope in np.linspace(-.18,.18,37):
        if abs(slope)<.025:continue
        y=np.rint(ys[:,None]+slope*(xs-center)).astype(int)
        if y.min()<0 or y.max()>=image.shape[0]:continue
        pixels=image[y,xs]
        valid=(abs(y[None,:,:]-staff_lines[:,None,:])>.12*space).all(axis=0)
        for threshold in (160,208):
            fill=((pixels<threshold)&valid).sum(axis=1)/np.maximum(1,valid.sum(axis=1))
            ink=(fill>=.86)&(valid.mean(axis=1)>=.40)
            edges=np.flatnonzero(np.diff(np.r_[False,ink,False])).reshape(-1,2)
            runs=[(int(i),int(j)) for i,j in edges if .27*space<=j-i<=.78*space]
            for group in combinations(runs,3):
                mids=np.array([(i+j-1)/2 for i,j in group])
                gaps=np.diff(mids)
                if not (.58*space<=gaps.min() and gaps.max()<=1.03*space and np.ptp(gaps)<=.17*space):continue
                if mids[0]+y0<ys0[0]-space/2 or mids[-1]+y0>ys0[-1]+space/2:continue
                quality=sum(float(fill[i:j].mean())*(j-i) for i,j in group)
                if threshold not in best or quality>best[threshold]['quality']:
                    best[threshold]=dict(slope=float(slope),quality=quality,bands=[[int(ys[i]),int(ys[j-1])] for i,j in group],
                                        centers=[float(y0+v) for v in mids],support_cols=[int(xs.min()),int(xs.max())])
    if len(best)!=2:return None
    c,d=best[160],best[208]
    if abs(c['slope']-d['slope'])>.045 or max(abs(x-y) for x,y in zip(c['centers'],d['centers']))>.20*space:return None
    return dict(space=space,center_x=center,appearance=best,tracks=3)


def scan(book,source):
    found=[]
    with ZipFile(book) as z:
        for name in z.namelist():
            m=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name)
            if not m:continue
            unit=int(m[1]);raw=(source/f'unit-{unit:03d}.png').read_bytes()
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),0);digest=hashlib.sha256(raw).hexdigest()
            for system in E.fromstring(z.read(name)).findall('page/system'):
                nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
                rel=defaultdict(list)
                for edge in system.findall('sig/relations/*'):
                    a,b=edge.get('source'),edge.get('target');kind=edge[0].tag
                    rel[a].append((kind,b,True));rel[b].append((kind,a,False))
                stacks={s.get('id'):s for s in system.findall('stack') if s.get('special')!='CAUTIONARY'}
                def stemless_whole(chord):
                    return bool(rel[chord]) and all(k=='containment' and f and nodes[i].get('shape')=='WHOLE_NOTE'
                        and all(t=='containment' for t,_,_ in rel[i]) for k,i,f in rel[chord])
                for part in system.findall('part'):
                    staff_list=part.findall('staff')
                    if len(staff_list)!=2:continue
                    for measure in part.findall('measure'):
                        stack=stacks.get(measure.get('id'))
                        if stack is None or stack.get('expected')!='1' or not stack.get('excess'):continue
                        for staff in staff_list:
                            sid=staff.get('id')
                            chords=[c for c in measure.findtext('head-chords','').split() if nodes[c].get('staff')==sid]
                            rests=[c for c in measure.findtext('rest-chords','').split()
                                if any(nodes[i].get('staff')==sid for k,i,f in rel[c] if k=='containment' and f)]
                            if len(chords)!=2 or rests or not all(stemless_whole(c) for c in chords):continue
                            a,b=sorted(chords,key=lambda c:bounds(nodes[c])[0])
                            proof=ink_group(image,staff,bounds(nodes[a]),bounds(nodes[b]))
                            if proof:
                                found.append(dict(unit=unit,system=system.get('id'),staff=sid,measure=measure.get('id'),
                                     left_chord=a,right_chord=b,source_sha256=digest,**proof))
    return found


if __name__=='__main__':
    book,source,out=map(Path,sys.argv[1:])
    result=scan(book,source)
    with out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(result,flush=True)
