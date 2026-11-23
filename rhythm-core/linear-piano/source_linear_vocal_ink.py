"""Check the printed evidence for a synchronized vocal rhythm at a repaired piano bar."""
import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def box(n):
    return [int(n.find("bounds").get(k)) for k in ("x","y","w","h")]


def xcenter(n):
    x,y,w,h=box(n)
    return x+w/2


def beam_ink(image,first,last,gap):
    a,b=[min(((float(q.get("x")),float(q.get("y"))) for q in s.findall("median/*")),key=lambda p:p[1])
         for s in (first,last)]
    x1,y1=a
    x2,y2=b
    dx=x2-x1
    if not 4*gap<=dx<=8*gap or abs(y2-y1)>gap*.4:
        return None
    def profile(x0,x3,off):
        xs=np.arange(round(x0),round(x3))
        ys=np.rint(y1+(xs-x1)*(y2-y1)/dx+off).astype(int)
        return min(float((image[ys,xs]<t).mean()) for t in (120,175))
    offsets=range(round(.15*gap),round(.50*gap)+1)
    base=max(offsets,key=lambda o:profile(x1+.5*gap,x2-.45*gap,o))
    p=profile(x1+.5*gap,x2-.45*gap,base)
    hook_range=range(base+round(.50*gap),base+round(.88*gap)+1)
    hook=max(hook_range,key=lambda o:profile(x2-1.07*gap,x2-.30*gap,o))
    end=profile(x2-1.07*gap,x2-.30*gap,hook)
    other=profile(x1+.9*gap,x2-2*gap,hook)
    if p>=.92 and end>=.92 and other<=.25:
        return [p,end,other]
    return None


def dot_ink(image, donor, head):
    x,y,w,h=box(donor)
    p,q,ww,hh=box(head)
    template=image[y:y+h,x:x+w]
    left,top=p+ww+1,q-7
    roi=image[top:q+hh+7,left:p+ww+19]
    candidates=cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED)
    _,maximum,_,(dx,dy)=cv2.minMaxLoc(candidates)
    if maximum<.94:
        return None
    sample=roi[dy:dy+h,dx:dx+w]
    quality=[]
    for t in (120,175,205):
        a,b=template<t,sample<t
        quality.append(np.count_nonzero(a&b)/max(1,np.count_nonzero(a|b)))
    if min(quality)<.84:
        return None
    _,labels,stats,_=cv2.connectedComponentsWithStats((roi<175).astype("uint8"),8)
    ids={n for n in np.unique(labels[dy:dy+h,dx:dx+w]) if n}
    round_dots=[n for n in ids if 4<=stats[n,2]<=9 and 4<=stats[n,3]<=9]
    return {"bounds":[left+dx,top+dy,w,h],"similarity":float(maximum)} if len(round_dots)==1 else None


def verify(raw,source,proposal,ledger,measure):
    unit,sys,local=proposal["unit"],proposal["system"],proposal["local_measure"]
    source_bytes=(source/f"unit-{unit:03d}.png").read_bytes()
    if sha256(source_bytes).hexdigest()!=proposal["source_sha256"]:
        raise ValueError("source image changed")
    im=cv2.imdecode(np.frombuffer(source_bytes,np.uint8),0)
    with ZipFile(raw) as z:
        page=ET.fromstring(z.read(f"sheet#{unit}/sheet#{unit}.xml"))
    s=page.find(f"page/system[@id='{sys}']")
    sig=s.find("sig")
    nodes={n.get("id"):n for n in sig.find("inters")}
    rels=list(sig.find("relations"))
    def owns(node,kind):
        return [nodes[r.get("target")] for r in rels if r.get("source")==node and r.find(kind) is not None]
    with ledger.open(encoding="utf8",newline="") as f:
        rows=list(csv.DictReader(f,delimiter="\t"))
    active=[r for r in rows if (r["unit"],r["system"],r["measure"],r["repeat_copy"])==(str(unit),sys,measure,"false")]
    by_staff={}
    for r in active:
        by_staff.setdefault(r["staff"],set()).add(r["part"])
    monophony=[]
    for part in s.findall("part"):
        staves=part.findall("staff")
        if len(staves)!=1:
            continue
        staff=staves[0].get("id")
        if len(by_staff.get(staff,()))!=1:
            return None
        [id]=by_staff[staff]
        bar=part.find(f"measure[@id='{local}']")
        if bar is None or bar.findtext("rest-chords"):
            return None
        heads=[]
        stems=[]
        physical=[]
        for cid in bar.findtext("head-chords","").split():
            h=owns(cid,"containment")
            stem=owns(cid,"chord-stem")
            if len(h)!=1 or len(stem)!=1 or h[0].get("shape")!="NOTEHEAD_BLACK":
                return None
            heads.append(h[0]);stems.append(stem[0]);physical.append(cid)
        if len(heads)!=6:
            return None
        ordered=sorted(zip(heads,stems,physical),key=lambda p:xcenter(p[0]))
        monophony.append((id,staves[0],ordered))
    if len(monophony)!=4:
        return None
    upper,*middle,lower=monophony
    for lane in (*middle,lower):
        if any(abs(xcenter(a[0])-xcenter(b[0]))>4 for a,b in zip(upper[2],lane[2])):
            return None
    for lane in monophony:
        recognized={r["head"]:r for r in active if r["part"]==lane[0]}
        expected=[head.get("id") for head,stem,c in lane[2]]
        if set(recognized)!=set(expected[:-1] if lane is upper else expected):
            return None
    top_id,staff,up=upper
    bottom_id,_,down=lower
    prev,ending=up[-2:]
    yl=[median(float(p.get("y")) for p in n.findall("point")) for n in staff.findall("lines/line")]
    gap=median(a-b for a,b in zip(yl[1:],yl))
    if not 8<gap<30:
        return None
    beams=beam_ink(im,prev[1],ending[1],gap)
    digits=[nodes[r.get("source")] for r in rels if r.get("target")==prev[0].get("id") and r.find("augmentation") is not None]
    if len(digits)!=1 or not beams:
        return None
    dot=dot_ink(im,digits[0],down[-2][0])
    if dot is None:
        return None
    rows_end=[n for n in nodes.values() if n.tag=="lyric-item" and n.get("staff")==staff.get("id") and n.get("value") 
              and n.find("bounds") is not None and 0<=xcenter(ending[0])-box(n)[0]<=1.3*gap
              and 25<=box(n)[1]-box(ending[0])[1]<=90]
    if len(rows_end)!=1:
        return None
    note=ending[0]
    neighbors=[i for i,(h,_,_) in enumerate(up[:-2]) if h.get("pitch")==note.get("pitch")
               and abs(box(h)[1]-box(note)[1])<=2]
    if len(neighbors)<2:
        return None
    return {"source_sha256":proposal["source_sha256"],"measure":measure,"parts":[p[0] for p in monophony],
            "missing_note":note.get("id"),"neighbor_indices":neighbors,
            "source_lyric_character":rows_end[0].get("value")[0],
            "beam_support":beams,"bass_dot":dot}


if __name__=="__main__":
    p=argparse.ArgumentParser()
    for k in ("raw","source","proposal","ledger","measure","output"):
        p.add_argument(k,type=str if k=="measure" else Path)
    a=p.parse_args()
    [proposal]=json.loads(a.proposal.read_text(encoding="utf8"))
    report=verify(a.raw,a.source,proposal,a.ledger,a.measure)
    if not report:
        raise SystemExit("source vocal figure cannot be fully confirmed")
    with a.output.open("x",encoding="utf8") as o:json.dump(report,o,ensure_ascii=False,indent=2)
    print("Source-backed vocal beam, note, lyric and dot confirmed")
