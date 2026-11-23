from collections import Counter
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import csv
import json
import sys

import numpy as np
from PIL import Image

from source_system_continuation_ink import continuation


def letter_pitch(note):
    return tuple(note.findtext("pitch/"+f,"0") for f in ("step","alter","octave"))


def staff(note):
    return note.findtext("staff","1")


def key(note):
    return staff(note),letter_pitch(note)


def core_density(image, box):
    x,y,w,h=(int(box[k]) for k in "xywh")
    central=image[y+int(.13*h):y+int(.82*h),x+int(.16*w):x+int(.82*w)]
    return [float(np.mean(central<cut)) for cut in (160,208)]


def erase_arc(image,witness):
    clone=image.copy()
    for x,y in zip(witness["xs"],witness["ys"]):
        yi=round(y)
        clone[yi-4:yi+5,x]=255
    return clone


def lookup(measure, archive, rows):
    units={r["unit"] for r in rows}
    systems={r["system"] for r in rows}
    if len(units)!=1 or len(systems)!=1:
        raise ValueError("source event is not on one system")
    unit=units.pop()
    root=ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml"))
    physical=root.findall("page/system")
    system=[s for s in physical if s.get("id") in systems]
    if len(system)!=1:
        raise ValueError("source system ambiguous")
    return system[0],physical.index(system[0])


def source_pairs(archive, source, entries, adjacent):
    rows,other=entries
    system,ix=lookup("",archive,list(rows.values()))
    following,jx=lookup("",archive,list(other.values()))
    if jx!=ix+1 or not adjacent:
        raise ValueError("physical systems are not consecutive")
    unit=next(iter(rows.values()))["unit"]
    page=source/f"unit-{int(unit):03d}.png"
    source_hash=sha256(page.read_bytes()).hexdigest()
    if any(r["unit"]!=unit or r["source_sha256"]!=source_hash for r in list(rows.values())+list(other.values())):
        raise ValueError("source raster does not match both systems")
    with Image.open(page) as raster:
        image=np.asarray(raster.convert("L"))
    details=[]
    for addr,before in rows.items():
        after=other[addr]
        st0=system.find(f"part/staff[@id='{before['staff']}']")
        st1=following.find(f"part/staff[@id='{after['staff']}']")
        edge=max(float(n.get("x")) for n in st0.findall("lines/line/point"))
        out=continuation(image,st0,before,"out",edge)
        if out is None:
            raise ValueError(f"no unique outgoing continuation for {addr}")
        inc=continuation(image,st1,after,"in",requested_sign=out["sign"])
        if inc is None:
            raise ValueError(f"no matching incoming continuation for {addr}")
        if continuation(erase_arc(image,out),st0,before,"out",edge,out["sign"]) is not None:
            raise ValueError(f"outgoing arc persists after removal for {addr}")
        if continuation(erase_arc(image,inc),st1,after,"in",requested_sign=out["sign"]) is not None:
            raise ValueError(f"incoming arc persists after removal for {addr}")
        details.append(dict(staff=addr[0],pitch=addr[1],outgoing=out,incoming=inc,
                            source_heads=[before["head"],after["head"]]))
    return details,image,following


def suspicious_head(row, image, system, confirmed):
    native=system.find(f"sig/inters/head[@id='{row['head']}']")
    if native is None or native.get("shape")!="NOTEHEAD_BLACK" or float(native.get("grade","1"))>.4:
        return None
    density=core_density(image,row)
    if max(density)>.73:
        return None
    x,y,w,h=(int(row[k]) for k in "xywh")
    if not confirmed or min(core_density(image,confirmed))<.86:
        return None
    cx=int(confirmed["x"])
    if not .72*w <= cx-x <= 1.23*w:
        return None
    return dict(head=row["head"],luminance_density=density,physical_head_density=core_density(image,confirmed))


def read_ledgers(folder):
    with (folder/"head-ledger.tsv").open(encoding="utf8",newline="") as f:
        return {(r["part"],r["measure"],int(r["note_index"])):r for r in csv.DictReader(f,delimiter="\t")
                if r["repeat_copy"]=="false"}


def find_bar(root,part,bar):
    return root.find(f"part[@id='{part}']/measure[@number='{bar}']")


def by_scope(events,scope):
    return {k:e for k,e in events.items() if k[:2]==scope}


def count_music(rows):
    return Counter((key(e["node"]),e["start"],e["duration"]) for e in rows.values() if e["node"].find("pitch") is not None)


def restore_tremolo_span(caller,native_dir,control_dir,source):
    from source_native_notation import score
    from source_lyric_roles import _score_events
    from source_boundary_piano_projection import add_tie

    new_native=score(native_dir/"result.mxl")
    old_native=score(control_dir/"result.mxl")
    native,new_dir=_score_events(new_native)
    control,_=_score_events(old_native)
    current,current_dir=_score_events(caller)
    ledger=read_ledgers(native_dir)
    output=deepcopy(caller)
    pairs=[]
    for part in new_native.findall("part"):
        pid=part.get("id")
        measures=part.findall("measure")
        for i,m in enumerate(measures[:-1]):
            number=m.get("number")
            scope=pid,number
            marks=m.findall("note/notations/ornaments/tremolo")
            if len(marks)!=4 or any(t.text!="2" for t in marks):
                continue
            if find_bar(old_native,*scope).find("note/notations/ornaments/tremolo") is not None:
                continue
            candidate=by_scope(native,scope)
            control_sig=count_music(by_scope(control,scope))
            if count_music(by_scope(current,scope))!=control_sig:
                raise ValueError("cooked tremolo differs from native control")
            next_scope=pid,measures[i+1].get("number")
            if count_music(by_scope(current,next_scope))!=count_music(by_scope(control,next_scope)):
                raise ValueError("cooked opening differs from native control")
            target=find_bar(output,*scope)
            incoming=find_bar(output,*next_scope)
            if incoming.find("print[@new-system='yes']") is None or candidate=={}:
                raise ValueError("matching bar is not a system opening")
            end={key(e["node"]):k for k,e in candidate.items() if e["start"]==2 and e["duration"]==2}
            opening={key(e["node"]):k for k,e in by_scope(current,next_scope).items()
                     if e["start"]==0 and e["node"].find("pitch") is not None}
            if len(end)!=4 or not end.keys()<=opening.keys() or len(opening.keys()-end.keys())!=1:
                raise ValueError("source boundary pitch relationship is ambiguous")
            endpoints={a:ledger[k] for a,k in end.items()}
            starts={a:ledger[opening[a]] for a in end}
            with ZipFile(native_dir/"refined.omr") as archive:
                proof,im,s2=source_pairs(archive,source, (endpoints,starts),True)
            false=(opening.keys()-end.keys()).pop()
            extra_key=opening[false]
            fake=ledger[extra_key]
            same_staff=[r for a,r in starts.items() if a[0]==false[0]]
            if not same_staff:
                raise ValueError("false note not on a matched staff")
            near=min(same_staff,key=lambda r:abs(int(r["y"])-int(fake["y"])))
            denial=suspicious_head(fake,im,s2,near)
            if denial is None:
                raise ValueError("source did not disprove the unmatched note")
            x,y,w,h=(int(fake[k]) for k in "xywh")
            matching_curve=next((v["incoming"] for v in proof if v["source_heads"][1]==near["head"]),None)
            if matching_curve is None:
                raise ValueError("extra pitch is not next to a known continuation")
            support=[x<=xx<x+w and y<=yy<y+h for xx,yy in zip(matching_curve["xs"],matching_curve["ys"])]
            if sum(support)<.55*len(support):
                raise ValueError("extra pitch does not occupy the continuation ink")
            denial["continuation_overlap"]=sum(support)/len(support)
            before_dirs=[(d["start"],d["staff"],ET.tostring(d["node"])) for d in current_dir if (d["part"],d["measure"])==scope]
            after_dirs=[(d["start"],d["staff"],ET.tostring(d["node"])) for d in new_dir if (d["part"],d["measure"])==scope]
            if before_dirs!=after_dirs:
                raise ValueError("recovered chords change original direction placement")
            if any(c.tag not in {"note","backup","direction"} for c in target) or any(c.tag not in {"note","backup","direction"} for c in m):
                raise ValueError("unexpected context in tremolo bar")
            old_backup=sum(int(x.findtext("duration")) for x in target.findall("backup"))
            new_backup=sum(int(x.findtext("duration")) for x in m.findall("backup"))
            if old_backup!=new_backup:
                raise ValueError("native and cooked tremolo ticks differ")
            for c in list(target):
                target.remove(c)
            target.extend(deepcopy(m[:]))
            current_notes=incoming.findall("note")
            fake_note=current_notes[extra_key[2]]
            promoted=current_notes[extra_key[2]+1]
            if fake_note.find("chord") is not None or promoted.find("chord") is None or key(promoted) not in end:
                raise ValueError("extra note is not the first chord head")
            promoted.remove(promoted.find("chord"))
            incoming.remove(fake_note)
            obsolete_out=[]
            obsolete_in=[]
            for witness in proof:
                addr=(witness["staff"],tuple(witness["pitch"]))
                left=target.findall("note")[end[addr][2]]
                right=current_notes[opening[addr][2]]
                old_l=left.findall("notations/slur")
                old_r=right.findall("notations/slur")
                obsolete_out.extend((left,t) for t in old_l)
                obsolete_in.extend((right,t) for t in old_r)
                side="over" if witness["outgoing"]["sign"]<0 else "under"
                for n,mode in ((left,"start"),(right,"stop")):
                    if n.find("tie") is not None or n.find("notations/tied") is not None:
                        raise ValueError("existing ties at source must be preserved")
                    add_tie(n,mode)
                    n.find("notations/tied").set("orientation",side)
            types=lambda links:Counter(t.get("type") for _,t in links)
            if types(obsolete_out)!=Counter(("start","continue")) or types(obsolete_in)!=Counter(("continue","stop")):
                raise ValueError("mislabelled curve sequence differs")
            if len({t.get("number") for _,t in obsolete_out+obsolete_in})!=1:
                raise ValueError("unrelated slur overlaps boundary")
            for n,t in obsolete_out+obsolete_in:
                n.find("notations").remove(t)
            pairs.append(dict(part=pid,bar=number,next_bar=next_scope[1],tied=proof,phantom=denial))
    return output,pairs


