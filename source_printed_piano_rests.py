"""Recover printed rests from a piano lane whose silence was exported as a forward."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import inherited_context, signature
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor

RESTS={"EIGHTH_REST":("eighth",F(1,2)), "QUARTER_REST":("quarter",F(1))}


def raw_rests(g,measure,nodes,staff):
    ids=set(measure.findtext("rest-chords","").split())
    return [nodes[r.get("target")] for r in g.findall("sig/relations/*")
            if r.get("source") in ids and r.find("containment") is not None
            and r.get("target") in nodes and nodes[r.get("target")].get("staff")==staff
            and nodes[r.get("target")].get("shape") in RESTS]


def evidence(image,target,candidates):
    x,y,w,h=coordinate(target)
    if not 11<=w<=21 or not 25<=h<=52:
        return None
    # Same raster and staff fixes scale, font and rail height.
    for donor in candidates:
        u,v,dw,dh=coordinate(donor)
        if abs(v-y)>2 or abs(dw-w)>2 or abs(dh-h)>2 or abs(u-x)<25:
            continue
        anchor=image[y:y+h,x:x+w]
        if anchor.shape!=(h,w):
            continue
        ratios=[]
        for threshold,required in ((110,.74),(155,.79),(185,.83)):
            a=anchor<threshold
            matches=[]
            for dx in (-2,-1,0,1,2):
                for dy in (-2,-1,0,1,2):
                    crop=image[v+dy:v+dy+h,u+dx:u+dx+w]
                    if crop.shape!=a.shape:
                        continue
                    b=crop<threshold
                    keep=(a.sum(1)<.83*w)&(b.sum(1)<.83*w)
                    union=(a[keep]|b[keep]).sum()
                    if keep.sum()>=.59*h and union>=40:
                        matches.append(float((a[keep]&b[keep]).sum())/union)
            if not matches or max(matches)<required:
                break
            ratios.append(round(max(matches),4))
        if len(ratios)==3:
            return dict(source=target.get("id"),peer=donor.get("id"),box=[x,y,w,h],similarity=ratios)
    return None


def recover(root,job):
    job=Path(job)
    config=job/"source-native-notation.json"
    raw=job/"raw/input.omr"
    prep=job/"source-native-preflight.json"
    if not all(p.is_file() for p in (config,raw,prep)):
        return root,dict(state="not_applicable",changes=[])
    meta=json.loads(config.read_text("utf8"))
    book,xml=(Path(meta[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if sha(book)!=meta.get("accepted_native_sha256") or sha(raw)!=json.loads(prep.read_text("utf8")).get("original_native_sha256"):
        return root,dict(state="not_applicable",changes=[])
    cloned=deepcopy(root)
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    current,_=_score_events(cloned)
    archived,_=_score_events(score(xml))
    notes=defaultdict(dict)
    for r in rows(xml.with_name("note-ledger.tsv")):
        k=r["part"],r["measure"],int(r["note_index"])
        if (k[0] in piano and k in current and k in archived and signature(current[k])==signature(archived[k])
            and current[k]["node"].get("default-x")==archived[k]["node"].get("default-x")):
            notes[k[:2]][current[k]["node"]]=(r,current[k])
    observations=[]
    with ZipFile(book) as verified, ZipFile(raw) as original:
        for part in cloned.findall("part"):
            pid=part.get("id")
            if pid not in piano:
                continue
            divisions=0
            for m,(bar,(_,meter)) in zip(part.findall("measure"),inherited_context(part),strict=True):
                divisions=int(m.findtext("attributes/divisions",str(divisions)))
                forward=m.findall("forward")
                backs=m.findall("backup")
                if meter!=("4","4") or divisions<=0 or divisions%2 or len(forward)!=1 or len(backs)!=2:
                    continue
                gap=forward[0]
                guards=list(m)
                index=guards.index(gap)
                if gap.findtext("staff")!="2" or guards[index-1] is not backs[1] or backs[0].findtext("duration")!=str(4*divisions):
                    continue
                head=guards[guards.index(backs[0])+1:index-1]
                trail=guards[index+1:]
                if not head or not trail:
                    continue
                if any(n.tag!="note" or n.findtext("staff")!="2" or n.find("pitch") is None or
                       n.find("grace") is not None or n.find("tie") is not None or n.find("notations/tied") is not None
                       for n in head+trail):
                    continue
                a,b=head[0].findtext("voice"),trail[0].findtext("voice")
                if a==b or b!=gap.findtext("voice") or any(n.findtext("voice")!=a for n in head) or any(n.findtext("voice")!=b for n in trail):
                    continue
                if any(n.find("chord") is None for n in head[1:]) or head[0].find("chord") is not None or trail[0].find("chord") is not None:
                    continue
                keys=notes[pid,bar]
                if any(x not in keys for x in head+trail):
                    continue
                left,left_event=keys[head[0]]
                right,right_event=keys[trail[0]]
                start=left_event["duration"]
                end=right_event["start"]
                if (left_event["start"]!=0 or start*divisions!=int(backs[1].findtext("duration"))
                        or end*divisions!=int(gap.findtext("duration")) or start>=end):
                    continue
                if max(keys[n][1]["start"]+keys[n][1]["duration"] for n in trail)!=4:
                    continue
                unit,sys=left["unit"],left["system"]
                if any((keys[n][0]["unit"],keys[n][0]["system"],keys[n][0]["staff"])!=(unit,sys,left["staff"]) for n in head+trail):
                    continue
                raw_graph=graph(original,unit,sys)
                cooked=graph(verified,unit,sys)
                if raw_graph is None or cooked is None:
                    continue
                raw_nodes,retained=map(with_bounds,(raw_graph,cooked))
                if not all(native_anchor(r,g) for n in head+trail for r,_ in [keys[n]] for g in (raw_nodes,retained)):
                    continue
                staff=left["staff"]
                raw_part=next((x for x in raw_graph.findall("part") if x.find(f"staff[@id='{staff}']") is not None),None)
                if raw_part is None:
                    continue
                group=[x for x in raw_part.findall("measure") if left["chord"] in x.findtext("head-chords","").split()
                       and right["chord"] in x.findtext("head-chords","").split()]
                if len(group)!=1:
                    continue
                target=sorted((n for n in raw_rests(raw_graph,group[0],raw_nodes,staff)
                               if int(left["x"])<coordinate(n)[0]<int(right["x"])),key=lambda n:coordinate(n)[0])
                if not target or len(target)>4:
                    continue
                at=start
                plan=[]
                upper=[(int(r["x"]),v["start"]) for n,(r,v) in keys.items()
                       if n.findtext("staff")=="1" and n.find("chord") is None and r["unit"]==unit and r["system"]==sys
                       and native_anchor(r,retained)]
                for n in target:
                    x=coordinate(n)[0]
                    if [onset for xx,onset in upper if abs(xx-x)<=3]!=[at]:
                        break
                    kind,length=RESTS[n.get("shape")]
                    plan.append((n,kind,length,at))
                    at+=length
                if len(plan)!=len(target) or at!=end:
                    continue
                img=job/f"unit-{int(unit):03d}.png"
                if sha(img)!=left["source_sha256"]:
                    continue
                with Image.open(img) as origin:
                    pixels=np.asarray(origin.convert("L"))
                attached={r.get("target") for r in cooked.findall("sig/relations/*") if r.find("containment") is not None}
                shapes=[]
                for node,kind,length,at in plan:
                    peers=[n for id_,n in retained.items() if id_ in attached and n.get("shape")==node.get("shape")
                           and n.get("staff")==staff and n.get("id") not in {r.get("id") for r in target}]
                    if (hit:=evidence(pixels,node,peers)) is None:
                        break
                    shapes.append(dict(kind=kind,at=str(at),duration=str(length),**hit))
                if len(shapes)!=len(plan):
                    continue
                insert=guards.index(backs[1])
                m.remove(backs[1]);m.remove(gap)
                for i,(_,kind,length,at) in enumerate(plan):
                    n=ET.Element("note")
                    ET.SubElement(n,"rest")
                    ET.SubElement(n,"duration").text=str(int(length*divisions))
                    ET.SubElement(n,"voice").text=a
                    ET.SubElement(n,"type").text=kind
                    ET.SubElement(n,"staff").text="2"
                    m.insert(insert+i,n)
                for n in trail:n.find("voice").text=a
                observations.append(dict(part=pid,measure=bar,staff=staff,source_sha256=left["source_sha256"],rests=shapes))
    return (cloned,dict(state="restored",changes=observations)) if observations else (root,dict(state="not_applicable",changes=[]))
