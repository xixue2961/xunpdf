"""Find repeated piano figures whose clipped beams and lost rests have independent image proof."""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def box(node):
    return [int(node.find("bounds").get(k)) for k in ("x","y","w","h")]


def center_x(node):
    x,y,w,h=box(node)
    return x+w/2


def line(node):
    return [float(v.get(k)) for v in node.findall("median/*") for k in ("x","y")]


def mask_match(image, a, b):
    def crop(rect):
        x,y,w,h=rect
        if x<0 or y<0 or x+w>image.shape[1] or y+h>image.shape[0]:
            return None
        return image[y:y+h,x:x+w]
    a,b=crop(a),crop(b)
    if a is None or b is None or a.shape!=b.shape:
        return 0
    values=[]
    for threshold in (120,175,205):
        p,q=a<threshold,b<threshold
        values.append(np.count_nonzero(p&q)/max(1,np.count_nonzero(p|q)))
    return min(values)


class Graph:
    def __init__(self, system):
        self.system=system
        sig=system.find("sig")
        self.nodes={n.get("id"):n for n in sig.find("inters")}
        self.members=defaultdict(list)
        self.beams=defaultdict(set)
        self.stem={}
        self.owner={}
        self.dots=defaultdict(list)
        for r in sig.find("relations"):
            src,dst=r.get("source"),r.get("target")
            tag=list(r)[0].tag
            if tag=="containment":
                self.members[src].append(dst)
                self.owner[dst]=src
            elif tag=="beam-stem":
                self.beams[src].add(dst)
            elif tag=="chord-stem":
                self.stem[src]=dst
            elif tag=="augmentation":
                self.dots[dst].append(src)
    def children(self,node):
        return [self.nodes[k] for k in self.members[node]]
    def chords(self,m,staff):
        return sorted([k for k in m.findtext("head-chords","").split()
                        if self.nodes[k].get("staff")==staff and k in self.stem],
                      key=lambda k:center_x(self.nodes[self.stem[k]]))
    def rests(self,m):
        return [v for k in m.findtext("rest-chords","").split() for v in self.children(k) if v.tag=="rest"]


def groups(graph,chords):
    labels=[]
    for offset in range(0,len(chords),4):
        slots=chords[offset:offset+4]
        stems=[graph.stem[c] for c in slots]
        branches=[b for b,owners in graph.beams.items() if owners.intersection(stems)]
        if len(branches)!=2 or len({graph.owner.get(b) for b in branches})!=1:
            return None
        members=[graph.beams[b] for b in branches]
        if all(v==set(stems) for v in members):
            status="full"
        elif all(v==set(stems[1:3]) for v in members):
            status="clipped"
        else:
            return None
        heads=[graph.children(c) for c in slots]
        if any(not h or any(v.get("shape")!="NOTEHEAD_BLACK" for v in h) for h in heads):
            return None
        pitch=[min(float(v.get("pitch")) for v in h) for h in heads]
        beam_ids=sorted(branches,key=lambda b:line(graph.nodes[b])[1])
        labels.append(dict(status=status,stems=stems,chords=slots,pitch=pitch,beams=beam_ids,heads=heads))
    if len({tuple(g["pitch"]) for g in labels})!=1 or sum(g["status"]=="clipped" for g in labels)!=1:
        return None
    return labels


def beam_plan(graph,image,figure,clipped):
    others=[g for g in figure if g is not clipped]
    proofs=[]
    expected_points=[]
    for ref in others:
        moves=[center_x(graph.nodes[t])-center_x(graph.nodes[s]) for s,t in zip(ref["stems"],clipped["stems"])]
        if max(moves)-min(moves)>3:
            continue
        shift=round(float(np.median(moves)))
        bands=[]
        for bid in ref["beams"]:
            donor=graph.nodes[bid]
            b=box(donor)
            new=[b[0]+shift,*b[1:]]
            quality=mask_match(image,b,new)
            if quality<.92:
                break
            x,y,xx,yy=line(donor)
            bands.append((x+shift,y,xx+shift,yy,float(donor.get("height")),bid))
            proofs.append(float(quality))
        if len(bands)==2:
            expected_points.append(bands)
        else:
            if bands:
                del proofs[-len(bands):]
    if len(expected_points)<2:
        return None
    chosen=expected_points[0]
    if any(abs(a[i]-b[i])>3 for g in expected_points for a,b in zip(chosen,g) for i in range(4)):
        return None
    return chosen,proofs


def rest_plan(graph,raw,raw_bar,bar,lower,lh,image):
    old=[r for r in raw.rests(raw_bar) if r.get("staff")==lower]
    missing=[r for r in old if r.get("shape")=="EIGHTH_REST" and r.get("id") not in graph.nodes]
    keep=[r for r in graph.rests(bar) if r.get("staff")==lower]
    if len(old)!=3 or len(missing)!=2 or len(keep)!=1 or keep[0].get("shape")!="QUARTER_REST":
        return None
    if {r.get("id") for r in old if r.get("shape")=="QUARTER_REST"}!={keep[0].get("id")}:
        return None
    a,b=sorted(missing,key=center_x)
    if not center_x(graph.nodes[lh[0]]) < center_x(a)<center_x(keep[0])<center_x(b)<center_x(graph.nodes[lh[1]]):
        return None
    quality=mask_match(image,box(a),box(b))
    return (a,b,quality) if quality>=.95 else None


def slur_dot(graph,image,figure):
    found=[]
    for g in figure:
        for chord in g["heads"]:
            for head in chord:
                for did in graph.dots[head.get("id")]:
                    node=graph.nodes[did]
                    x,y,w,h=box(node)
                    if w>5 or h<3*w:
                        return None
                    hits=[box(v) for v in graph.nodes.values() if v.tag=="slur" and v.get("shape")=="SLUR_BELOW"
                          and v.find("bounds") is not None]
                    arcs=[v for v in hits if abs(v[0]-x)<=w and y<=v[1]<y+h and v[2]>=50]
                    if len(arcs)!=1:
                        return None
                    ax,ay,aw,ah=arcs[0]
                    _,ids,stats,_=cv2.connectedComponentsWithStats((image[ay:ay+ah,ax:ax+aw]<170).astype("uint8"),8)
                    selected=ids[:y+h-ay,max(x-ax,0):x+w-ax]
                    if not any(stats[n,2]>40 for n in np.unique(selected) if n>0):
                        return None
                    found.append((did,head.get("id"),(x,y,w,h)))
    return found[0] if len(found)==1 else None


def scan(book,raw_book,source):
    with ZipFile(book) as archive, ZipFile(raw_book) as originals:
        for name in archive.namelist():
            if not (name.startswith("sheet#") and name.endswith(".xml") and name in originals.namelist()):
                continue
            raw_context={s.get("id"):s for s in ET.fromstring(originals.read(name)).findall("page/system")}
            unit=int(name.split("/")[0][6:])
            image=None
            for system in ET.fromstring(archive.read(name)).findall("page/system"):
                sid=system.get("id")
                if sid not in raw_context:
                    continue
                other=raw_context[sid]
                stack={s.get("id"):s for s in system.findall("stack") if s.get("expected")=="1" and s.get("excess")}
                original_stacks={s.get("id"):s for s in other.findall("stack")}
                if not stack:
                    continue
                graph=Graph(system)
                raw=Graph(other)
                for part in system.findall("part"):
                    staffs=[s.get("id") for s in part.findall("staff")]
                    if len(staffs)!=2:
                        continue
                    peers=[p for p in other.findall("part") if [s.get("id") for s in p.findall("staff")]==staffs]
                    if len(peers)!=1:
                        continue
                    upper,lower=staffs
                    for m in part.findall("measure"):
                        number=m.get("id")
                        if number not in stack or number not in original_stacks:
                            continue
                        a,b=stack[number],original_stacks[number]
                        if any(abs(int(a.get(k))-int(b.get(k)))>3 for k in ("left","right")):
                            continue
                        raw_measure=peers[0].find(f"measure[@id='{number}']")
                        if raw_measure is None:
                            continue
                        rh=graph.chords(m,upper)
                        lh=graph.chords(m,lower)
                        if len(rh)!=16 or len(lh)!=4:
                            continue
                        if not (all(len(graph.children(c))==3 for c in lh)
                                and {v.get("id") for c in rh+lh for v in graph.children(c)}==
                                {v.get("id") for c in raw.chords(raw_measure,upper)+raw.chords(raw_measure,lower) for v in raw.children(c)}):
                            continue
                        figure=groups(graph,rh)
                        if not figure:
                            continue
                        if image is None:
                            png=(source/f"unit-{unit:03d}.png").read_bytes()
                            image=cv2.imdecode(np.frombuffer(png,np.uint8),0)
                            digest=sha256(png).hexdigest()
                        clipped=next(c for c in figure if c["status"]=="clipped")
                        beam=beam_plan(graph,image,figure,clipped)
                        rests=rest_plan(graph,raw,raw_measure,m,lower,lh,image)
                        dot=slur_dot(graph,image,figure)
                        if not (beam and rests and dot):
                            continue
                        plan=[]
                        def emit(*fields):
                            plan.append("\t".join(map(str,fields+(digest,))))
                        for row in beam[0]:
                            emit("BEAM",unit,sid,upper,",".join(clipped["chords"]),",".join(clipped["stems"]),*row)
                        for rest in rests[:2]:
                            emit("REST",unit,sid,lower,number,*box(rest),rest.get("pitch"),rest.get("id"))
                        emit("DOT",unit,sid,upper,*dot[:2],*dot[2])
                        yield dict(unit=unit,system=sid,local_measure=number,
                                   source_sha256=digest,source_multipoint_matches=beam[1],rest_ink=float(rests[2]),
                                   parts=[upper,lower],plan=plan)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for n in ("book","raw","source","evidence","plan"):
        parser.add_argument(n,type=Path)
    a=parser.parse_args()
    found=list(scan(a.book,a.raw,a.source))
    with a.evidence.open("x",encoding="utf8") as o:
        json.dump(found,o,ensure_ascii=False,indent=2)
    if len(found)==1:
        with a.plan.open("x",encoding="utf8") as o:
            o.write("\n".join(found[0]["plan"])+"\n")
    print("source-consistent piano bar proposals",len(found))
