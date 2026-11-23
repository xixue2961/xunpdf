"""Locate repeated four-note shapes in an original piano staff."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import argparse
import json
import math

import cv2
import numpy as np


def rect(node):
    return tuple(int(node.find("bounds").get(k)) for k in ("x","y","w","h"))


def center(node):
    x,y,w,h=rect(node)
    return x+w/2,y+h/2


def staff_setup(staff):
    if len(staff.findall("lines/line"))!=5:
        return None
    rows=[]
    for line in staff.findall("lines/line"):
        y=[float(p.get("y")) for p in line.findall("point")]
        if not y or abs(y[-1]-y[0])>2:
            return None
        rows.append(float(np.median(y)))
    return (rows[-1]-rows[0])/4


def within_ink(image, cx, cy, rx, ry):
    x,y=round(cx),round(cy)
    patch=image[y-ry:y+ry+1,x-rx:x+rx+1]
    if patch.shape!=(2*ry+1,2*rx+1):
        return False
    yy,xx=np.mgrid[-ry:ry+1,-rx:rx+1]
    middle=(xx/rx)**2+(yy/ry)**2<=1
    return np.all((patch<120)[middle])


def references(image, members, heads, stacks, space):
    patterns=defaultdict(list)
    for chord,native in members.items():
        if len(native)!=4:
            continue
        selected=sorted((heads.get(h) for h in native if h in heads),key=lambda h:float(h.get("pitch")))
        if len(selected)!=4 or any(n.get("shape")!="NOTEHEAD_BLACK" for n in selected):
            continue
        pitch=tuple(float(n.get("pitch")) for n in selected)
        if len(set(pitch))!=4 or any(p!=round(p) for p in pitch):
            continue
        if not all(within_ink(image,*center(n),max(3,round(space*.27)),max(3,round(space*.22))) for n in selected):
            continue
        x,y,w,h=zip(*map(rect,selected))
        left,top=min(x),min(y)
        width=max(v+w0 for v,w0 in zip(x,w))-left
        height=max(v+h0 for v,h0 in zip(y,h))-top
        if not .95*space<=width<=3.15*space or not 1.35*space<=height<=6.15*space:
            continue
        measures=[a.get("id") for a in stacks if int(a.get("left"))+3<left<int(a.get("right"))-3]
        if len(measures)!=1:
            continue
        relative=[(center(n)[0]-left,center(n)[1]-top) for n in selected]
        patterns[pitch].append({"chord":chord,"measure":measures[0],"left":left,"top":top,
                              "width":width,"height":height,"relative":relative})
    return patterns


def on_staff(image, staff, system, nodes, chord_members):
    space=staff_setup(staff)
    if space is None or not 12<=space<=30:
        return []
    sid=staff.get("id")
    heads={k:v for k,v in nodes.items() if v.tag=="head" and v.get("staff")==sid}
    chords={k:v for k,v in chord_members.items() if nodes[k].get("staff")==sid}
    stacks=system.findall("stack")
    families=references(image,chords,heads,stacks,space)
    missing=[]
    for pitches,templates in families.items():
        if len(templates)<4 or len({d["measure"] for d in templates})<2:
            continue
        # Match in the same printed staff band. Pitch is witnessed by the aligned source heads.
        left,right=int(staff.get("left")),int(staff.get("right"))
        ymin=max(0,min(v["top"] for v in templates)-2)
        ymax=min(image.shape[0],max(v["top"]+v["height"] for v in templates)+2)
        target=image[ymin:ymax,left:right]
        if not target.size:
            continue
        masks=[(target<threshold).astype("uint8") for threshold in (120,150)]
        possibilities=[]
        for ref in sorted(templates,key=lambda r:r["left"]):
            x,y,w,h=[ref[k] for k in ("left","top","width","height")]
            if target.shape[0]<h or target.shape[1]<w:
                continue
            motif=image[y:y+h,x:x+w]
            votes=[cv2.matchTemplate(a,(motif<t).astype("uint8"),cv2.TM_CCOEFF_NORMED)
                   for a,t in zip(masks,(120,150))]
            quality=np.minimum.reduce(votes)
            for _ in range(len(templates)+12):
                _,confidence,_,(u,v)=cv2.minMaxLoc(quality)
                if confidence<.945:
                    break
                possibilities.append((float(confidence),float(u+left),float(v+ymin),ref))
                radius=max(2,round(space*.45))
                quality[:,max(0,u-radius):u+radius+1]=-1
        clusters=[]
        for score,x,y,ref in sorted(possibilities,key=lambda c:-c[0]):
            match=next((c for c in clusters if abs(c[0]-x)<=.24*space and abs(c[1]-y)<=2),None)
            if match is None:
                match=(x,y,[])
                clusters.append(match)
            if not any(t[1]["chord"]==ref["chord"] for t in match[2]):
                match[2].append((score,ref))
        for x,y,votes in clusters:
            measures=[b.get("id") for b in stacks if int(b.get("left"))+3<x<int(b.get("right"))-3]
            if len(measures)!=1:
                continue
            donors=[(v,r) for v,r in votes if r["measure"]!=measures[0]]
            if len(donors)<3 or len({r["measure"] for _,r in donors})<2:
                continue
            centers=[(x+dx,y+dy) for dx,dy in donors[0][1]["relative"]]
            if not all(within_ink(image,cx,cy,max(3,round(space*.25)),max(3,round(space*.19))) for cx,cy in centers):
                continue
            target_notes=[]
            for pitch,(cx,cy) in zip(pitches,centers):
                nearby=[(head,n) for head,n in heads.items()
                        if math.dist(center(n),(cx,cy))<=.46*space]
                if nearby and (len(nearby)!=1 or float(nearby[0][1].get("pitch"))!=pitch):
                    break
                target_notes.append(dict(staff_pitch=pitch,center=[round(cx,1),round(cy,1)],
                                         head=nearby[0][0] if nearby else None))
            if len(target_notes)==4 and any(n["head"] is None for n in target_notes):
                missing.append(dict(staff=sid,stack=measures[0],anchor=[int(x),int(y)],notes=target_notes,
                                    witness_chords=[r["chord"] for _,r in donors],
                                    witness_stacks=sorted({r["measure"] for _,r in donors}),
                                    minimum_similarity=round(min(v for v,_ in donors),5)))
    return missing


def scan(native, source, pages):
    result=[]
    with ZipFile(native) as book:
        for unit in pages:
            raw=source/f"unit-{unit:03}.png"
            data=raw.read_bytes()
            image=cv2.imdecode(np.frombuffer(data,np.uint8),0)
            page=ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml"))
            for system in page.findall("page/system"):
                nodes={n.get("id"):n for n in system.findall("sig/inters/*")}
                members=defaultdict(list)
                for rel in system.findall("sig/relations/relation"):
                    if rel.find("containment") is not None and rel.get("source") in nodes and nodes[rel.get("source")].tag=="head-chord":
                        members[rel.get("source")].append(rel.get("target"))
                for part in system.findall("part"):
                    staves=part.findall("staff")
                    if len(staves)!=2:
                        continue
                    for staff in staves:
                        for candidate in on_staff(image,staff,system,nodes,members):
                            result.append(dict(**candidate,page=unit,system=system.get("id"),
                                               image_sha256=sha256(data).hexdigest()))
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("native",type=Path)
    parser.add_argument("source",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("pages",nargs="+",type=int)
    args=parser.parse_args()
    found=scan(args.native,args.source,args.pages)
    with args.output.open("x",encoding="utf8") as stream:
        json.dump(found,stream,ensure_ascii=False,indent=2)
    print([(c["page"],c["system"],c["staff"],c["stack"],c["anchor"],
            sum(n["head"] is None for n in c["notes"])) for c in found])

