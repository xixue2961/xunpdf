"""Recover missing piano triplets from the original same-bar printed number."""
from collections import defaultdict
from copy import deepcopy
import csv
from fractions import Fraction as F
from hashlib import sha256
from io import BytesIO
from itertools import product
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score
from source_stemless_tremolo_projection import inherited_context, signature
from source_three_stroke_tremolo_ink_v3 import bounds


def read(path):
    return json.loads(path.read_text("utf-8"))


def exact_meter(info,unit,system,x):
    active=None
    for entry in info["results"]:
        same=(entry["unit"],entry["system"])==(unit,system)
        for col in entry["source_columns"]:
            if col["kind"]=="courtesy" or same and col["x"]>x:
                continue
            active=col
        if same:
            break
    else:
        return False
    return bool(active and not active["conflicting_time_at_same_column"] and active["time"]=="4/4"
                and len({v["staff"] for v in active["votes"]})>=2)


def groups_from_graph(graph, ids):
    nodes={n.get("id"):n for n in graph.findall("sig/inters/*")}
    stem={}
    links=defaultdict(set)
    triplets=defaultdict(set)
    for r in graph.findall("sig/relations/*"):
        a,b,kind=r.get("source"),r.get("target"),r[0].tag
        if kind=="chord-stem" and a in ids:
            stem[a]=b
        elif kind=="beam-stem":
            links[a].add(b)
        elif kind=="chord-tuplet":
            triplets[b].add(a)
    owners={s:c for c,s in stem.items()}
    grouped=defaultdict(list)
    for id,stems in links.items():
        if len(stems)==3 and stems<=owners.keys():
            ch=tuple(sorted((owners[s] for s in stems),key=lambda c:bounds(nodes[c])[0]))
            grouped[ch].append(nodes[id])
    return nodes,grouped,triplets


def locate_number(gray,template,beam,space):
    left,top,right,bottom=beam
    cy=(left+right)/2
    h,w=template.shape
    x0=round(cy-w/2)
    ideals=[]
    for side in ("above","below"):
        gaps=range(1,round(.85*space)+1)
        ys=[top-h-g for g in gaps] if side=="above" else [bottom+g for g in gaps]
        for x,y in product(range(x0-round(.5*space),x0+round(.5*space)+1),ys):
            if x<0 or y<0:
                continue
            crop=gray[y:y+h,x:x+w]
            if crop.shape!=template.shape:
                continue
            values=[]
            for cutoff in (105,125,150):
                a,b=template<cutoff,crop<cutoff
                values.append(float((a&b).sum())/max(1,(a|b).sum()))
            if min(values)>=.80:
                ideals.append((min(values),x,y,side))
    if not ideals:
        return None
    best=max(ideals)
    if any(abs(r[1]-best[1])>2 or abs(r[2]-best[2])>2 or r[3]!=best[3] for r in ideals):
        return None
    return dict(bounds=[best[1],best[2],w,h],side=best[3],overlap=float(best[0]))


def identify_triplets(gray,graph,lanes):
    ids={g["chord"] for lane in lanes.values() for g in lane}
    nodes,beams,linked=groups_from_graph(graph,ids)
    beam_boxes={}
    for group,tracks in beams.items():
        if not 1<=len(tracks)<=2:
            continue
        boxes=[bounds(n) for n in tracks]
        beam_boxes[group]=(min(x for x,y,w,h in boxes),min(y for x,y,w,h in boxes),
                           max(x+w for x,y,w,h in boxes),max(y+h for x,y,w,h in boxes))
    donors=[]
    for group,box in beam_boxes.items():
        matches=[nodes[n] for n,linked_chords in linked.items() if linked_chords==set(group)
                 and n in nodes and nodes[n].get("shape")=="TUPLET_THREE"]
        if len(matches)!=1:
            continue
        for staff,lane in lanes.items():
            matching=[r for r in lane if r["chord"] in group]
            if len(matching)!=3 or any(n.findtext("time-modification/actual-notes")!="3"
                    or n.findtext("time-modification/normal-notes")!="2" for g in matching for n in g["notes"]):
                continue
            x,y,w,h=bounds(matches[0])
            if .75*lane[0]["space"]<=w<=1.5*lane[0]["space"] and 1.05*lane[0]["space"]<=h<=1.85*lane[0]["space"]:
                donors.append((gray[y:y+h,x:x+w],staff,group,matches[0].get("id")))
    if len(donors)!=1:
        return {}
    template,_,donor_group,identity=donors[0]
    found={}
    for group,box in beam_boxes.items():
        for lane in lanes.values():
            ordered=[r["chord"] for r in lane]
            if group[0] not in ordered:
                continue
            start=ordered.index(group[0])
            if tuple(ordered[start:start+3])!=group:
                continue
            wanted="16th" if len(beams[group])==2 else "eighth"
            if any(n.findtext("type")!=wanted for r in lane[start:start+3] for n in r["notes"]):
                continue
            mark=locate_number(gray,template,box,lane[0]["space"])
            if mark is not None:
                found[group]=dict(mark,template_native_id=identity,beam_ids=[b.get("id") for b in beams[group]])
    return found if donor_group in found else {}


def capture_directions(bar,index,groups,marked):
    directions=defaultdict(list)
    removed=[]
    processed=set()
    i=0
    for elem in bar:
        if elem.tag=="note":
            i+=1
        elif elem.tag=="direction":
            if i not in index or elem.find("offset") is not None:
                return None
            staff,chord=index[i]
            fields=elem.findall("direction-type/*")
            if elem.find("sound") is None and len(fields)==1 and fields[0].tag=="words" and (fields[0].text or "").strip()=="3":
                possible=[group for group, evidence in marked.items() if chord in group
                          and elem.findtext("staff","1")==staff and elem.get("placement")==evidence["side"]]
                if len(possible)!=1:
                    return None
                removed.append(dict(staff=staff,chord=chord,bounds=marked[possible[0]]["bounds"]))
            elif len(fields)==1 and fields[0].tag=="wedge":
                typ=fields[0].get("type")
                available=elem.findtext("staff","1")
                if typ not in ("crescendo","diminuendo","stop") or available not in groups:
                    return None
                positions=[x for x in groups[available].values() if abs(x["x"]-groups[staff][chord]["x"])<=2]
                if len(positions)!=1 or positions[0]["new_start"]!=groups[staff][chord]["new_start"]:
                    return None
                token=available,fields[0].get("number","1")
                if typ=="stop":
                    if token not in processed:
                        return None
                    processed.remove(token)
                else:
                    if token in processed:
                        return None
                    processed.add(token)
                directions[staff,chord].append(deepcopy(elem))
            else:
                return None
    return (directions,removed) if not processed else None


def reconstruct(bar,lanes,marks,divisions):
    accepted={"print","attributes","note","direction","backup","forward","barline"}
    if any(node.tag not in accepted for node in bar):
        return None
    rhythms={"quarter":F(1),"eighth":F(1,2),"16th":F(1,4)}
    membership={}
    for group in marks:
        for i,chord in enumerate(group):
            if chord in membership:
                return None
            membership[chord]=(i,group)
    if not membership:
        return None
    did_change=False
    for lane in lanes.values():
        clock=F(0)
        for group in lane:
            nodes=group["notes"]
            kind=nodes[0].findtext("type")
            chord=group["chord"]
            if kind not in rhythms or any(n.findtext("type")!=kind or n.find("dot") is not None for n in nodes):
                return None
            group["new_start"]=clock
            duration=rhythms[kind]
            if chord in membership:
                duration*=F(2,3)
            for n in nodes:
                modification=n.find("time-modification")
                if modification is None and chord in membership:
                    did_change=True
                if modification is not None and (chord not in membership
                    or (modification.findtext("actual-notes"),modification.findtext("normal-notes"))!=("3","2")):
                    return None
            group["new_duration"]=duration
            clock+=duration
        if clock!=4:
            return None
    if not did_change:
        return None
    left,right=lanes.values()
    for g in right:
        aligned=[v for v in left if abs(g["x"]-v["x"])<=2]
        if len(aligned)!=1 or aligned[0]["new_start"]!=g["new_start"]:
            return None
    by_staff={s:{r["chord"]:r for r in groups} for s,groups in lanes.items()}
    index={g["indices"][0]:(s,g["chord"]) for s,groups in lanes.items() for g in groups}
    # Direction anchors in the existing export identify the next physical stem.
    captured=capture_directions(bar,index,by_staff,marks)
    if captured is None:
        return None
    attached,removed=captured
    pre=[deepcopy(n) for n in bar if n.tag in ("print","attributes")]
    end=[deepcopy(n) for n in bar if n.tag=="barline"]
    live=list(bar)
    first=next(i for i,v in enumerate(live) if v.tag=="note")
    if any(v.tag in ("print","attributes") for v in live[first:]):
        return None
    stream=pre
    for i,(s,groups) in enumerate(lanes.items()):
        if i:
            backup=ET.Element("backup")
            ET.SubElement(backup,"duration").text=str(4*divisions)
            stream.append(backup)
        voice=groups[0]["notes"][0].findtext("voice","1")
        for g in groups:
            chord=g["chord"]
            stream.extend(attached[s,chord])
            duration=g["new_duration"]*divisions
            if duration.denominator!=1:
                return None
            for member,old in enumerate(g["notes"]):
                n=deepcopy(old)
                n.find("voice").text=voice
                n.find("duration").text=str(duration.numerator)
                if chord in membership and n.find("time-modification") is None:
                    a=ET.Element("time-modification")
                    ET.SubElement(a,"actual-notes").text="3"
                    ET.SubElement(a,"normal-notes").text="2"
                    anchor=n.find("accidental")
                    if anchor is None:
                        anchor=n.find("type")
                    n.insert(list(n).index(anchor)+1,a)
                    position,_=membership[chord]
                    if member==0 and position in (0,2):
                        notation=n.find("notations")
                        if notation is None:
                            notation=ET.SubElement(n,"notations")
                        ET.SubElement(notation,"tuplet",{"type":"start" if position==0 else "stop","number":"1"})
                stream.append(n)
    bar[:]=stream+end
    return removed


def recover(root,job):
    job=Path(job)
    proof=job/"source-native-notation.json"
    meter=job/"source-tuplet-evidence/meter/source-meters.json"
    if not proof.is_file() or not meter.is_file():
        return root,{"changes":[]}
    info=read(proof)
    full=Path(info["accepted_native_xml"])
    ledger=full.with_name("head-ledger.tsv")
    if not ledger.is_file() and not info["stages"]:
        full=job/"source-native-notation/curve-overlap-ledger/result.mxl"
        ledger=full.with_name("head-ledger.tsv")
    if not ledger.is_file():
        return root,{"changes":[]}
    control,_=_score_events(score(full))
    out=deepcopy(root)
    exported,_=_score_events(out)
    source_meter=read(meter)
    rows=defaultdict(list)
    with ledger.open(encoding="utf-8",newline="") as stream:
        for row in csv.DictReader(stream,delimiter="\t"):
            if row["repeat_copy"]=="false" and row["shape"]=="NOTEHEAD_BLACK":
                rows[row["part"],row["measure"]].append(row)
    changes=[]
    with ZipFile(info["accepted_native_book"]) as book:
        for part in out.findall("part"):
            if not part.findall("measure/attributes/staves"):
                continue
            meters=dict(inherited_context(part))
            divisions=1
            for bar in part.findall("measure"):
                s=bar.findtext("attributes/divisions")
                if s is not None:divisions=int(s)
                m=bar.get("number")
                key=(part.get("id"),m)
                notes=bar.findall("note")
                if meters[m][1]!=("4","4") or not notes or len(notes)!=len(rows[key]) or any(n.find("pitch") is None for n in notes):
                    continue
                # Overshoot is a filter. The ink, written values, and column agreement below are the evidence.
                current=[exported.get((*key,i)) for i in range(len(notes))]
                if any(e is None for e in current) or max(e["start"]+e["duration"] for e in current)<=4:
                    continue
                rs=sorted(rows[key],key=lambda v:int(v["note_index"]))
                serial={(int(r["unit"]),r["system"],r["native_part"],r["source_sha256"]) for r in rs}
                if len(serial)!=1:
                    continue
                if any(int(r["note_index"])!=i or (*key,i) not in control
                       or signature(current[i])!=signature(control[*key,i]) for i,r in enumerate(rs)):
                    continue
                u,sys,native_part,hash_value=serial.pop()
                if not exact_meter(source_meter,u,sys,min(int(r["x"]) for r in rs)):
                    continue
                image_content=(job/f"unit-{u:03d}.png").read_bytes()
                if sha256(image_content).hexdigest()!=hash_value:
                    continue
                graph=ET.fromstring(book.read(f"sheet#{u}/sheet#{u}.xml")).find(f"page/system[@id='{sys}']")
                if graph is None:
                    continue
                origin=graph.find(f"part[@id='{native_part}']")
                staves=origin.findall("staff") if origin is not None else []
                if len(staves)!=2:
                    continue
                lanes={}
                for staff_number,staff in enumerate(staves,1):
                    line_y=[float(r.find("point").get("y")) for r in staff.findall("lines/line")]
                    if len(line_y)!=5:
                        break
                    physical=defaultdict(list)
                    for r in rs:
                        if r["staff"]==staff.get("id") and r["staff_number"]==str(staff_number):
                            physical[r["chord"]].append(r)
                    lane=[]
                    for chord,group in physical.items():
                        indices=[int(r["note_index"]) for r in group]
                        selected=[notes[i] for i in indices]
                        if (selected[0].find("chord") is not None or any(n.find("chord") is None for n in selected[1:])
                            or indices!=list(range(indices[0],indices[0]+len(indices)))):
                            lane=[]
                            break
                        lane.append(dict(chord=chord,indices=indices,notes=selected,
                                         x=min(int(r["x"]) for r in group),space=(line_y[-1]-line_y[0])/4))
                    if not lane or any(a["x"]>=b["x"] for a,b in zip(lane,lane[1:])):
                        break
                    lanes[str(staff_number)]=lane
                if len(lanes)!=2 or sum(len(g["notes"]) for lane in lanes.values() for g in lane)!=len(notes):
                    continue
                with Image.open(BytesIO(image_content)) as page:
                    gray=np.asarray(page.convert("L"))
                marks=identify_triplets(gray,graph,lanes)
                if not marks:
                    continue
                omitted=reconstruct(bar,lanes,marks,divisions)
                if omitted is not None:
                    changes.append(dict(part=key[0],measure=m,source_sha256=hash_value,
                                        groups=[dict(mark,chords=group) for group,mark in marks.items()],
                                        omitted_misread_numbers=omitted))
    return (out if changes else root),dict(changes=changes)
