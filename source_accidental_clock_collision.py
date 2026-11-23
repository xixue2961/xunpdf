"""Rebuild a two-staff clock when accidentals masquerade as a rest and notes."""
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_cluster_accidental_ink import samples, signs_at
from source_dotted_chord_pair_arcs import paired_arcs
from source_native_notation import sha
from source_parallel_rest_gap import graph, with_bounds
from source_parallel_tail_ties import marks, set_mark
from source_piano_control import read_control
from source_repeated_piano_rest_lanes import contact
from source_vocal_split_clock import coordinate, native_anchor, pitch

ALTER = {"NATURAL": "0", "SHARP": "1", "FLAT": "-1"}


def layout(measure):
    if measure is None:
        return None
    nodes = [n for n in measure if n.tag in ("note", "backup", "forward")]
    if len(nodes) != 18 or [i for i,n in enumerate(nodes) if n.tag != "note"] != [11,15,16]:
        return None
    if [nodes[i].tag for i in (11,15,16)] != ["backup","backup","forward"]:
        return None
    notes = [n for n in nodes if n.tag == "note"]
    if len(measure.findall("note")) != len(notes):
        return None
    rhythm = ["quarter","quarter",*["eighth"]*4,*["half"]*5,"quarter","half","quarter","quarter"]
    if [n.findtext("type") for n in notes] != rhythm:
        return None
    if [n.findtext("staff") for n in notes] != ["1"]*11+["2","1","2","2"]:
        return None
    if [i for i,n in enumerate(notes) if n.find("dot") is not None] != [0,11,14]:
        return None
    if [i for i,n in enumerate(notes) if n.find("rest") is not None] != [1]:
        return None
    if [i for i,n in enumerate(notes) if n.find("chord") is not None] != [3,4,5,7,8,9,10]:
        return None
    if any(len(n.findall("dot")) > 1 or n.find("time-modification") is not None for n in notes):
        return None
    step=int(notes[2].findtext("duration"))
    if step <= 0 or [int(n.findtext("duration")) for n in notes] != [
            step*v for v in [3,2,1,1,1,1,4,4,4,4,4,3,4,2,3]]:
        return None
    if [int(nodes[i].findtext("duration")) for i in (11,15,16)] != [10*step,9*step,5*step]:
        return None
    voices = [n.findtext("voice") for n in notes]
    if len(set(voices))!=3 or any(voice is None for voice in voices) or len(set(voices[:11]))!=1:
        return None
    if len(set(voices[11:14]))!=1 or voices[14] != nodes[16].findtext("voice"):
        return None
    if any(n.find("accidental") is not None for n in notes[2:11]) or pitch(notes[11]) != pitch(notes[14]):
        return None
    if [pitch(n) for n in notes[2:6]] != [pitch(n) for n in notes[7:11]]:
        return None
    if len({pitch(n) for n in notes[2:6]}) != 4:
        return None
    ties = [(marks(a), marks(b)) for a,b in zip(notes[2:6],notes[7:11],strict=True)]
    if ties.count((["start"],["stop"]))!=1 or ties.count(([],[]))!=3:
        return None
    for n in (notes[1],notes[6],notes[12]):
        if not {q.tag for q in n}<={"rest","pitch","duration","voice","type","stem","staff"}:
            return None
    if notes[14].find("notations") is not None or notes[14].find("lyric") is not None:
        return None
    return notes,nodes,step


def attached(graph, source=None, target=None, kind="containment"):
    return [rel.get("target") if source is not None else rel.get("source")
            for rel in graph.findall("sig/relations/relation")
            if (source is None or rel.get("source")==source)
            and (target is None or rel.get("target")==target)
            and rel.find(kind) is not None]


def shape_proof(image, accepted, raw, rows, space):
    by_id = with_bounds(accepted)
    original = with_bounds(raw)
    if not all(native_anchor(r, by_id) and native_anchor(r,original) for r in rows.values()):
        return None
    early, late = [rows[i] for i in range(2,6)], [rows[i] for i in range(7,11)]
    if any(r["shape"]!="NOTEHEAD_BLACK" for r in early) or any(r["shape"]!="NOTEHEAD_VOID" for r in late):
        return None
    if len({r["staff"] for r in early+late+[rows[0],rows[6],rows[12]]})!=1:
        return None
    for group in (early,late):
        if len({r["chord"] for r in group})!=1 or max(int(r["x"]) for r in group)-min(int(r["x"]) for r in group)>.13*space:
            return None
    if any(abs(int(a["y"])-int(b["y"]))>.13*space for a,b in zip(early,late,strict=True)):
        return None
    part=next((p for p in accepted.findall("part") if len(p.findall("staff"))==2
               and p.findall("staff")[0].get("id")==early[0]["staff"]
               and p.findall("staff")[1].get("id")==rows[11]["staff"]),None)
    if part is None or any(rows[i]["staff"]!=rows[11]["staff"] for i in (13,14)):
        return None
    measure=next((m for m in part.findall("measure") if early[0]["chord"] in m.findtext("head-chords","").split()),None)
    if measure is None or set(measure.findtext("head-chords","").split())!={r["chord"] for r in rows.values()}:
        return None
    if abs(int(rows[0]["x"])-int(rows[11]["x"]))>.18*space or abs(int(early[0]["x"])-int(rows[14]["x"]))>.18*space:
        return None
    for i in (0,11,14):
        dots=attached(accepted,target=rows[i]["head"],kind="augmentation")
        if len(dots)!=1 or dots[0] not in original or coordinate(by_id[dots[0]])!=coordinate(original[dots[0]]):
            return None
        x,y,w,h=coordinate(by_id[dots[0]])
        if (image[y:y+h,x:x+w]<130).mean()<.45:
            return None
    stems=attached(accepted,source=early[0]["chord"],kind="chord-stem")
    if len(stems)!=1:
        return None
    flags=attached(accepted,target=stems[0],kind="flag-stem")
    if len(flags)!=1 or by_id[flags[0]].get("shape")!="FLAG_1_DOWN":
        return None
    if attached(accepted,target=stems[0],kind="beam-stem"):
        return None
    rest_nodes=[by_id[item] for chord in measure.findtext("rest-chords","").split()
                for item in attached(accepted,source=chord) if item in by_id]
    if len(rest_nodes)!=1 or rest_nodes[0].get("shape")!="QUARTER_REST" or rest_nodes[0].get("staff")!=early[0]["staff"]:
        return None
    return rest_nodes[0],flags[0]


def overlap_design(box, incr):
    x,y,w,h=box
    a,b,c,d=incr
    return max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))/(w*h)


def bad_head(image, row, donors, feature_boxes, threshold):
    x,y,w,h=(int(row[t]) for t in ("x","y","w","h"))
    box=(x,y,w,h)
    if max((overlap_design(box,f) for f in feature_boxes),default=0)<.24:
        return False
    for donor in donors:
        candidate=tuple(int(donor[t]) for t in ("x","y","w","h"))
        if contact(image,candidate,(x,y),threshold,(2,2)) is not None:
            return False
    return True


def rebuilt(selection, signs, rows):
    notes,nodes,step=selection
    for a,b,i in zip(notes[2:6],notes[7:11],range(2,6),strict=True):
        glyph=signs.get(rows[i]["head"])
        if glyph is not None:
            for note in (a,b):
                p=note.find("pitch")
                alter=p.find("alter")
                if alter is None:
                    alter=ET.Element("alter")
                    p.insert(list(p).index(p.find("octave")),alter)
                alter.text=ALTER[glyph["shape"]]
            future={"time-modification","stem","notehead","notehead-text","staff","beam","notations","lyric","play","listen"}
            e=ET.Element("accidental")
            e.text=glyph["shape"].lower()
            a.insert(next((n for n,c in enumerate(a) if c.tag in future),len(a)),e)
        if not marks(a):
            set_mark(a,"start")
            set_mark(b,"stop")
    notes[7].remove(notes[7].find("chord"))
    nodes[11].find("duration").text=str(8*step)
    notes[14].find("voice").text=notes[11].findtext("voice")


def recover(root,job):
    job=Path(job)
    base=read_control(job)
    original=job/"raw/input.omr"
    if base is None or not original.is_file():
        return root,dict(state="not_assessed",changes=[])
    book,control,ledger=base
    ref={p.get("id"):p for p in control.findall("part")}
    signed_pages = defaultdict(set)
    for row in ledger.values():
        signed_pages[int(row["unit"])].add(row["source_sha256"])
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    result=deepcopy(root)
    changes=[]
    @lru_cache(maxsize=None)
    def source_signed(unit):
        return signed_pages[unit] == {sha(job / f"unit-{unit:03d}.png")}

    @lru_cache(maxsize=3)
    def raster(unit):
        with Image.open(job/f"unit-{unit:03d}.png") as src:
            return np.asarray(src.convert("L"))
    with ZipFile(book) as archive,ZipFile(original) as original_archive:
        for part in result.findall("part"):
            pid=part.get("id")
            if pid not in piano or pid not in ref:
                continue
            for measure in part.findall("measure"):
                sel=layout(measure)
                if sel is None:
                    continue
                number=measure.get("number")
                source=layout(ref[pid].find(f"measure[@number='{number}']"))
                if source is None or any(ET.tostring(a)!=ET.tostring(b) for a,b in zip(source[1],sel[1],strict=True)):
                    continue
                rows={i:ledger.get((pid,number,i)) for i in range(15) if i!=1}
                if not all(rows.values()) or len({(r["unit"],r["system"],r["source_sha256"]) for r in rows.values()})!=1:
                    continue
                unit,system,signed=next(iter((r["unit"],r["system"],r["source_sha256"]) for r in rows.values()))
                unit=int(unit)
                if not source_signed(unit):
                    continue
                accepted,raw=graph(archive,unit,system),graph(original_archive,unit,system)
                staff=accepted.find(f".//staff[@id='{rows[2]['staff']}']")
                lines=[float(line.find("point").get("y")) for line in staff.findall("lines/line")]
                if len(lines)!=5 or not 12<=(space:=(lines[-1]-lines[0])/4)<=25:
                    continue
                image=raster(unit)
                shape=shape_proof(image,accepted,raw,rows,space)
                if shape is None:
                    continue
                rest,flag=shape
                before,after=[rows[i] for i in range(2,6)],[rows[i] for i in range(7,11)]
                arcs=paired_arcs(image,before,after,space)
                if arcs is None:
                    continue
                nearby=[u for u in (unit-1,unit,unit+1) if f"sheet#{u}/sheet#{u}.xml" in archive.namelist() and source_signed(u)]
                signs=signs_at(image,before,samples(archive,nearby,raster,space),space)
                if signs is None or {s["shape"] for s in signs.values()}!=set(ALTER) or len(signs)!=3:
                    continue
                by_type={s["shape"]:s["box"] for s in signs.values()}
                if overlap_design(coordinate(rest),by_type["SHARP"])<.88:
                    continue
                arc_boxes=[(a,b,c-a,d-b) for a,b,c,d in (fit[0] for arc in arcs for fit in arc["fits"])]
                if not bad_head(image,rows[6],after,arc_boxes,.39) or not bad_head(image,rows[12],after,[by_type["NATURAL"]],.54):
                    continue
                notes,nodes,_=sel
                rebuilt(sel,signs,rows)
                # Move the printed second left-hand note into the same voice between its neighbors.
                measure.remove(notes[14])
                measure.insert(list(measure).index(notes[13]),notes[14])
                for node in (notes[1],notes[6],notes[12],nodes[15],nodes[16]):
                    measure.remove(node)
                changes.append(dict(part=pid,measure=number,image_sha256=signed,
                                    flag=flag,rest=rest.get("id"),arcs=arcs,accidentals=signs))
    return (result,dict(state="restored",changes=changes)) if changes else (root,dict(state="unchanged",changes=[]))
