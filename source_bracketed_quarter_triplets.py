"""Use staff-aligned chord columns and both printed bracket rails to locate quarter triplets."""
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from collections import defaultdict

import cv2
import numpy as np
from PIL import Image


def box(node):
    n=node.find("bounds")
    return tuple(int(n.get(k)) for k in ("x","y","w","h"))


def staff_extent(staff):
    lines=staff.findall("lines/line")
    if len(lines)!=5:
        return None
    y=[float(line.find("point").get("y")) for line in lines]
    return y[0],y[-1],(y[-1]-y[0])/4


def chord_map(system, nodes):
    heads=defaultdict(list)
    beams=set()
    for relation in system.findall("sig/relations/relation"):
        a,b=relation.get("source"),relation.get("target")
        if a not in nodes or b not in nodes:
            continue
        if relation.find("containment") is not None and nodes[b].get("shape")=="NOTEHEAD_BLACK":
            heads[a].append(nodes[b])
        if relation.find("beam-stem") is not None:
            beams.add(b)
    beamed=set(r.get("source") for r in system.findall("sig/relations/relation")
               if r.find("chord-stem") is not None and r.get("target") in beams)
    return {ch:(round(float(np.median([box(h)[0] for h in members]))),[h.get("id") for h in members])
            for ch,members in heads.items() if ch not in beamed and nodes[ch].tag=="head-chord"}


def template_digits(gray, system):
    return [np.ascontiguousarray(gray[y:y+h,x:x+w]<148,dtype=np.uint8)
            for digit in system.findall("sig/inters/tuplet")
            if digit.get("shape")=="TUPLET_THREE" and .50<=float(digit.get("grade","0"))
            for x,y,w,h in [box(digit)] if 9<=w<=26 and 14<=h<=34]


def digit_hits(gray, at, rail_y, space, donors):
    y0,y1=map(round,rail_y)
    x0,x1=round(at-1.6*space),round(at+1.6*space)
    tile=np.ascontiguousarray(gray[y0:y1,x0:x1]<148,dtype=np.uint8)
    hits=[]
    for tpl in donors:
        h,w=tpl.shape
        if h>tile.shape[0] or w>tile.shape[1]:
            continue
        result=cv2.matchTemplate(tile,tpl,cv2.TM_CCOEFF_NORMED)
        yy,xx=np.where(result>=.82)
        hits.extend((float(result[y,x]),int(x)+x0,int(y)+y0,w,h) for y,x in zip(yy,xx))
    selected=[]
    for quality,x,y,w,h in sorted(hits,reverse=True):
        if all(abs(y+h/2-(sy+sh/2))>.28*space for _,sx,sy,sw,sh in selected):
            selected.append((quality,x,y,w,h))
    return selected


def rail_pair(gray,left,right,digit,space):
    _,x,y,w,h=digit
    middle=x+w/2
    # Trim the bracket corners and the digit gap before fitting the central rails.
    l0,l1=round(left+.72*space),round(x-.44*space)
    r0,r1=round(x+w+.44*space),round(right-.60*space)
    if min(l1-l0,r1-r0)<1.15*space:
        return None
    xs=np.r_[np.arange(l0,l1),np.arange(r0,r1)]
    ink=gray<145
    offsets=np.arange(-2,3)
    best=None
    for slope in np.arange(-.135,.136,.01):
        for dy in range(-round(.37*space),round(.37*space)+1):
            center=y+h/2+dy
            yy=np.rint(center+(xs-middle)*slope).astype(int)
            flags=ink[yy[:,None]+offsets,xs[:,None]].any(1)
            coverage=min(float(flags[:l1-l0].mean()),float(flags[l1-l0:].mean()))
            if best is None or coverage>best[0]:
                best=(coverage,float(slope),float(center))
    if best is None or best[0]<.85:
        return None
    coverage,slope,center=best
    # At least one vertical hook must agree with the sloped line.
    hooks={1:0,-1:0}
    count=round(.52*space)
    for base in (left,right):
        for x in range(round(base-.56*space),round(base+.81*space)):
            y=round(center+(x-middle)*slope)
            for side in hooks:
                ys=y+side*np.arange(2,count+2)
                support=float(ink[ys,x-1:x+2].any(1).mean())
                hooks[side]=max(hooks[side],support)
    if max(hooks.values())<.65:
        return None
    if max(hooks.values())-min(hooks.values())<.14:
        return None
    return dict(rail_coverage=coverage,slope=slope,y=center,hook=max(hooks,key=hooks.get),
                digit_bounds=list(digit[1:]),digit_similarity=digit[0])


def scan(book, images):
    results=[]
    with ZipFile(book) as archive:
        for path in archive.namelist():
            if not path.startswith("sheet#") or not path.endswith(".xml") or path.count("/")!=1:
                continue
            unit=int(path.split("/")[0].split("#")[1])
            page=ET.fromstring(archive.read(path))
            if not any(s.find("sig/inters/tuplet[@shape='TUPLET_THREE']") is not None for s in page.findall("page/system")):
                continue
            with Image.open(Path(images)/f"unit-{unit:03}.png") as im:
                gray=np.asarray(im.convert("L"))
            for s in page.findall("page/system"):
                refs=template_digits(gray,s)
                if len(refs)<2:
                    continue
                nodes={n.get("id"):n for n in s.findall("sig/inters/*")}
                heads=chord_map(s,nodes)
                for part in s.findall("part"):
                    staves=part.findall("staff")
                    if len(staves)!=2:
                        continue
                    upper,lower=map(staff_extent,staves)
                    if upper is None or lower is None:
                        continue
                    ids=[n.get("id") for n in staves]
                    space=(upper[2]+lower[2])/2
                    if not 11<=space<=26 or lower[0]-upper[1]<3*space:
                        continue
                    for measure in part.findall("measure"):
                        groups=[sorted((heads[c][0],c,heads[c][1]) for c in measure.findtext("head-chords","").split()
                                       if c in heads and nodes[c].get("staff")==staff) for staff in ids]
                        for start in range(len(groups[0])-2):
                            hand=groups[0][start:start+3]
                            x=[v[0] for v in hand]
                            if not 2*space<=x[1]-x[0]<=7*space or not 2*space<=x[2]-x[1]<=7*space:
                                continue
                            lower_pair=[[h for h in groups[1] if abs(h[0]-a)<.24*space] for a in x]
                            if any(len(p)>1 for p in lower_pair) or sum(bool(p) for p in lower_pair)<2:
                                continue
                            bounds=(upper[1]+.25*space,lower[0]-.08*space)
                            found=defaultdict(list)
                            for hit in digit_hits(gray,x[1]+.67*space,bounds,space,refs):
                                proof=rail_pair(gray,x[0],x[2]+.9*space,hit,space)
                                if proof is not None:
                                    found[proof["hook"]].append(proof)
                            if len(found[1])!=1 or len(found[-1])!=1 or found[1][0]["y"]<=found[-1][0]["y"]:
                                continue
                            results.append(dict(unit=unit,system=s.get("id"),measure=measure.get("id"),columns=x,
                                                upper=[list(v) for v in hand],lower=[list(v[0]) if v else None for v in lower_pair],
                                                music_status="unresolved_principal_chord" if not all(lower_pair) else "all_chords_located",
                                                upper_bracket=found[-1][0],lower_bracket=found[1][0]))
    return results

def write_evidence(book, job, output):
    from hashlib import sha256
    import json
    source=Path(book)
    digest=sha256(source.read_bytes()).hexdigest()
    groups=scan(source,job)
    units={g["unit"] for g in groups}
    data=dict(book_sha256=digest,groups=groups,
              raster_sha256={u:sha256((Path(job)/f"unit-{u:03d}.png").read_bytes()).hexdigest() for u in units})
    if sha256(source.read_bytes()).hexdigest()!=digest:
        raise ValueError("source book changed during bracket scan")
    with Path(output).open("x",encoding="utf8") as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)


if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser()
    for key in ("book","job","output"):
        ap.add_argument("--"+key,type=Path,required=True)
    args=ap.parse_args()
    write_evidence(args.book,args.job,args.output)
