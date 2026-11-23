"""Verify original articulations and paired hairpins within a native rhythm repair."""
import argparse
import csv
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def bounds(node):
    return [int(node.find("bounds").get(k)) for k in ("x","y","w","h")]


def match(image, bank, left, top, right, bottom, center, threshold, center_limit):
    if left < 0 or top < 0 or right > image.shape[1] or bottom > image.shape[0]:
        return None
    cut = image[top:bottom,left:right]
    verified = []
    for donor, template in bank:
        h,w = template.shape
        if h > cut.shape[0] or w > cut.shape[1]:
            continue
        locations = cv2.matchTemplate(cut,template,cv2.TM_CCOEFF_NORMED)
        for y,x in np.argwhere(locations >= threshold):
            if abs(left+x+w/2-center)>center_limit:
                continue
            patch = cut[y:y+h,x:x+w]
            agreement=[]
            for ink in (135,180):
                a,b = patch<ink,template<ink
                union = np.count_nonzero(a|b)
                agreement.append(np.count_nonzero(a&b)/max(1,union))
            quality = min(float(locations[y,x]),*agreement)
            if min(agreement)>= (.77 if w <= 8 else .83):
                verified.append((quality,donor,[left+int(x),top+int(y),w,h]))
    if not verified:
        return None
    best=max(verified)
    peers={donor for _,donor,rect in verified if abs(rect[0]-best[2][0])<=3 and abs(rect[1]-best[2][1])<=3}
    if len(peers)<2:
        return None
    return {"bounds":best[2],"similarity":best[0],"source_peers":sorted(peers)}


def ink_pair(image, a, b):
    x,y,w,h = a
    xx,yy,ww,hh = b
    if abs(x-xx)>2 or abs(w-ww)>2 or abs(h-hh)>2 or w < 5*h:
        return None
    one=image[y:y+h,x:x+w]
    two=image[yy:yy+hh,xx:xx+ww]
    if one.shape!=two.shape:
        two=cv2.resize(two,(w,h),interpolation=cv2.INTER_AREA)
    q=[]
    for threshold in (135,180):
        p,r=one<threshold,two<threshold
        q.append(np.count_nonzero(p&r)/max(1,np.count_nonzero(p|r)))
    return min(q)


def scan(apply, source, plan):
    beams=[]
    for line in plan.read_text(encoding="utf8").splitlines():
        f=line.split("\t")
        if f[0]=="BEAM":
            beams.append(f)
    units={(int(f[1]),f[2],f[-1]) for f in beams}
    if len(units)!=1:
        return None
    [(unit,sid,digest)]=units
    selected={v for f in beams for v in f[4].split(",")}
    original=(source/f"unit-{unit:03d}.png").read_bytes()
    if sha256(original).hexdigest()!=digest:
        return None
    image=cv2.imdecode(np.frombuffer(original,np.uint8),0)
    with ZipFile(apply/"refined.omr") as archive:
        system=ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml")).find(f"page/system[@id='{sid}']")
    by_chord=defaultdict(list)
    with (apply/"head-ledger.tsv").open(encoding="utf8",newline="") as stream:
        rows=list(csv.DictReader(stream,delimiter="\t"))
    destination={(r["part"],r["measure"]) for r in rows if r["unit"]==str(unit) and r["system"]==sid
                 and r["chord"] in selected and r["repeat_copy"]=="false"}
    if len(destination)!=2 or len({p for p,m in destination})!=1:
        return None
    for r in rows:
        if (r["part"],r["measure"]) in destination and r["repeat_copy"]=="false":
            if r["unit"]!=str(unit) or r["system"]!=sid or r["source_sha256"]!=digest or r["shape"]!="NOTEHEAD_BLACK":
                return None
            by_chord[r["chord"]].append(r)
    if not selected<=by_chord.keys():
        return None
    sig=system.find("sig")
    nodes={n.get("id"):n for n in sig.find("inters")}
    templates={"ACCENT":[],"STACCATO":[]}
    for r in sig.find("relations"):
        if r.find("chord-articulation") is None or r.get("source") not in by_chord:
            continue
        shape=nodes[r.get("target")]
        kind=shape.get("shape")
        if kind not in templates or float(shape.get("ctx-grade","0"))<.85:
            continue
        x,y,w,h=bounds(shape)
        start=by_chord[r.get("source")][0]
        if kind=="STACCATO" and (start["staff_number"]!="2" or w not in (5,6,7) or h not in (5,6,7)):
            continue
        templates[kind].append((shape.get("id"),image[y:y+h,x:x+w]))
    if any(len(v)<3 for v in templates.values()):
        return None

    articulations=[]
    anchors=defaultdict(list)
    for chord, group in by_chord.items():
        r=min(group,key=lambda row:int(row["note_index"]))
        boxes=[[int(q[k]) for k in ("x","y","w","h")] for q in group]
        top=min(y for x,y,w,h in boxes)
        bottom=max(y+h for x,y,w,h in boxes)
        x=round(np.median([x+w/2 for x,y,w,h in boxes]))
        anchors[r["part"],r["measure"],r["staff_number"]].append((x,int(r["note_index"])))
        upper=r["staff_number"]=="1"
        if upper:
            y0,y1=top-27,top-4
        else:
            y0,y1=bottom+1,bottom+29
        hit=match(image,templates["ACCENT"],x-17,y0,x+18,y1,x,.91,5)
        dot=match(image,templates["STACCATO"],x-12,y0,x+13,y1,x,.90,5)
        if dot and hit:
            ax,ay,aw,ah=hit["bounds"]
            bx,by,bw,bh=dot["bounds"]
            if ax<bx+bw and bx<ax+aw and ay<by+bh and by<ay+ah:
                dot=None
        marks={}
        if hit:
            marks["accent"]=hit
        if dot:
            marks["staccato"]=dot
        articulations.append({"part":r["part"],"measure":r["measure"],"staff":r["staff_number"],
                              "note_index":int(r["note_index"]),"marks":marks})
    report={"source_sha256":digest,"unit":unit,"system":sid,"articulations":articulations,"wedges":[]}
    for part,measure in sorted(destination,key=lambda x:(x[0],int(x[1]))):
        upper=sorted(anchors[part,measure,"1"])
        lower=sorted(anchors[part,measure,"2"])
        if len(upper)!=12 or len(lower)!=8:
            return None
        left,right=upper[0][0]-17,upper[-1][0]+34
        lower_head_y=max(int(r["y"])+int(r["h"]) for g in by_chord.values() for r in g
                         if (r["part"],r["measure"],r["staff_number"])==(part,measure,"2"))
        wedges=[n for n in sig.find("inters") if n.tag=="wedge" and n.get("shape")=="CRESCENDO"
                and left<=bounds(n)[0] and bounds(n)[0]+bounds(n)[2]<=right
                and 0.6 < float(n.get("grade","0"))]
        if not wedges:
            continue
        if len(wedges)!=2:
            return None
        a,b=sorted(map(bounds,wedges),key=lambda v:v[1])
        quality=ink_pair(image,a,b)
        if quality is None or quality<.72 or not a[1]<lower_head_y<b[1] or b[1]-lower_head_y>90:
            return None
        ends=[int(stack.get("right")) for stack in system.findall("stack")
              if int(stack.get("left"))<upper[0][0]<upper[-1][0]<int(stack.get("right"))]
        if len(ends)!=1 or not 0<=ends[0]-a[0]-a[2]<=20 or not 0<=ends[0]-b[0]-b[2]<=20:
            return None
        for staff,rect,events in (("1",a,upper),("2",b,lower)):
            x,y,w,h=rect
            at,note=min(events,key=lambda row:abs(row[0]-x))
            if abs(at-x)>13 or x+w<=events[-1][0]:
                return None
            report["wedges"].append(dict(part=part,measure=measure,staff=staff,first_note_index=note,
                                         stop_at_measure_end=True,original_bounds=rect,matching_other_staff=quality))
    first=min(destination,key=lambda x:int(x[1]))
    start_x,start_index=min(anchors[*first,"1"])
    first_chord=next(g for g in by_chord.values() if (g[0]["part"],g[0]["measure"],g[0]["staff_number"])==(*first,"1")
                     and min(int(r["note_index"]) for r in g)==start_index)
    first_head_y=min(int(r["y"]) for r in first_chord)
    tempos=[bounds(n) for n in sig.find("inters") if n.tag=="metronome" and
            start_x-65<bounds(n)[0]<start_x and first_head_y-90<bounds(n)[1]<first_head_y-12]
    report["tempo_before_first_attack"]=len(tempos)==1
    return report


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    for arg in ("apply","source","plan","output"):
        ap.add_argument(arg,type=Path)
    opts=ap.parse_args()
    proof=scan(opts.apply,opts.source,opts.plan)
    if proof is None:
        raise SystemExit("original printed expressions are not uniquely proven")
    from collections import Counter
    print(Counter((r["measure"],r["staff"],key) for r in proof["articulations"] for key in r["marks"]))
    print(proof["wedges"],proof["tempo_before_first_attack"])
    with opts.output.open("x",encoding="utf8") as out:
        json.dump(proof,out,indent=2)
