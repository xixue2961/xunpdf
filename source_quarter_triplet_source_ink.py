"""Original-ink checks for octave heads and independently printed accidentals."""
from itertools import combinations
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

from source_vocal_split_clock import coordinate

SHAPES={"FLAT","NATURAL","SHARP"}


def head_profile(image,x,y,w,h):
    if min(x-20,y)<0 or x+w+20>image.shape[1] or y+h>image.shape[0]:
        return None
    tile=image[y+2:y+h-2,x:x+w]
    surround=image[y+2:y+h-2,x-20:x+w+20]
    return [(tile<cut,(surround<cut).mean(1)>.68) for cut in (90,115,145)]


def compare(a,b):
    hits=[]
    for (orig,left),(target,right) in zip(a,b,strict=True):
        keep=~(left|right)
        u,v=orig[keep],target[keep]
        union=(u|v).sum()
        hits.append(float((u&v).sum()/union) if union>=30 else 0.)
    return min(hits)


def find_head(image,reference,at):
    a=head_profile(image,*reference)
    if a is None:
        return None
    u,v=at
    w,h=reference[2:]
    options=[]
    for x in range(u-3,u+4):
        for y in range(v-3,v+4):
            q=head_profile(image,x,y,w,h)
            if q is not None:
                options.append((compare(a,q),x,y))
    return max(options,default=None)


def prove_octave_columns(groups,nodes,image):
    restored=[]
    removed=[]
    for group in groups:
        for lane in ("upper","lower"):
            cols=group[lane]
            for i,col in enumerate(cols):
                if col is None:
                    if i!=len(cols)-1 or i<1 or cols[i-1] is None:
                        return None
                    other="upper" if lane=="lower" else "lower"
                    peer=group[other][i]
                    prior_peer=group[other][i-1]
                    if peer is None or prior_peer is None or len(peer[2])!=2 or len(prior_peer[2])!=2:
                        return None
                    if sorted(int(nodes[h].get("pitch")) for h in peer[2])!=sorted(int(nodes[h].get("pitch")) for h in prior_peer[2]):
                        return None
                    previous=cols[i-1]
                    if len(previous[2])!=2:
                        return None
                    dx=peer[0]-previous[0]
                    if not 45<=dx<=95:
                        return None
                    new_ids=[]
                    for h in previous[2]:
                        x,y,w,hgt=coordinate(nodes[h])
                        fit=find_head(image,(x,y,w,hgt),(x+dx,y))
                        if fit is None:
                            return None
                        quality,left,top=fit
                        if quality<.95 or abs(top-y)>1:
                            return None
                        new_id=f"ink-{left}-{top}"
                        if new_id in nodes:
                            return None
                        n=ET.Element("head",id=new_id,pitch=nodes[h].get("pitch"),
                                     shape="NOTEHEAD_BLACK",staff=nodes[h].get("staff"))
                        box=[left,top,w,hgt]
                        ET.SubElement(n,"bounds",dict(zip("xywh",map(str,box),strict=True)))
                        nodes[new_id]=n
                        new_ids.append(new_id)
                        restored.append(dict(head=new_id,donor=h,bounds=box,similarity=quality))
                    if abs(coordinate(nodes[new_ids[0]])[0]-coordinate(nodes[new_ids[1]])[0])>2:
                        return None
                    cols[i]=[peer[0],"",new_ids]
                elif len(col[2])==3:
                    ids=col[2]
                    unique=[(a,b) for a,b in combinations(ids,2)
                            if abs(int(nodes[a].get("pitch"))-int(nodes[b].get("pitch")))==7]
                    if len(unique)!=1:
                        return None
                    keep=unique[0]
                    extra=next(h for h in ids if h not in keep)
                    if min(abs(int(nodes[extra].get("pitch"))-int(nodes[h].get("pitch"))) for h in keep)!=2:
                        return None
                    possible=[find_head(image,coordinate(nodes[h]),coordinate(nodes[extra])[:2]) for h in keep]
                    if any(q is None or q[0]>=.55 for q in possible):
                        return None
                    col[2]=list(keep)
                    removed.append(dict(head=extra,bounds=coordinate(nodes[extra]),
                                        similarity=max(q[0] for q in possible)))
                elif len(col[2])!=2:
                    return None
    return dict(restored_heads=restored,removed_heads=removed)


def blank_accidental_neighborhood(gray, head):
    x,y,*_=coordinate(head)
    if x<42 or y<20 or x+27>gray.shape[1] or y+18>gray.shape[0]:
        return False
    for t in (105,130,155):
        core=gray[y-19:y+18,x-34:x-6]<t
        keep=(gray[y-19:y+18,x-42:x+27]<t).mean(1)<.65
        if keep.sum()<20 or float(core[keep].mean())>.016:
            return False
    return True


def small_symbol(gray,head,donors,local_ink):
    x,y,*_=coordinate(head)
    choices={}
    for label in sorted(SHAPES):
        best=[]
        for node,_ in donors.get(label,[]):
            a,b,w,h=coordinate(node)
            if w<14 or h<40:
                continue
            src=Image.fromarray(gray[b:b+h,a:a+w])
            per_donor=[]
            for scale in (.48,.54,.60,.67):
                width,height=round(w*scale),round(h*scale)
                sample=np.asarray(src.resize((width,height),Image.Resampling.LANCZOS))
                for left in range(x-39,x-29):
                    for top in range(y-8,y+2):
                        local=local_ink(gray,left,top,width,height)
                        if local is None:
                            continue
                        values=[]
                        for t,(v,mask) in zip((105,145,175),local,strict=True):
                            u=(sample<t)[~mask]
                            v=v[~mask]
                            union=(u|v).sum()
                            values.append(float((u&v).sum()/union) if union>=40 else 0.)
                        per_donor.append((min(values),left,top,width,height))
            if per_donor:
                best.append((max(per_donor),node.get("id")))
        choices[label]=sorted(best,reverse=True)
    return choices
