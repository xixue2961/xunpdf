"""Identify articulation fragments whose source ink belongs to the repaired bar's slurs."""
import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def rect(node):
    return [int(node.find("bounds").get(k)) for k in ("x","y","w","h")]


def source_anchors(apply, source, proposed):
    [spec]=json.loads(proposed.read_text(encoding="utf8"))
    unit,sid=spec["unit"],spec["system"]
    upper,lower=spec["parts"]
    beam_rows=[line.split("\t") for line in spec["plan"] if line.startswith("BEAM\t")]
    linked={k for row in beam_rows for k in row[4].split(",")}
    full_image=(source/f"unit-{unit:03d}.png").read_bytes()
    digest=sha256(full_image).hexdigest()
    if digest!=spec["source_sha256"]:
        raise ValueError("original image differs from the beam proof")
    image=cv2.imdecode(np.frombuffer(full_image,np.uint8),0)
    ledger=apply/"head-ledger.tsv"
    with ledger.open(encoding="utf8",newline="") as inside:
        rows=list(csv.DictReader(inside,delimiter="\t"))
    scopes={(r["part"],r["measure"]) for r in rows if r["unit"]==str(unit) and r["system"]==sid
             and r["chord"] in linked and r["repeat_copy"]=="false"}
    if len(scopes)!=1:
        raise ValueError("physical source chord scope is ambiguous")
    part,measure=scopes.pop()
    rows=[r for r in rows if (r["part"],r["measure"],r["unit"],r["system"],r["repeat_copy"])==(part,measure,str(unit),sid,"false")]
    if len(rows)!=30 or {r["source_sha256"] for r in rows}!={digest}:
        raise ValueError("source note scope changed after native reflow")
    heads={r["head"] for r in rows if r["staff"]==upper}
    chords={r["chord"] for r in rows if r["staff"]==upper}
    book=apply/"refined.omr"
    with ZipFile(book) as z:
        sys=ET.fromstring(z.read(f"sheet#{unit}/sheet#{unit}.xml")).find(f"page/system[@id='{sid}']")
    sig=sys.find("sig")
    nodes={v.get("id"):v for v in sig.find("inters")}
    links=list(sig.find("relations"))
    arcs={}
    for rel in links:
        if rel.find("slur-head") is not None:
            arcs.setdefault(rel.get("source"),set()).add(rel.get("target"))
    true_curves=[nodes[k] for k,ids in arcs.items() if len(ids)==2 and ids<=heads]
    if len(true_curves)!=4:
        raise ValueError("source curve anchors are incomplete")
    boxes=[rect(n) for n in true_curves]
    x0=min(x for x,y,w,h in boxes)-12
    y0=min(y for x,y,w,h in boxes)-16
    x1=max(x+w for x,y,w,h in boxes)+8
    y1=max(y+h for x,y,w,h in boxes)+6
    masks=[]
    for t in (120,175,205):
        _,labels,stats,_=cv2.connectedComponentsWithStats((image[y0:y1,x0:x1]<t).astype("uint8"),8)
        masks.append((labels,stats))
    marks=[]
    supported={"STACCATO":"staccato","STACCATISSIMO_BELOW":"staccatissimo","STACCATISSIMO_ABOVE":"staccatissimo"}
    for rel in links:
        chord=rel.get("source")
        if chord not in chords or rel.find("chord-articulation") is None:
            continue
        item=nodes[rel.get("target")]
        if item.get("shape") not in supported:
            raise ValueError("unknown right-hand articulation in an ambiguous curve")
        x,y,w,h=rect(item)
        if x<x0 or y<y0 or x+w>x1 or y+h>y1:
            raise ValueError("printed articulation lies outside the proven curves")
        for labels,stats in masks:
            region=labels[y-y0:y+h-y0,x-x0:x+w-x0]
            ink=[(i,int(k)) for i,k in zip(*np.unique(region,return_counts=True)) if i]
            if not ink or any(stats[i,2]<50 for i,k in ink if k>3):
                raise ValueError("independent source articulation must be preserved")
        index=min(int(r["note_index"]) for r in rows if r["chord"]==chord)
        marks.append({"note_index":index,"kind":supported[item.get("shape")],"source_node":item.get("id"),"bounds":[x,y,w,h]})
    return {"part":part,"measure":measure,"source_sha256":digest,"marks":marks,
            "book_sha256":sha256(book.read_bytes()).hexdigest(),"ledger_sha256":sha256(ledger.read_bytes()).hexdigest()}


if __name__=="__main__":
    p=argparse.ArgumentParser()
    for k in ("apply","source","plan_evidence","output"):
        p.add_argument(k,type=Path)
    a=p.parse_args()
    report=source_anchors(a.apply,a.source,a.plan_evidence)
    with a.output.open("x",encoding="utf8") as o:
        json.dump(report,o,ensure_ascii=False,indent=2)
    print("Source curve fragments",len(report["marks"]))
