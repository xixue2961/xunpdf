"""Recover printed matching left/right piano accent columns."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_piano_horizontal_accent import accent, sim
from source_stemless_tremolo_projection import signature
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def same_accent(a,b):
    return max(sim(a,{(x+dx,y+dy) for x,y in b}) for dx in (-1,0,1) for dy in (-1,0,1))


def accent_at(image, x, edge, staff):
    offsets = range(2,28) if staff=="2" else range(-34,-6)
    found=[]
    for threshold in (130,165):
        cells={}
        for dy in offsets:
            candidate=accent(image,x,edge+dy,threshold)
            if candidate:
                b,shape=candidate
                cells[b]=shape
        ranked=sorted(cells.items(),key=lambda a:len(a[1]),reverse=True)
        if not ranked:
            return None
        bound,shape=ranked[0]
        if abs((bound[0]+bound[2])/2-x)>4 or any(
                max(abs(a-b) for a,b in zip(bound,k))>3 for k in cells):
            return None
        found.append((bound,shape))
    if max(abs(a-b) for a,b in zip(found[0][0],found[1][0]))>2 or same_accent(found[0][1],found[1][1])<.68:
        return None
    x0,y0,x1,y1=found[0][0]
    if (staff=="2" and not 2<=y0-edge<=19) or (staff=="1" and not 4<=edge-y1<=30):
        return None
    return found[0]


def recover(root,job):
    job=Path(job)
    meta_file=job/"source-native-notation.json"
    if not meta_file.is_file():
        return root,dict(state="not_applicable",changes=[])
    meta=json.loads(meta_file.read_text("utf8"))
    book,xml=(Path(meta[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if not meta.get("accepted_native_sha256") or sha(book)!=meta["accepted_native_sha256"]:
        return root,dict(state="not_applicable",changes=[])
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,dict(state="not_applicable",changes=[])
    clone=deepcopy(root)
    live=_score_events(clone)[0]
    native=_score_events(score(xml))[0]
    chords=defaultdict(list)
    for r in rows(xml.with_name("note-ledger.tsv")):
        if r["part"] in piano and r["staff_number"] in ("1","2") and r["shape"] in ("NOTEHEAD_BLACK","NOTEHEAD_VOID"):
            chords[r["part"],r["measure"],r["staff_number"],r["chord"]].append(r)
    bars=defaultdict(dict)
    for (part,bar,staff,chord),records in chords.items():
        if len(records)<2:
            continue
        records.sort(key=lambda r:int(r["note_index"]))
        keys=[(part,bar,int(r["note_index"])) for r in records]
        if any(k not in live or k not in native or signature(live[k])!=signature(native[k])
               or live[k]["node"].get("default-x")!=native[k]["node"].get("default-x") for k in keys):
            continue
        group=[live[k] for k in keys]
        nodes=[v["node"] for v in group]
        if any(n.find("pitch") is None or n.find("grace") is not None or n.find("cue") is not None
               or n.find("tie") is not None for n in nodes) or nodes[0].find("chord") is not None:
            continue
        x=median(int(r["x"])+int(r["w"])/2 for r in records)
        side=min(int(r["y"]) for r in records) if staff=="1" else max(int(r["y"])+int(r["h"]) for r in records)
        key=(staff,group[0]["start"])
        if key not in bars[part,bar]:
            bars[part,bar][key]=(records,nodes,x,side)
        else:
            bars[part,bar][key]=None
    changes=[]
    last_unit=None
    with ZipFile(book) as z:
        for (part,bar),found in bars.items():
            rhythm=sorted(time for st,time in found if st=="1" and found.get(("1",time)) and found.get(("2",time)))
            if len(rhythm)<3:
                continue
            targets=[(st,t,found[st,t]) for t in rhythm for st in ("1","2")]
            if not any(any(n.find("notations/articulations/accent") is not None for n in v[1]) for _,_,v in targets):
                continue
            if any(abs(found["1",t][2]-found["2",t][2])>5 for t in rhythm):
                continue
            contexts={(r["unit"],r["system"],r["source_sha256"]) for _,_,v in targets for r in v[0]}
            if len(contexts)!=1:
                continue
            unit,system,digest=next(iter(contexts))
            scan=graph(z,unit,system)
            if scan is None:
                continue
            nodes=with_bounds(scan)
            if not all(native_anchor(r,nodes) for _,_,v in targets for r in v[0]):
                continue
            if unit!=last_unit:
                img=job/f"unit-{int(unit):03d}.png"
                image_sha=sha(img)
                with Image.open(img) as p:
                    image=p.convert("L")
                last_unit=unit
            if image_sha!=digest:
                continue
            located=[(st,t,records,ns,accent_at(image,x,y,st)) for st,t,(records,ns,x,y) in targets]
            if any(v[-1] is None for v in located):
                continue
            donor=next(v[-1][1] for v in located if any(n.find("notations/articulations/accent") is not None for n in v[3]))
            if any(same_accent(v[-1][1],donor)<.72 for v in located):
                continue
            for st,t,records,notes,shape in located:
                if any(n.find("notations/articulations/accent") is not None for n in notes):
                    continue
                note=notes[0]
                notation=note.find("notations")
                if notation is None:
                    notation=ET.Element("notations")
                    note.insert(next((i for i,n in enumerate(note) if n.tag in ("lyric","play","listen")),len(note)),notation)
                art=notation.find("articulations")
                if art is None:
                    art=ET.SubElement(notation,"articulations")
                ET.SubElement(art,"accent",placement="above" if st=="1" else "below")
                changes.append(dict(part=part,measure=bar,staff=st,onset=str(t),box=shape[0],source_sha256=digest))
    changes.extend(recover_repeated_lower_marks(clone,job,book,xml,piano))
    return (clone,dict(state="restored",changes=changes)) if changes else (root,dict(state="not_applicable",changes=[]))


def note_address(v):
    n=v["node"]
    return n.get("default-x"), n.findtext("staff","1"), pitch(n), v["duration"]


def linked_staccato(g,nodes,chord):
    found=[nodes[rel.get("target")] for rel in g.findall("sig/relations/relation")
           if rel.get("source")==chord and rel.find("chord-articulation") is not None
           and rel.get("target") in nodes and nodes[rel.get("target")].get("shape")=="STACCATO"]
    return found[0] if len(found)==1 else None


def repeated_dot(image, source_dot, origin, target):
    x,y,w,h=coordinate(source_dot)
    if not 3<=w<=9 or not 3<=h<=9:
        return None
    dx,dy=round(target["x"]-origin["x"]),target["edge"]-origin["edge"]
    bounds=(x-2,y-2,x+w+2,y+h+2)
    reference=list(image.crop(bounds).getdata())
    best=None
    for xx in range(dx-2,dx+3):
        for yy in range(dy-2,dy+3):
            query=(bounds[0]+xx,bounds[1]+yy,bounds[2]+xx,bounds[3]+yy)
            if not 3<=query[1]+2-target["edge"]<=14 or query[-1]>target["accent"][0][1]:
                continue
            pixels=list(image.crop(query).getdata())
            evidence=[]
            for threshold in (130,155,185):
                a=[n<threshold for n in reference]
                b=[n<threshold for n in pixels]
                if min(sum(a),sum(b))<8:
                    break
                quality=sum(u and v for u,v in zip(a,b))/sum(u or v for u,v in zip(a,b))
                if quality<.88:
                    break
                evidence.append(quality)
            if len(evidence)==3 and (best is None or min(evidence)>min(best["overlap"])):
                best=dict(box=query,overlap=evidence)
    return best


def set_articulation(note,name):
    notation=note.find("notations")
    if notation is None:
        notation=ET.Element("notations")
        note.insert(next((i for i,n in enumerate(note) if n.tag in ("lyric","play","listen")),len(note)),notation)
    art=notation.find("articulations")
    if art is None:
        art=ET.SubElement(notation,"articulations")
    ET.SubElement(art,name,placement="below")


def recover_repeated_lower_marks(root,job,book,xml,piano):
    live=_score_events(root)[0]
    native=_score_events(score(xml))[0]
    addresses=defaultdict(list)
    for (part,bar,i),ev in live.items():
        if part in piano and ev["node"].find("pitch") is not None and ev["node"].get("default-x") is not None:
            addresses[part,bar,note_address(ev)].append(ev)
    raw_chords=defaultdict(list)
    for row in rows(xml.with_name("note-ledger.tsv")):
        if row["part"] in piano and row["staff_number"] in ("1","2") and row["shape"]=="NOTEHEAD_BLACK":
            raw_chords[row["part"],row["measure"],row["staff_number"],row["chord"]].append(row)
    pairs=defaultdict(lambda: defaultdict(list))
    for (pid,bar,staff,chord),group in raw_chords.items():
        if len(group)<2:
            continue
        group.sort(key=lambda a:int(a["note_index"]))
        events=[]
        for row in group:
            old=native.get((pid,bar,int(row["note_index"])))
            if old is None or not old["node"].get("default-x"):
                break
            possible=addresses[pid,bar,note_address(old)]
            if len(possible)!=1:
                break
            events.append(possible[0])
        else:
            notes=[e["node"] for e in events]
            onsets={e["start"] for e in events}
            if len(onsets)!=1 or notes[0].find("chord") is not None or any(n.find("chord") is None for n in notes[1:]):
                continue
            if any(n.find(t) is not None for n in notes for t in ("tie","cue","grace")):
                continue
            if staff=="1" and not any(n.find("notations/articulations/accent") is not None for n in notes):
                continue
            context={(r["unit"],r["system"],r["source_sha256"]) for r in group}
            if len(context)!=1:
                continue
            time=events[0]["start"]
            x=median(int(r["x"])+int(r["w"])/2 for r in group)
            edge=max(int(r["y"])+int(r["h"]) for r in group)
            pairs[pid,bar,*next(iter(context))][staff,time].append(
                dict(rows=group,notes=notes,chord=chord,x=x,edge=edge,time=str(time)))
    changes=[]
    with ZipFile(book) as archive, ZipFile(job/"raw/input.omr") as original:
        for (pid,bar,unit,system,digest),matched in pairs.items():
            aligned=[]
            for (staff,time),values in matched.items():
                if staff!="2" or len(values)!=1 or len(matched.get(("1",time),[]))!=1:
                    continue
                lower,upper=values[0],matched["1",time][0]
                if abs(lower["x"]-upper["x"])<=4:
                    aligned.append((lower,upper))
            if len(aligned)<2:
                continue
            loc=job/f"unit-{int(unit):03}.png"
            if sha(loc)!=digest:
                continue
            verified,raw=graph(archive,unit,system),graph(original,unit,system)
            if verified is None or raw is None:
                continue
            a,b=with_bounds(verified),with_bounds(raw)
            with Image.open(loc) as f:
                image=f.convert("L")
            valid=[]
            for lower,upper in aligned:
                if not all(native_anchor(row,q) for s in (lower,upper) for row in s["rows"] for q in (a,b)):
                    continue
                found=accent_at(image,lower["x"],lower["edge"],"2")
                if found is None:
                    continue
                lower["accent"]=found
                if any(n.find("notations/articulations/staccato") is not None for n in lower["notes"]):
                    lower["staccato"]=linked_staccato(verified,a,lower["chord"])
                valid.append(lower)
            for target in valid:
                peers=[other for other in valid if other is not target and other.get("staccato") is not None
                       and same_accent(target["accent"][1],other["accent"][1])>=.9]
                # The existing marked target can itself certify a dot on a different
                # repeated chord. Every proposed accent still needs a second shape.
                repeats=[other for other in valid if other is not target
                         and same_accent(target["accent"][1],other["accent"][1])>=.9]
                if not repeats or not (target.get("staccato") is not None or peers):
                    continue
                dot=None
                if target.get("staccato") is None:
                    witnesses=[repeated_dot(image,k["staccato"],k,target) for k in peers]
                    present=[v for v in witnesses if v is not None]
                    if len(present)!=1 or any(n.find("notations/articulations/staccato") is not None for n in target["notes"]):
                        continue
                    dot=present[0]
                missing=[]
                if not any(n.find("notations/articulations/accent") is not None for n in target["notes"]):
                    missing.append("accent")
                if dot is not None:
                    missing.append("staccato")
                for name in missing:
                    set_articulation(target["notes"][0],name)
                    changes.append(dict(part=pid,measure=bar,staff="2",onset=target["time"],
                                        mark=name,method="repeated_lower_chord_marks",
                                        box=target["accent"][0] if name=="accent" else dot["box"],
                                        source_sha256=digest))
    return changes
