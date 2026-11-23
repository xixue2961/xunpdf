"""Find the printed numbers next to existing same-staff beam groups."""
from collections import defaultdict
from statistics import median

import cv2


def bounds(n):return [int(n.find('bounds').get(k)) for k in 'xywh']
def center(n):
    x,y,w,h=bounds(n)
    return x+w/2,y+h/2

def candidate_groups(system, *, include_thirty_second=False):
    nodes={n.get('id'):n for n in system.find('sig/inters')}
    children=defaultdict(set)
    links=defaultdict(set)
    from_stem={}
    typed=set()
    for e in system.findall('sig/relations/relation'):
        a,b=e.get('source'),e.get('target')
        if e.find('containment') is not None:
            children[a].add(b)
        elif e.find('beam-stem') is not None:
            links[a].add(b)
        elif e.find('chord-stem') is not None:
            from_stem[b]=a
        elif e.find('chord-tuplet') is not None:
            typed.add(a)
    staff = {s.get('id'): s for part in system.findall('part')
             if len(part.findall('staff')) > 1 for s in part.findall('staff')}
    for g in system.findall('sig/inters/beam-group'):
        beams=sorted(children[g.get('id')])
        if len(beams) not in (1,2,3) or any(i not in nodes or nodes[i].tag!='beam' for i in beams):continue
        members=[links[i] for i in beams]
        stems=set.intersection(*members)
        if stems!=set.union(*members) or not stems<=from_stem.keys():continue
        n=len(stems)
        if (len(beams),n) not in ((1,3),(2,5),(2,6),(3,6)):continue
        if len(beams)==3 and not include_thirty_second:continue
        chords={from_stem[s] for s in stems}
        staffs={nodes[c].get('staff') for c in chords}
        if len(chords)!=n or len(staffs)!=1 or chords&typed or not staffs<=staff.keys():continue
        sid=next(iter(staffs))
        lines=staff[sid].findall('lines/line')
        if len(lines)!=5:continue
        ys=[median(float(p.get('y')) for p in el.findall('point')) for el in lines]
        gap=median(b-a for a,b in zip(ys,ys[1:]))
        cs=sorted(chords,key=lambda c:center(nodes[c])[0])
        positions=[center(nodes[h]) for c in cs for h in children[c] if nodes[h].tag=='head']
        if not positions:continue
        cx=(min(p[0] for p in positions)+max(p[0] for p in positions))/2
        edges=[]
        for bid in beams:
            node=nodes[bid]
            a=node.find('median/p1');b=node.find('median/p2')
            ax,ay=float(a.get('x')),float(a.get('y'))
            bx,by=float(b.get('x')),float(b.get('y'))
            edges.append((ay+(by-ay)*(cx-ax)/(bx-ax),float(node.get('height'))))
        above=median(y for y,_ in edges)<median(y for _,y in positions)
        side=-1 if above else 1
        ref=(min if above else max)(y+side*h/2 for y,h in edges)
        yield dict(group=g.get('id'),count=n,chords=cs,staff=sid,center_x=cx,beam_edge=ref,
                   side=side,digit_placement='above' if above else 'below',space=gap,
                   note_type={1:'eighth',2:'16th',3:'32nd'}[len(beams)])

def glyphs(image,g):
    space=g['space'];cx=g['center_x'];ref=g['beam_edge'];side=g['side']
    left,right=round(cx-2.15*space),round(cx+2.15*space)
    near,far=ref+side*.14*space,ref+side*(3.85 if g["note_type"]=="32nd" else 3.2)*space
    y0,y1=sorted([round(near),round(far)])
    if left < 0 or y0 < 0 or right > image.shape[1] or y1 > image.shape[0] or y0 >= y1:
        return
    cut=image[y0:y1,left:right]
    _,labels,stats,_=cv2.connectedComponentsWithStats((cut<208).astype('uint8'))
    for k,(x,y,bw,bh,area) in enumerate(stats[1:],1):
        if not (.34*space<=bw<=(1.42 if g["note_type"]=="32nd" else 1.25)*space and .77*space<=bh<=1.53*space and area>=.10*space*space):
            continue
        if min(x,y)==0 or x+bw==cut.shape[1] or y+bh==cut.shape[0]:
            continue
        if abs(left+x+bw/2-cx)>1.65*space:
            continue
        box=[left+int(x),y0+int(y),int(bw),int(bh)]
        digit=cut[y:y+bh,x:x+bw].copy()
        mask=labels[y:y+bh,x:x+bw]==k
        if int((digit[mask]<160).sum())<.66*int(area):
            continue
        digit[~mask]=255
        for scale in (1.8,2.35):
            enlarged=cv2.resize(digit,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
            pad=cv2.copyMakeBorder(enlarged,6,6,26,26,cv2.BORDER_CONSTANT,value=255)
            yield box,scale,cv2.cvtColor(pad,cv2.COLOR_GRAY2BGR)
