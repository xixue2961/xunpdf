"""Reject native ornament and pause fragments accounted for by printed curves."""
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
from source_vocal_split_clock import native_anchor, coordinate


KINDS={
    'caesura':('CAESURA','chord-pause','articulations'),
    'staccato':('STACCATO','chord-articulation','articulations'),
    'inverted-mordent':('MORDENT','chord-ornament','ornaments'),
}


def curve_axes(curve):
    a=np.array([[float(curve.get(k)) for k in pair] for pair in
                (('x1','y1'),('ctrlx1','ctrly1'),('ctrlx2','ctrly2'),('x2','y2'))])
    t=np.linspace(0,1,max(120,round(2*abs(a[-1][0]-a[0][0]))))[:,None]
    return ((1-t)**3)*a[0]+3*(1-t)**2*t*a[1]+3*(1-t)*t**2*a[2]+t**3*a[3]


def slur_explains(image, mark, slur):
    x,y,w,h=coordinate(mark)
    curve=slur.find('curve')
    if curve is None:
        return None
    path=curve_axes(curve)
    if path[0,0]>=x-7 or path[-1,0]<=x+w+5 or np.any(np.diff(path[:,0])<=0):
        return None
    # Match the printed centerline at both thresholds, allowing raster and
    # native cubic controls to differ by a few pixels.
    cols=np.arange(x-5,x+w+5)
    expected=np.interp(cols,path[:,0],path[:,1])
    if cols[0]<0 or cols[-1]>=image.shape[1] or expected.min()<10 or expected.max()>=image.shape[0]-10:
        return None
    outside=np.r_[np.arange(5),np.arange(5+w,len(cols))]
    offset=[]
    for cutoff in (115,165):
        local=[]
        for i in outside:
            base=round(expected[i])
            hits=np.flatnonzero(image[base-9:base+10,cols[i]]<cutoff)
            if not 2<=len(hits)<=10 or hits[-1]-hits[0]+1!=len(hits):
                return None
            local.append(float(np.median(hits)+base-9-expected[i]))
        shift=float(np.median(local))
        if abs(shift)>4.2 or max(abs(v-shift) for v in local)>2.8:
            return None
        offset.append(shift)
    if abs(offset[0]-offset[1])>1.1:
        return None
    scores=[]
    for cutoff,shift in zip((115,165),offset):
        yy,xx=np.where(image[y:y+h,x:x+w]<cutoff)
        if len(xx)<max(5,w) or len(np.unique(xx))<w*.55:
            return None
        distance=np.abs(yy+y-(np.interp(xx+x,path[:,0],path[:,1])+shift))
        fit=float((distance<=5.2).mean())
        if fit<.91:
            return None
        scores.append(fit)
    return scores


def one_remote_curve(image, box, source_row):
    x,y,w,h=box
    note_y=int(source_row['y'])
    if not 17<=w<=34 or not 12<=h<=22 or note_y-(y+h)<95:
        return None
    # Unlike two separate caesura slashes, this line enters the left crop
    # edge and leaves the right crop edge with a single vertical run.
    values=[]
    for cutoff in (115,165):
        crop=image[y-8:y+h+8,x-5:x+w+5]<cutoff
        runs=[np.flatnonzero(col) for col in crop.T]
        if len(runs)!=w+10 or any(len(r)<3 or len(r)>12 or r[-1]-r[0]+1!=len(r) for r in runs):
            return None
        centers=[float(np.median(r)) for r in runs]
        if not 8<=centers[0]-centers[-1]<=h+10 or any(not -2<=b-a<=.6 for a,b in zip(centers,centers[1:])):
            return None
        values.append([centers[0],centers[-1],min(map(len,runs)),max(map(len,runs))])
    return values


def evidence(image,tag,mark,bound,curves):
    x,y,w,h=coordinate(mark)
    if tag=='staccato' and not (1<=w<=3 and h>=2.3*w and 6<=h<=14):
        return None
    if tag=='caesura' and (p:=one_remote_curve(image,(x,y,w,h),bound)):
        return {'one_continuous_remote_curve':p}
    if tag=='caesura' and w>12:
        return None
    supported=[]
    for slur in curves:
        if slur.get('tie')=='true' or slur.get('above')!='true':
            continue
        xx,yy,ww,hh=coordinate(slur)
        if xx<=x and x+w<=xx+ww and yy-3<=y and y+h<=yy+hh+4:
            if fit:=slur_explains(image,mark,slur):
                supported.append(dict(source_slur=slur.get('id'),inside_curve=fit))
    return supported[0] if len(supported)==1 else None


def recover(root,job):
    job=Path(job)
    meta=job/'source-native-notation.json'
    raster_index=job/'source-tuplet-evidence/meter/source-meters.json'
    if not meta.is_file() or not raster_index.is_file():
        return root,dict(state='insufficient_evidence',changes=[])
    info=json.loads(meta.read_text('utf8'))
    source,ctl=(Path(info[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(source)!=info.get('accepted_native_sha256'):
        raise ValueError('fragment check archive hash mismatch')
    rasters={str(q['unit']):q['image_sha256'] for q in json.loads(raster_index.read_text('utf8'))['source_images']}
    ledger=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):
        if r['implicit']=='false' and r['repeat_copy']=='false':
            ledger[r['part'],r['measure'],int(r['note_index'])].append(r)
    clone=deepcopy(root)
    ev,_=_score_events(clone)
    changes=[]
    previous=None
    cache={}
    with ZipFile(source) as z:
        for key,doc in ev.items():
            note=doc['node']
            marks=[]
            for tag,(kind,rel,no_tag) in KINDS.items():
                for group in note.findall('notations'):
                    for container in group.findall(no_tag):
                        marks.extend((a,group,container,tag,kind,rel) for a in container.findall(tag))
            refs=ledger.get(key,[])
            if not marks or len(refs)!=1 or note.find('chord') is not None:
                continue
            row=refs[0]
            u,s,c=row['unit'],row['system'],row['chord']
            if (u,s) not in cache:
                g=graph(z,u,s)
                nodes={n.get('id'):n for n in g.findall('sig/inters/*') if n.find('bounds') is not None}
                edges=defaultdict(list)
                for r in g.findall('sig/relations/relation'):
                    if r.get('target') in nodes:
                        edges[r.get('source'),r[0].tag].append(nodes[r.get('target')])
                cache[u,s]=(nodes,edges,[n for n in nodes.values() if n.tag=='slur'])
            nodes,edges,curves=cache[u,s]
            if not native_anchor(row,nodes):
                continue
            paired=[a for a in marks if a[3]=='caesura']
            native_pair=[n for n in edges[c,'chord-pause'] if n.get('shape')=='CAESURA']
            for art,notation,container,tag,kind,rel in marks:
                sources=[n for n in edges[c,rel] if n.get('shape')==kind]
                duplicate=tag=='caesura' and len(paired)==len(native_pair)==2
                if len(sources)!=1 and not duplicate:
                    continue
                if previous!=u:
                    path=job/f'unit-{int(u):03d}.png'
                    if sha(path)!=rasters.get(u):
                        raise ValueError('fragment check raster hash mismatch')
                    with Image.open(path) as page:
                        image=np.asarray(page.convert('L'))
                    previous=u
                item=sources[0]
                bbox=coordinate(item)
                if duplicate:
                    if art is not paired[0][0]:
                        continue
                    boxes=sorted(map(coordinate,sources))
                    a,b=boxes
                    if a[0]+a[2]+3<b[0] or abs(a[1]-b[1])>7:
                        continue
                    left,top=min(a[0],b[0]),min(a[1],b[1])
                    bbox=(left,top,max(x+w for x,y,w,h in boxes)-left,max(y+h for x,y,w,h in boxes)-top)
                    line=one_remote_curve(image,bbox,row)
                    ink={'single_remote_curve_for_split_candidates':line} if line else None
                    discard=paired
                else:
                    ink=evidence(image,tag,item,row,curves)
                    discard=[(art,notation,container,tag,kind,rel)]
                if ink:
                    for node,layer,holder,*_ in discard:
                        holder.remove(node)
                        if not len(holder) and not holder.attrib:layer.remove(holder)
                        if not len(layer) and not layer.attrib:note.remove(layer)
                    changes.append(dict(part=key[0],measure=key[1],note_index=key[2],mark=tag,
                                        source=[n.get('id') for n in sources] if duplicate else item.get('id'),
                                        bounds=bbox,proof=ink))
    return (clone if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)