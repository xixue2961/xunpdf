"""Scan original images for a paired two-stroke, stemless grand-staff tremolo."""
from collections import defaultdict
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import re
import sys
import json

import cv2
import numpy as np


def bounds(node):
    return tuple(int(node.find("bounds").get(key)) for key in ("x","y","w","h"))


def stroke_pair(image, staff, first, second):
    lines = [np.array([(float(p.get("x")), float(p.get("y"))) for p in line.findall("point")])
             for line in staff.findall("lines/line")]
    if len(lines)!=5 or any(not len(line) for line in lines):
        return None
    at = [float(np.median(line[:,1])) for line in lines]
    space = float(np.median(np.diff(at)))
    xs0, xs1 = (n[0]+n[2]/2 for n in (first,second))
    span = xs1-xs0
    if not 4.6*space <= span <= 10*space:
        return None
    top = max(0,int(at[0]-1.8*space))
    bottom = min(image.shape[0],int(at[-1]+.6*space))
    rows = np.arange(top,bottom)
    choices = {}
    width = round(2.2*space)
    for left in range(round(xs0+.60*space), round(xs1-width-.30*space), 2):
        xs = np.arange(left,left+width)
        center = float(xs.mean())
        staff_lines = np.array([np.interp(xs,line[:,0],line[:,1]) for line in lines])
        for slope in np.r_[np.arange(-.30,-.075,.015),np.arange(.075,.301,.015)]:
            sample = np.rint(rows[:,None]+slope*(xs-center)).astype(int)
            patch=image[sample,xs]
            available=(abs(sample[None,:,:]-staff_lines[:,None,:])>.16*space).all(axis=0)
            support=available.mean(axis=1)
            for threshold in (160,208):
                ratios=((patch<threshold)&available).sum(axis=1)/np.maximum(1,available.sum(axis=1))
                mask=(ratios>=.86)&(support>=.56)
                edges=np.flatnonzero(np.diff(np.r_[False,mask,False])).reshape(-1,2)
                bands=[(a,b,(a+b-1)/2) for a,b in edges if .27*space<=b-a<=.89*space]
                for one,two in combinations(bands,2):
                    if not .58*space<=two[2]-one[2]<=1.05*space:
                        continue
                    if abs((one[1]-one[0])-(two[1]-two[0]))>.23*space:
                        continue
                    if any(.54*space<=one[2]-v[2]<=1.1*space or .54*space<=v[2]-two[2]<=1.1*space for v in bands):
                        continue
                    score=sum(float(ratios[a:b].mean())*(b-a) for a,b,_ in (one,two))
                    if threshold not in choices or score>choices[threshold]["quality"]:
                        choices[threshold]=dict(left=left,right=left+width,slope=float(slope),
                                                centers=[float(top+one[2]),float(top+two[2])],bands=[(top+one[0],top+one[1]-1),(top+two[0],top+two[1]-1)],quality=score)
    if len(choices)!=2:
        return None
    a,b=choices[160],choices[208]
    if abs(a["slope"]-b["slope"])>.06 or abs(a["left"]-b["left"])>.8*space:
        return None
    offset=(b["left"]-a["left"])*(a["slope"]+b["slope"])/2
    if max(abs(y+offset-z) for y,z in zip(a["centers"],b["centers"]))>.22*space:
        return None
    return dict(strokes=2,interline=space,thresholds=choices)


def scan(book, source):
    seen=[]
    with ZipFile(book) as archive:
        for name in archive.namelist():
            match=re.fullmatch(r"sheet#([1-9][0-9]*)/sheet#\1\.xml",name)
            if not match:
                continue
            unit=int(match[1])
            payload=(source/f"unit-{unit:03d}.png").read_bytes()
            image=cv2.imdecode(np.frombuffer(payload,np.uint8),0)
            for system in ET.fromstring(archive.read(name)).findall("page/system"):
                inters={n.get("id"):n for n in system.findall("sig/inters/*")}
                graph=defaultdict(list)
                for edge in system.findall("sig/relations/relation"):
                    graph[edge.get("source")].append((edge[0].tag,edge.get("target"),True))
                    graph[edge.get("target")].append((edge[0].tag,edge.get("source"),False))
                stacks={s.get("id"):s for s in system.findall("stack") if s.get("expected")=="1" and s.get("special")!="CAUTIONARY"}
                for part in system.findall("part"):
                    if len(part.findall("staff"))!=2:
                        continue
                    for measure in part.findall("measure"):
                        if measure.get("id") not in stacks or (not stacks[measure.get("id")].get("excess") and measure.get("abnormal")!="true"):
                            continue
                        pairs=[]
                        for staff in part.findall("staff"):
                            sid=staff.get("id")
                            chords=[i for i in measure.findtext("head-chords","").split() if inters[i].get("staff")==sid]
                            if len(chords)!=2:
                                break
                            chords.sort(key=lambda i:bounds(inters[i])[0])
                            heads=[[inters[obj] for relation,obj,outgoing in graph[key] if outgoing and relation=="containment"]
                                   for key in chords]
                            if any(not group or not all(n.tag=="head" and n.get("shape")=="WHOLE_NOTE" for n in group) for group in heads):
                                break
                            if any(relation not in {"containment","slur-head","alter-head","chord-articulation","chord-sentence","chord-dynamics"}
                                   for items in [graph[key] for key in chords] for relation,_,_ in items):
                                break
                            proof=stroke_pair(image,staff,*(bounds(inters[key]) for key in chords))
                            if not proof:
                                break
                            pairs.append(dict(staff=sid,chords=chords,native_heads=[[h.get("id") for h in group] for group in heads],**proof))
                        if len(pairs)==2 and max(len(group) for p in pairs for group in p["native_heads"])>=2:
                            seen.append(dict(unit=unit,system=system.get("id"),stack=measure.get("id"),staves=pairs,
                                             source_sha256=sha256(payload).hexdigest()))
    return seen


if __name__=="__main__":
    book,source,output=map(Path,sys.argv[1:])
    findings=scan(book,source)
    with output.open("x",encoding="utf-8") as f:
        json.dump(dict(book_sha256=sha256(book.read_bytes()).hexdigest(),findings=findings),f,indent=2)
    print([(row["unit"],row["system"],row["stack"],[p["chords"] for p in row["staves"]]) for row in findings])
