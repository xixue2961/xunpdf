"""Recover the original columns of hollow whole-note tremolo chords."""
from collections import defaultdict
from hashlib import sha256
from itertools import product
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json
import sys

import numpy as np
from PIL import Image

from source_three_stroke_tremolo_ink_v3 import bounds, scan


def isolated_ink(image,cx,cy,step,space):
    radius=round(.45*space)
    half_width=round(1.03*space)
    if min(cx-half_width,cy-radius)<0 or cx+half_width>=image.shape[1] or cy+radius>=image.shape[0]:
        return None
    pixels=image[cy-radius:cy+radius+1,cx-half_width:cx+half_width]
    excluded=(pixels<120).mean(axis=1)>.87
    if step%2==0:
        excluded[radius-1:radius+2]=True
    return pixels,~excluded


def similarity(observed, printed):
    a,usable=observed
    b,reference=printed
    if a.shape!=b.shape:
        return 0
    keep=usable&reference
    if keep.sum()<max(7,.53*len(keep)):
        return 0
    a,b=a[keep],b[keep]
    scores=[]
    for threshold in (120,160,185):
        first,second=a<threshold,b<threshold
        scores.append(float((first&second).sum())/max(1,(first|second).sum()))
    return min(scores)


def original_staff_parts(graph,site):
    part=graph.find(f"part[@id='{site['piano']}']")
    nodes={h.get("id"):h for h in graph.findall("sig/inters/*")}
    chord_ids={c for row in site["chords"] for c in row}
    members=defaultdict(list)
    for edge in graph.findall("sig/relations/*"):
        if edge.get("source") in chord_ids and edge[0].tag=="containment":
            members[edge.get("source")].append(nodes[edge.get("target")])
    return part.findall("staff"),members


def inspect_wholes(image,staffs,members,site):
    donor_bank=[]
    contexts=[]
    for staff,ids in zip(staffs,site["chords"]):
        lines=[float(line.find("point").get("y")) for line in staff.findall("lines/line")]
        if len(lines)!=5:
            return []
        top=lines[0]
        space=(lines[-1]-top)/4
        by_position={}
        for head in [h for c in ids for h in members[c]]:
            x,y,w,h=bounds(head)
            cx,cy=round(x+w/2),round(y+h/2)
            step=round(2*(cy-top)/space)
            column=min(range(2),key=lambda i: abs(cx-site["columns"][i]))
            if abs(cx-site["columns"][column])>3 or (column,step) in by_position:
                return []
            if abs(cy-(top+step*space/2))>.16*space:
                return []
            sample=isolated_ink(image,cx,cy,step,space)
            if sample is not None:
                donor_bank.append((sample,head.get("id")))
            by_position[column,step]=head.get("id")
        contexts.append((staff,top,space,by_position))
    result=[]
    for staff,top,space,known in contexts:
        columns=[]
        for group,x in enumerate(site["columns"]):
            recovered=[]
            for step in range(-15,21):
                cx0,cy0=round(x),round(top+step*space/2)
                best=(0,None,None)
                for dx,dy in product((-1,0,1),repeat=2):
                    cx,cy=cx0+dx,cy0+dy
                    query=isolated_ink(image,cx,cy,step,space)
                    if query is None:
                        continue
                    for donor,head in donor_bank:
                        value=similarity(query,donor)
                        if value>best[0]:
                            best=(value,head,[cx,cy])
                key=(group,step)
                if best[0]>=.84:
                    recovered.append(dict(step=step,confidence=float(best[0]),donor=best[1],
                                          center=best[2],existing_head=known.get(key)))
            if not recovered or any(k not in {r["step"] for r in recovered} for group_index,k in known if group_index==group):
                return []
            columns.append(recovered)
        result.append(dict(staff=staff.get("id"),columns=columns))
    return result


def verify(job):
    report=scan(job)
    sites=[]
    with ZipFile(report["book"]) as archive:
        for site in report["findings"]:
            unit=site["unit"]
            pixels=(job/f"unit-{unit:03d}.png").read_bytes()
            if sha256(pixels).hexdigest()!=site["source_sha256"]:
                continue
            graph=ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml")).find(
                f"page/system[@id='{site['system']}']")
            staffs,members=original_staff_parts(graph,site)
            if site["head_shape"]=="WHOLE_NOTE":
                with Image.open(job/f"unit-{unit:03d}.png") as page:
                    image=np.asarray(page.convert("L"))
                heads=inspect_wholes(image,staffs,members,site)
                if not heads:
                    continue
            else:
                # The existing native stemmed chords remain intact; only the
                # original ink reference and unique identities are recorded here.
                heads=[dict(staff=staff.get("id"),native_chords=[
                            [h.get("id") for h in members[c]] for c in ids])
                       for staff,ids in zip(staffs,site["chords"])]
            sites.append(dict(site,heads=heads))
    return dict(book=report["book"],book_sha256=report["book_sha256"],certified_sites=sites)


if __name__=="__main__":
    job,output=map(Path,sys.argv[1:])
    data=verify(job)
    with output.open("x",encoding="utf-8") as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
    for site in data["certified_sites"]:
        heads=site["heads"]
        counts=[[len(c) for c in v.get("columns",v.get("native_chords",[]))] for v in heads]
        found=[(v["staff"],i,x["step"],round(x["confidence"],3)) for v in heads
               for i,column in enumerate(v.get("columns",[])) for x in column if x["existing_head"] is None]
        print(site["unit"],site["system"],site["stack"],counts,found)
