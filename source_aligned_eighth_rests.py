"""Reinstate an omitted eighth rest verified against another staff in the same beat column."""
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
from source_vocal_split_clock import native_anchor


def middle_line(staff,x):
    line=staff.findall("lines/line")
    if len(line)!=5:
        return None
    points=[(float(p.get("x")),float(p.get("y"))) for p in line[2].findall("point")]
    return float(np.interp(x,*zip(*points))) if len(points)>1 else None


def match_rest(pixels,rest,shift):
    x,y,w,h=[int(rest[k]) for k in ("x","y","w","h")]
    if not (12<=w<=22 and 24<=h<=39):
        return None
    base=pixels[y:y+h,x:x+w]
    if base.shape!=(h,w):
        return None
    matches=[]
    for threshold,minimum in ((110,.73),(155,.80),(185,.86)):
        anchor=base<threshold
        best=[]
        for dx in (-2,-1,0,1,2):
            for delta in range(-4,5):
                xx,yy=x+dx,y+shift+delta
                if xx<0 or yy<0:
                    continue
                crop=pixels[yy:yy+h,xx:xx+w]
                if crop.shape!=base.shape:
                    continue
                probe=crop<threshold
                keep=(anchor.sum(1)<.88*w)&(probe.sum(1)<.88*w)
                union=(anchor[keep]|probe[keep]).sum()
                if keep.sum()<.55*h or union<.09*w*h:
                    continue
                quality=float((anchor[keep]&probe[keep]).sum())/union
                best.append((float(quality),xx,yy))
        best.sort(reverse=True)
        if not best or best[0][0]<minimum or len(best)>1 and best[0][0]-best[1][0]<.08:
            return None
        matches.append(best[0])
    if max(x[1] for x in matches)-min(x[1] for x in matches)>1 or max(x[2] for x in matches)-min(x[2] for x in matches)>1:
        return None
    return dict(box=[matches[1][1],matches[1][2],w,h],similarity=[round(x[0],4) for x in matches])


def recover(root,job):
    job=Path(job)
    report=job/"source-native-notation.json"
    if not report.is_file():
        return root,dict(state="not_applicable",changes=[])
    cfg=json.loads(report.read_text("utf8"))
    book,xml=(Path(cfg[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if not cfg.get("accepted_native_sha256") or sha(book)!=cfg["accepted_native_sha256"]:
        return root,dict(state="not_applicable",changes=[])
    body=deepcopy(root)
    live,dirs=_score_events(body)
    orig=_score_events(score(xml))[0]
    collected=defaultdict(list)
    for n in rows(xml.with_name("note-ledger.tsv")):
        key=n["part"],n["measure"],int(n["note_index"])
        if (n["shape"] not in ("NOTEHEAD_BLACK","NOTEHEAD_VOID","EIGHTH_REST") or key not in live or key not in orig
                or signature(live[key])!=signature(orig[key])
                or live[key]["node"].get("default-x")!=orig[key]["node"].get("default-x")):
            continue
        child=live[key]["node"]
        if child.find("chord") is None and child.find("grace") is None and child.find("cue") is None:
            collected[key[:2]+(n["staff_number"],)].append((n,live[key]))
    parts={p.get("id"):p.findtext("part-name","").casefold() for p in body.findall("part-list/score-part")}
    seen=defaultdict(list)
    for p in body.findall("part"):
        pid=p.get("id")
        pname=parts.get(pid)
        if pname not in ("soprano","alto","tenor","bass","piano"):
            continue
        div=0
        for m,(bar,(_,meter)) in zip(p.findall("measure"),inherited_context(p),strict=True):
            div=int(m.findtext("attributes/divisions",str(div)))
            if meter!=("4","4") or div<=0 or div%2 or m.findall("forward"):
                continue
            lane=sorted(collected[pid,bar,"1"],key=lambda v:int(v[0]["note_index"]))
            staff=[n for n in m.findall("note") if n.findtext("staff","1")=="1"]
            lead=[n for n in staff if n.find("chord") is None]
            if [v["node"] for _,v in lane]!=lead or len({n.findtext("voice","1") for n in staff})!=1:
                continue
            types=[r["shape"] for r,_ in lane]
            durations=[v["duration"] for _,v in lane]
            relative=[v["start"] for _,v in lane]
            donor=(types==["NOTEHEAD_VOID","EIGHTH_REST",*(["NOTEHEAD_BLACK"]*3)]
                   and durations==[2,*([F(1,2)]*4)] and relative==[0,2,F(5,2),3,F(7,2)])
            receiver=(types==["NOTEHEAD_VOID",*(["NOTEHEAD_BLACK"]*3)]
                   and durations==[2,*([F(1,2)]*3)] and relative==[0,2,F(5,2),3])
            if not donor and not receiver:
                continue
            backups=m.findall("backup")
            if receiver and (any(d["start"]>=2 and d["part"]==pid and d["measure"]==bar for d in dirs)
                or len(backups)!=(1 if pname=="piano" else 0)
                or backups and (backups[0].findtext("duration")!=str(7*div//2)
                    or list(m).index(backups[0])<=list(m).index(lead[-1]))):
                continue
            head=lane[0][0]
            seen[head["unit"],head["system"],bar].append((pname,lane,donor,m,div,backups))
    changes=[]
    with ZipFile(book) as archive:
        for (unit,system,bar),lanes in seen.items():
            donors=[x for x in lanes if x[2] and x[0]!="piano"]
            if len(donors)!=1 or not any(not x[2] for x in lanes):
                continue
            _,source,_,_,_,_=donors[0]
            rest=source[1][0]
            g=graph(archive,unit,system)
            if g is None:
                continue
            nodes=with_bounds(g)
            if not all(native_anchor(r,nodes) for r,v in source):
                continue
            staff=g.find(f"part/staff[@id='{rest['staff']}']")
            x=int(rest["x"])
            y=middle_line(staff,x) if staff is not None else None
            path=job/f"unit-{int(unit):03d}.png"
            if y is None or sha(path)!=rest["source_sha256"]:
                continue
            with Image.open(path) as original:
                pixels=np.asarray(original.convert("L"))
            for part,lane,donor,m,div,backups in lanes:
                if donor or any(r["source_sha256"]!=rest["source_sha256"] or not native_anchor(r,nodes) for r,_ in lane):
                    continue
                pitch_rows=[source[0],*source[2:]]
                if any(abs(int(a["x"])-int(b["x"]))>3 for (a,_),(b,_) in zip(pitch_rows,lane,strict=True)):
                    continue
                target=lane[0][0]
                if target["staff"]==rest["staff"] or not int(target["x"]) < x < int(lane[1][0]["x"]):
                    continue
                other=g.find(f"part/staff[@id='{target['staff']}']")
                dest=middle_line(other,x) if other is not None else None
                if dest is None or (match:=match_rest(pixels,rest,round(dest-y))) is None:
                    continue
                node=lane[1][1]["node"]
                created=ET.Element("note")
                ET.SubElement(created,"rest")
                ET.SubElement(created,"duration").text=str(div//2)
                ET.SubElement(created,"voice").text=node.findtext("voice","1")
                ET.SubElement(created,"type").text="eighth"
                if node.find("staff") is not None:
                    ET.SubElement(created,"staff").text=node.findtext("staff")
                m.insert(list(m).index(node),created)
                if backups:
                    backups[0].find("duration").text=str(4*div)
                changes.append(dict(part=target["part"],measure=bar,source_part=rest["part"],source_rest=rest["head"],
                                    onset="2",duration="1/2",image_sha256=rest["source_sha256"],**match))
    return (body,dict(state="restored",changes=changes)) if changes else (root,dict(state="not_applicable",changes=[]))
