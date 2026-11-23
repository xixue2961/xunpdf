"""Find paired three-stroke piano tremolos in the original page image."""
from collections import defaultdict
from hashlib import sha256
from itertools import combinations, pairwise
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json
import re
import sys

import numpy as np
from PIL import Image


def bounds(symbol):
    box=symbol.find("bounds")
    return tuple(int(box.get(k)) for k in ("x","y","w","h"))


def head_center(nodes):
    return median(x+w/2 for x,_,w,_ in map(bounds,nodes))


def printed_strokes(image,staff,first,second,shift=0,staff_overlap=False):
    lines=[[(float(q.get("x")),float(q.get("y"))) for q in line.findall("point")]
           for line in staff.findall("lines/line")]
    if len(lines)!=5 or any(not line for line in lines):
        return None
    center=(first+second)/2+shift
    gap=second-first
    y_at_center=[float(np.interp(center,*zip(*line))) for line in lines]
    space=median(b-a for a,b in pairwise(y_at_center))
    if not 3.8*space<=gap<=11*space:
        return None
    xs=np.arange(round(center-.185*gap),round(center+.185*gap)+1)
    ys=np.arange(max(0,round(y_at_center[0]-3.1*space)),min(image.shape[0],round(y_at_center[-1]+1.2*space)))
    if len(xs)<1.38*space or xs[0]<0 or xs[-1]>=image.shape[1] or not len(ys):
        return None
    staff_lines=np.array([np.interp(xs,*zip(*line)) for line in lines])
    witnesses=[]
    for slope in np.linspace(-.28,.28,57):
        if not .035<=abs(slope)<=.28:
            continue
        sample_y=np.rint(ys[:,None]+slope*(xs-center)).astype(int)
        if sample_y.min()<0 or sample_y.max()>=image.shape[0]:
            continue
        pixels=image[sample_y,xs]
        valid=(abs(sample_y[None,:,:]-staff_lines[:,None,:])>.11*space).all(axis=0)
        width=valid.sum(axis=1)
        best={}
        for threshold in (120,160):
            fill=((pixels<threshold)&valid).sum(axis=1)/np.maximum(1,width)
            enough=(fill>=.86)&(width>=.52*len(xs))
            supported=enough
            if staff_overlap:
                enough=(pixels<threshold).mean(axis=1)>=.88
            runs=np.flatnonzero(np.diff(np.r_[False,enough,False])).reshape(-1,2)
            runs=[(int(a),int(b)) for a,b in runs if .28*space<=b-a<=(.98 if staff_overlap else .82)*space
                  and (not staff_overlap or supported[a:b].sum()>=max(2,.135*space))]
            for triplet in combinations(runs,3):
                mids=[float(ys[(a+b-1)//2]) for a,b in triplet]
                gaps=[b-a for a,b in pairwise(mids)]
                if min(gaps)<.56*space or max(gaps)>1.15*space or abs(gaps[0]-gaps[1])>.19*space:
                    continue
                quality=sum(float(fill[a:b].sum()) for a,b in triplet)
                if threshold not in best or best[threshold]["quality"]<quality:
                    best[threshold]=dict(slope=float(slope),centers=mids,quality=quality,
                        stripes=[[int(ys[a]),int(ys[b-1])] for a,b in triplet])
        if len(best)==2:
            a,b=best.values()
            if all(abs(x-y)<=.17*space for x,y in zip(a["centers"],b["centers"])):
                witnesses.append(best)
    if not witnesses:
        return None
    matching=max(witnesses,key=lambda pair:min(a["quality"] for a in pair.values()))
    return dict(center_x=center,columns=[int(xs[0]),int(xs[-1])],space=space,thresholds=matching)



def find_strokes(image,staff,first,second,side):
    """Check the natural offset of the strokes from the two note columns."""
    lines=[float(d.find("point").get("y")) for d in staff.findall("lines/line")]
    if len(lines)!=5:
        return None
    space=median(b-a for a,b in pairwise(lines))
    offsets=(0,-side*.55,-side*.85,-side*1.12)
    for overlap in (False,True):
        choices=[]
        for factor in offsets:
            shift=factor*space
            witness=printed_strokes(image,staff,first,second,shift,overlap)
            if witness and all(side*v["slope"]>0 for v in witness["thresholds"].values()):
                witness["overlaps_staff_lines"]=overlap
                choices.append(witness)
        if choices:
            return max(choices,key=lambda a:min(v["quality"] for v in a["thresholds"].values()))
    return None


def scan(job):
    receipt=json.loads((job/"source-native-notation.json").read_text("utf-8"))
    book=Path(receipt["accepted_native_book"])
    findings=[]
    with ZipFile(book) as archive:
        for name in archive.namelist():
            indexed=re.fullmatch(r"sheet#(\d+)/sheet#\1\.xml",name)
            if not indexed:
                continue
            unit=int(indexed[1])
            image=None
            for graph in ET.fromstring(archive.read(name)).findall("page/system"):
                symbols={n.get("id"):n for n in graph.findall("sig/inters/*")}
                members=defaultdict(list)
                for relation in graph.findall("sig/relations/*"):
                    if relation[0].tag=="containment" and relation.get("target") in symbols:
                        members[relation.get("source")].append(symbols[relation.get("target")])
                for part in graph.findall("part"):
                    staffs=part.findall("staff")
                    if len(staffs)!=2:
                        continue
                    upper,lower=staffs
                    letters=[s.get("id") for s in staffs]
                    for measure in part.findall("measure"):
                        ids=measure.findtext("head-chords","").split()
                        kinds=("WHOLE_NOTE","NOTEHEAD_VOID")
                        relevant=[]
                        for staff_id in letters:
                            chords=[i for i in ids if symbols[i].get("staff")==staff_id and members[i]
                                    and all(h.tag=="head" and h.get("shape") in kinds for h in members[i])]
                            relevant.append(sorted(chords,key=lambda i:head_center(members[i])))
                        upper_ids,lower_ids=relevant
                        if len(upper_ids)!=2 or not 1<=len(lower_ids)<=2:
                            continue
                        kind=members[upper_ids[0]][0].get("shape")
                        if any(h.get("shape")!=kind for i in upper_ids+lower_ids for h in members[i]):
                            continue
                        ux=[head_center(members[i]) for i in upper_ids]
                        lx=[head_center(members[i]) for i in lower_ids]
                        if any(min(abs(x-y) for y in ux)>3 for x in lx):
                            continue
                        if image is None:
                            page=job/f"unit-{unit:03d}.png"
                            digest=sha256(page.read_bytes()).hexdigest()
                            with Image.open(page) as pixels:
                                image=np.asarray(pixels.convert("L"))
                        top=find_strokes(image,upper,*ux,side=1)
                        if top is None:
                            continue
                        bottom=find_strokes(image,lower,*ux,side=-1)
                        if bottom is None:
                            continue
                        if top["thresholds"][120]["slope"]*bottom["thresholds"][120]["slope"]>=0:
                            continue
                        findings.append(dict(unit=unit,system=graph.get("id"),stack=measure.get("id"),
                            piano=part.get("id"),staff=letters,head_shape=kind,chords=relevant,
                            columns=ux,source_sha256=digest,strokes={"upper":top,"lower":bottom}))
    return dict(book=str(book),book_sha256=sha256(book.read_bytes()).hexdigest(),findings=findings)


if __name__=="__main__":
    job,out=map(Path,sys.argv[1:])
    result=scan(job)
    with out.open("x",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print([(f["unit"],f["system"],f["stack"],f["head_shape"],f["columns"]) for f in result["findings"]])
