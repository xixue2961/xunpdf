
"""Verify matching grand-staff wedges and repeated tempo words in original ink."""
import csv
from collections import defaultdict
from functools import lru_cache
from hashlib import file_digest, sha256
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def sha(path):
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def bounds(n):
    return [int(n.find("bounds").get(k)) for k in ("x","y","w","h")]


def lines(n):
    return [sorted((float(p.get("x")), float(p.get("y"))) for p in n.findall(tag+"/*"))
            for tag in ("l1","l2")]


def staff_box(staff):
    y=[float(n.find("point").get("y")) for n in staff.findall("lines/line")]
    return y[0],y[-1],(y[-1]-y[0])/4


def line_support(image, end_points):
    (x0,y0),(x1,y1)=end_points
    x=np.arange(round(x0)+2,round(x1)-1)
    y=np.rint(y0+(y1-y0)*(x-x0)/(x1-x0)).astype(int)
    if not len(x) or x.min()<1 or x.max()+1>=image.shape[1] or y.min()<3 or y.max()+3>=image.shape[0]:
        return 0.0
    return float(np.logical_or.reduce([image[y+d,x]<160 for d in range(-2,3)]).mean())


def wedge_geometry(n,bottom,space):
    if float(n.get("grade","0"))<.65 or n.get("shape") not in {"CRESCENDO","DIMINUENDO"}:
        return None
    legs=lines(n)
    if any(len(leg)!=2 for leg in legs):
        return None
    xs,ys=np.array(legs).transpose(2,0,1)
    left,right=xs.mean(axis=0)
    spread=abs(ys[1]-ys[0])
    kind="crescendo" if spread[0]<spread[1] else "diminuendo"
    if (n.get("shape").lower()!=kind or ys.min()<bottom+.7*space or ys.max()>bottom+6*space
            or right-left<6*space or spread.min()>.25*space or not .7*space<=spread.max()<=3*space):
        return None
    return dict(node=n,left=left,right=right,kind=kind,spread=spread.tolist(),legs=legs)


def paired_wedges(system,part,staffs,by_chord,image):
    graph=system.findall("sig/inters/wedge")
    geoms=[]
    for staff in staffs:
        _,bottom,space=staff_box(staff)
        geoms.append([g for node in graph if (g:=wedge_geometry(node,bottom,space)) is not None])
    for measure,stack in zip(part.findall("measure"),system.findall("stack")):
        start,stop=float(stack.get("left")),float(stack.get("right"))
        pairs=[]
        for upper in geoms[0]:
            for lower in geoms[1]:
                if (upper["kind"]!=lower["kind"] or upper["node"] is lower["node"]
                        or not all(start<=g["left"]<g["right"]<=stop and stop-g["right"]<1.25*space for g in (upper,lower))
                        or max(abs(upper[d]-lower[d]) for d in ("left","right"))>.35*space
                        or max(abs(a-b) for a,b in zip(upper["spread"],lower["spread"]))>.35*space):
                    continue
                pairs.append((upper,lower))
        if len(pairs)!=1:
            continue
        upper,lower=pairs[0]
        candidates=[row for cid in measure.findtext("head-chords","").split()
                    for row in by_chord.get(cid,[]) if abs(int(row["x"])-upper["left"])<1.6*space]
        addresses={(r["part"],r["measure"]) for r in candidates}
        if len(addresses)!=1 or {r["staff"] for r in candidates}!={s.get("id") for s in staffs}:
            continue
        metric=[line_support(image(),leg) for shape in (upper,lower) for leg in shape["legs"]]
        if min(metric)<.97:
            continue
        part_id,measure_id=next(iter(addresses))
        yield dict(part=part_id,measure=measure_id,kind=lower["kind"],
                   native_ids=[x["node"].get("id") for x in (upper,lower)],coverage=metric,
                   lower_staff=str(staffs[1].get("id")),lower_bounds=bounds(lower["node"]),
                   spread_tenths=round(max(lower["spread"])*10/space,2),anchor_rows=candidates,
                   print_x=[float(lower[d]) for d in ("left","right")])


def find_word(image, roi, pattern):
    x,y,x2,y2=roi
    section=image[y:y2,x:x2]
    if section.size==0 or section.shape[0]<pattern.shape[0] or section.shape[1]<pattern.shape[1]:
        return None
    peaks=[]
    for threshold in (120,160,195,208):
        corr=cv2.matchTemplate((section<threshold).astype("uint8"),(pattern<threshold).astype("uint8"),cv2.TM_CCOEFF_NORMED)
        _,score,_,at=cv2.minMaxLoc(corr)
        peaks.append((float(score),x+at[0],y+at[1]))
    if min(p[0] for p in peaks)<.85 or max(p[1] for p in peaks)-min(p[1] for p in peaks)>1 or max(p[2] for p in peaks)-min(p[2] for p in peaks)>1:
        return None
    return dict(bounds=[peaks[1][1],peaks[1][2],pattern.shape[1],pattern.shape[0]],correlations=[p[0] for p in peaks])


def tempo_words(system, upper, by_chord, image, donors):
    sid=upper.get("id")
    space=staff_box(upper)[2]
    nodes={n.get("id"):n for n in system.findall("sig/inters/*")}
    rels=system.findall("sig/relations/*")
    beam_by_stem=defaultdict(list)
    for rel in rels:
        if rel.find("beam-stem") is not None:
            beam_by_stem[rel.get("target")].append(nodes[rel.get("source")])
    for rel in rels:
        chord,stem=rel.get("source"),rel.get("target")
        if rel.find("chord-stem") is None or chord not in by_chord or nodes[chord].get("staff")!=sid:
            continue
        beams=beam_by_stem[stem]
        if len(beams)<3 or any(b.tag!="beam" for b in beams):
            continue
        stem_x,_,_,_=bounds(nodes[stem])
        if any(abs(bounds(b)[0]-stem_x)>.6*space for b in beams):
            continue
        rows=by_chord[chord]
        if len(rows)!=1:
            continue
        row=rows[0]
        x,y,w,h=[int(row[k]) for k in ("x","y","w","h")]
        roi=(max(0,round(x-4.0*space)), max(0,round(y-7.5*space)),
             round(x+3.5*space), round(y-1.3*space))
        for donor in donors:
            if donor["unit"]==int(row["unit"]) or abs(donor["space"]/space-1)>.04:
                continue
            match=find_word(image(),roi,donor["pixels"])
            if match is None:
                continue
            xx,yy,ww,hh=match["bounds"]
            if not abs(xx+ww/2-x-w/2)<=1.2*space or not 1.2*space<=y-(yy+hh)<=3.5*space:
                continue
            yield dict(row=row,text="rit.",reference={k:v for k,v in donor.items() if k!="pixels"},**match)


def scan(job):
    manifest=job/"source-piano-context.json"
    if not manifest.is_file():
        return dict(wedges=[],words=[],state="no_native_piano_mapping")
    info=json.loads(manifest.read_text(encoding="utf8")).get("native_recovery",{})
    if not all(info.get(k) for k in ("native_book","native_head_ledger","native_xml")):
        return dict(wedges=[],words=[],state="no_native_piano_mapping")
    archive,ledger,control=(Path(info[k]) for k in ("native_book","native_head_ledger","native_xml"))
    if not all(p.is_file() for p in (archive,ledger,control)):
        return dict(wedges=[],words=[],state="no_native_piano_mapping")
    files={}
    @lru_cache(maxsize=2)
    def image(u):
        data=(job/f"unit-{u:03d}.png").read_bytes()
        files[u]=sha256(data).hexdigest()
        return cv2.imdecode(np.frombuffer(data,np.uint8),0)
    rows=defaultdict(lambda:defaultdict(list))
    with ledger.open(encoding="utf8",newline="") as f:
        for row in csv.DictReader(f,delimiter="\t"):
            if row["repeat_copy"]=="false":
                rows[(int(row["unit"]),row["system"])][row["chord"]].append(row)
    systems=[]
    donors=[]
    with ZipFile(archive) as z:
        page=None
        for u,system_id in sorted(rows):
            if u!=page:
                page=u
                sheet=ET.fromstring(z.read(f"sheet#{page}/sheet#{page}.xml"))
            system=sheet.find(f"page/system[@id='{system_id}']")
            if system is None:
                continue
            systems.append((u,system_id,system))
            for word in system.findall("sig/inters/word"):
                if word.get("value","").lower().strip()!="rit." or float(word.get("grade","0"))<.55:
                    continue
                x,y,w,h=bounds(word)
                staff=next((s for s in system.findall("part/staff") if s.get("id")==word.get("staff")),None)
                if staff is not None:
                    donors.append(dict(unit=u,system=system_id,id=word.get("id"),space=staff_box(staff)[2],
                                       bounds=[x,y,w,h],pixels=image(u)[y:y+h,x:x+w].copy()))
    wedges,words=[],[]
    for u,system_id,system in systems:
        by_chord=rows[(u,system_id)]
        for part in system.findall("part"):
            staffs=part.findall("staff")
            if len(staffs)!=2:
                continue
            local=lambda: image(u)
            for collection, cases in ((wedges,paired_wedges(system,part,staffs,by_chord,local)),
                                      (words,tempo_words(system,staffs[0],by_chord,local,donors))):
                for case in cases:
                    if case.get("row") and case["row"]["source_sha256"]!=files[u]:
                        continue
                    collection.append(dict(case,unit=u,system=system_id,source_sha256=files[u]))
    return dict(wedges=wedges,words=words,ref_images={d["unit"]:files[d["unit"]] for d in donors},
                native_book_sha256=sha(archive),native_xml_sha256=sha(control))


if __name__=="__main__":
    import sys
    print(json.dumps(scan(Path(sys.argv[1])),ensure_ascii=False))

