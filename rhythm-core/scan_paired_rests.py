"""Find unrecognized matching eighth-rest columns when a recovered flag produced a false head."""
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json
import re
import sys
from xml.etree import ElementTree as ET

import cv2
import numpy as np


def box(node):
    b=node.find('bounds')
    return tuple(int(b.get(k)) for k in ('x','y','w','h'))


def overlaps(a,b):
    return a[0]<b[0]+b[2] and b[0]<a[0]+a[2] and a[1]<b[1]+b[3] and b[1]<a[1]+a[3]


def hits_at(rect,regions,xx,yy):
    mask=np.zeros_like(xx,dtype=bool)
    for x,y,w,h in regions:
        mask|=(xx>=x)&(xx<x+w)&(yy>=y)&(yy<y+h)
    return mask


def staff_mask(staff,image_shape):
    lines=[]
    for line in staff.findall('lines/line'):
        points=np.array([(float(p.get('x')),float(p.get('y'))) for p in line.findall('point')])
        lines.append(np.interp(np.arange(image_shape[1]),points[:,0],points[:,1]))
    return np.array(lines)


def clear_lines(lines,x,y,w,h):
    yy=np.arange(y,y+h)[None,:,None]
    return (abs(lines[:,None,x:x+w]-yy)>2.4).all(axis=0)


def tile(image,rect):
    x,y,w,h=rect
    return image[y:y+h,x:x+w]


def witness_match(image,lines,candidate,reference):
    x,y,w,h=candidate
    rx,ry,rw,rh=reference
    width=min(w,rw);a=tile(image,(x,y,width,h));ma=clear_lines(lines,x,y,width,h)
    best=0.
    for dx in range(-2,3):
        for dy in range(-2,3):
            other=(rx+dx,ry+dy,width,h)
            b=tile(image,other)
            if b.shape!=a.shape:continue
            active=ma&clear_lines(lines,*other)
            qs=[]
            for t in (160,208):
                u=(a<t)&active;v=(b<t)&active
                qs.append(float((u&v).sum()/max(1,max(u.sum(),v.sum()))))
            best=max(best,min(qs))
    return best


def find_pairs(image,lines,left,right,suspect,flag,stem,references,occupied,space):
    sx,sy,sw,sh=suspect
    wx=max(b[2] for _,b in references)
    hy=int(np.median([b[3] for _,b in references]))
    ry=int(np.median([b[1] for _,b in references]))
    low=[]
    for y in range(ry-2,ry+3):
        for x in range(max(left+2,flag[0]+flag[2]),min(right-wx,sx+sw-1)+1):
            b=(x,y,wx,hy)
            if not overlaps(b,suspect) or any(overlaps(b,o) for o in occupied):continue
            data=tile(image,b)
            if ((data<160)&clear_lines(lines,*b)).sum()<3*space:continue
            low.append((b,data.tobytes()))
    if not low:return []
    admissible=defaultdict(list)
    for b,raw in low:admissible[raw].append(b)
    pairs=[]
    for y in range(ry-2,ry+3):
        for x in range(left+2,min(right-wx,sx-wx-round(2*space))+1):
            b=(x,y,wx,hy)
            if any(overlaps(b,o) for o in occupied):continue
            for second in admissible.get(tile(image,b).tobytes(),[]):
                if abs(y-second[1])>1:continue
                xx,yy=np.meshgrid(np.arange(sx,sx+sw),np.arange(sy,sy+sh))
                covered=hits_at(suspect,(flag,stem,second),xx,yy)|~clear_lines(lines,*suspect)
                if ((tile(image,suspect)<208)&~covered).any():continue
                scores=[(rid,witness_match(image,lines,b,ref)) for rid,ref in references]
                if sum(v>=.85 for _,v in scores)<2:continue
                pairs.append(dict(first=b,second=second,witnesses=[(i,v) for i,v in scores if v>=.85]))
    grouped=defaultdict(list)
    for pair in pairs:
        grouped[(pair['first'][0],pair['second'][0])].append(pair)
    # Several vertical paddings can capture the same physical pair.
    # Use the established staff position of the independently recognized rests.
    return [min(group,key=lambda p:(abs(p['first'][1]-ry)+abs(p['second'][1]-ry),
                    -min(score for _,score in p['witnesses'])))
            for group in grouped.values()]


def scan(book,source):
    result=[]
    with ZipFile(book) as archive:
        for name in archive.namelist():
            match=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name)
            if not match:continue
            u=int(match[1]);data=(source/f'unit-{u:03d}.png').read_bytes()
            image=cv2.imdecode(np.frombuffer(data,np.uint8),0);sha=hashlib.sha256(data).hexdigest()
            for system in ET.fromstring(archive.read(name)).findall('page/system'):
                nodes={v.get('id'):v for v in system.findall('sig/inters/*')}
                rel=defaultdict(list)
                for edge in system.findall('sig/relations/*'):
                    a,b=edge.get('source'),edge.get('target')
                    rel[a].append((edge[0].tag,b,True));rel[b].append((edge[0].tag,a,False))
                stacks={x.get('id'):x for x in system.findall('stack')}
                for part in system.findall('part'):
                    staves=part.findall('staff')
                    if len(staves)!=2:continue
                    staff=staves[-1];sid=staff.get('id');geometry=staff_mask(staff,image.shape)
                    space=float(np.median(np.diff(geometry,axis=0)))
                    for measure in part.findall('measure'):
                        stack=stacks.get(measure.get('id'))
                        if stack is None or not stack.get('excess'):continue
                        if not measure.findtext('tuplets','').strip():continue
                        chords=[nodes[i] for i in measure.findtext('head-chords','').split() if nodes[i].get('staff')==sid]
                        rest_ids=[i for c in measure.findtext('rest-chords','').split() for kind,i,forward in rel[c] if kind=='containment' and forward and nodes[i].get('staff')==sid]
                        rests=[(i,box(nodes[i])) for i in rest_ids if nodes[i].get('shape')=='EIGHTH_REST']
                        if len(rests)<2:continue
                        for ch in chords:
                            whole_id=ch.get('id')
                            members=rel[whole_id]
                            if len(members)!=1 or members[0][0]!='containment' or not members[0][2]:continue
                            head_id=members[0][1];head=nodes[head_id]
                            if head.get('shape')!='WHOLE_NOTE' or head.get('manual')=='true' or float(head.get('grade','1'))>.55 or len(rel[head_id])!=1:continue
                            suspect=box(head)
                            occupied=[box(nodes[i]) for other in chords if other.get('id')!=whole_id
                                      for kind,i,forward in rel[other.get('id')] if kind=='containment' and forward]
                            occupied.extend(box(nodes[i]) for i in rest_ids)
                            choices=[]
                            for neighbor in chords:
                                nid=neighbor.get('id')
                                stems=[i for k,i,f in rel[nid] if k=='chord-stem' and f]
                                if len(stems)!=1:continue
                                stem_id=stems[0];stem=box(nodes[stem_id])
                                flags=[i for k,i,f in rel[stem_id] if k=='flag-stem' and not f]
                                if len(flags)!=1:continue
                                flag_id=flags[0];flag=nodes[flag_id]
                                if flag.get('shape')!='FLAG_1' or flag.get('manual')!='true' or not overlaps(suspect,box(flag)):continue
                                pairs=find_pairs(image,geometry,int(stack.get('left')),int(stack.get('right')),suspect,box(flag),stem,rests,occupied,space)
                                for pair in pairs:
                                    choices.append(dict(**pair,neighbor=nid,flag=flag_id,stem=stem_id))
                            if len(choices)==1:
                                result.append(dict(unit=u,system=system.get('id'),staff=sid,measure=measure.get('id'),
                                    head=head_id,chord=whole_id,source_sha256=sha,**choices[0]))
    return result


if __name__=='__main__':
    book,source,out=map(Path,sys.argv[1:])
    proposals=scan(book,source)
    with out.open('x',encoding='utf8') as stream:json.dump(proposals,stream,indent=2)
    print(proposals,flush=True)
