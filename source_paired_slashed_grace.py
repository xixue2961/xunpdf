"""Recover repeated same-pitch slashed grace notes from the original raster."""
from collections import defaultdict
import csv
import json
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_piano_control import read_control
from source_vocal_split_clock import native_anchor


def grace_ink(image, row, space):
    x,y=(int(row[k]) for k in ("x","y"))
    def scale(n):
        return round(n*space/18.4)
    x1,x2=x-scale(27),x+scale(13)
    y1,y2=y-scale(31),y+scale(18)
    if not 0<=x1<x2<=image.shape[1] or not 0<=y1<y2<=image.shape[0]:
        return None
    crop=Image.fromarray(image[y1:y2,x1:x2]).resize((40,49),Image.Resampling.BILINEAR)
    patch=np.asarray(crop)
    votes=[]
    for cutoff in (100,125,155):
        raw=patch<cutoff
        ink=raw.copy()
        ink[ink.mean(axis=1)>.86]=False
        core=float(ink[34:47,4:22].mean())
        stem=raw[6:33,19:26].mean(axis=0)
        location=int(stem.argmax())+19
        continuity=float(stem.max())
        flag=int(ink[6:23,location+3:location+14].sum())
        # The flag alone is insufficient. A short diagonal reaches left of
        # the upright stem, which rules out ordinary stem/flag neighbors.
        slash=int(ink[11:27,location-5:location-1].sum())
        if core<.30 or continuity<(.87 if cutoff==100 else .94) or flag<24 or slash<2 or location not in (20,21,22,23,24):
            return None
        votes.append(dict(cutoff=cutoff,head_ink=core,stem=continuity,flag=flag,slash=slash))
    return dict(bounds=[x1,y1,x2,y2],votes=votes)


def small_note(principal):
    n=ET.Element("note")
    ET.SubElement(n,"grace",slash="yes")
    n.append(deepcopy(principal.find("pitch")))
    ET.SubElement(n,"voice").text=principal.findtext("voice","1")
    ET.SubElement(n,"type").text="eighth"
    ET.SubElement(n,"stem").text="up"
    ET.SubElement(n,"staff").text=principal.findtext("staff","1")
    return n


def fallback_control(job):
    report=job/"source-native-notation.json"
    folder=job/"source-long-tuplets-ledger"
    xml=folder/"result.mxl"
    head=folder/"head-ledger.tsv"
    if not all(p.is_file() for p in (report,xml,head)):
        return None
    data=json.loads(report.read_text(encoding="utf8"))
    if not all(data.get(k) for k in ("accepted_native_book","accepted_native_sha256")):
        return None
    archive=Path(data["accepted_native_book"])
    if sha(archive)!=data["accepted_native_sha256"]:
        return None
    mapped=defaultdict(list)
    with head.open(encoding="utf8",newline="") as f:
        for row in csv.DictReader(f,delimiter="\t"):
            if row["repeat_copy"]=="false" and row["head"]!="0":
                mapped[row["part"],row["measure"],int(row["note_index"])].append(row)
    return archive,score(xml),{k:v[0] for k,v in mapped.items() if len(v)==1}


def recover(root,job):
    job=Path(job)
    base=read_control(job) or fallback_control(job)
    if base is None or not (job/"raw/input.omr").is_file():
        return root,dict(state="not_assessed",changes=[])
    book,control,ledger=base
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    ref=_score_events(control)[0]
    fixed=deepcopy(root)
    live=_score_events(fixed)[0]
    scopes=defaultdict(list)
    for key,row in ledger.items():
        if key[0] not in piano or row["shape"]!="NOTEHEAD_BLACK" or key not in live or key not in ref:
            continue
        a,b=live[key],ref[key]
        n=a["node"]
        if (_music_signature(a)!=_music_signature(b) or b["node"].get("default-x")!=n.get("default-x")
                or n.find("pitch") is None or n.find("grace") is not None
                or n.findtext("type")!="quarter" or n.find("lyric") is not None
                or n.find("cue") is not None or n.find("notations/ornaments") is not None):
            continue
        scopes[row["unit"],row["system"]].append((key,row))
    changes=[]
    with ZipFile(book) as accepted, ZipFile(job/"raw/input.omr") as original:
        for (unit,system),entries in sorted(scopes.items()):
            a,b=graph(accepted,unit,system),graph(original,unit,system)
            if a is None or b is None:
                continue
            na,nb=with_bounds(a),with_bounds(b)
            raster=job/f"unit-{int(unit):03}.png"
            digest=sha(raster)
            if digest not in {r["source_sha256"] for _,r in entries}:
                continue
            with Image.open(raster) as png:
                image=np.asarray(png.convert("L"))
            spaces={}
            for staff in a.findall("part/staff"):
                ys=[float(v.find("point").get("y")) for v in staff.findall("lines/line")]
                if len(ys)==5:
                    spaces[staff.get("id")]=(ys[-1]-ys[0])/4
            attacks=defaultdict(list)
            for key,row in entries:
                value=live[key]
                note=value["node"]
                if value["measure"].find("note/grace") is not None:
                    continue
                if not all(native_anchor(row,owner) for owner in (na,nb)) or row["source_sha256"]!=digest:
                    continue
                space=spaces.get(row["staff"],0)
                if not 11<=space<=27:
                    continue
                ink=grace_ink(image,row,space)
                if ink is not None:
                    attacks[key[:2],value["start"]].append((key,row,note,ink))
            by_bar=defaultdict(list)
            for (bar,at),pair in attacks.items():
                if len(pair)!=2 or {r["staff_number"] for _,r,_,_ in pair}!={"1","2"}:
                    continue
                if abs(int(pair[0][1]["x"])-int(pair[1][1]["x"]))>4:
                    continue
                by_bar[bar].append((at,pair))
            for bar,groups in by_bar.items():
                if len(groups)<3:
                    continue
                groups.sort(key=lambda g:g[0])
                when=[t for t,_ in groups]
                if len(set(b-a for a,b in zip(when,when[1:])))!=1:
                    continue
                for at,pair in groups:
                    for key,row,principal,ink in pair:
                        measure=live[key]["measure"]
                        idx=list(measure).index(principal)
                        while idx>0 and measure[idx].find("chord") is not None and measure[idx-1].tag=="note":
                            idx-=1
                        measure.insert(idx,small_note(principal))
                        changes.append(dict(part=key[0],measure=key[1],note_index=key[2], onset=str(at),staff=row["staff_number"],
                                            source_sha256=digest,principal_head=row["head"],**ink))
    return (fixed,dict(state="restored",changes=changes)) if changes else (root,dict(state="unchanged",changes=[]))
