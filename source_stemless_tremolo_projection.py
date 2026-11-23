"""Restore independently verified stemless three-stroke piano tremolos."""
from collections import defaultdict
from copy import deepcopy
import csv
from fractions import Fraction
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import score
from source_three_stroke_tremolo_ink_v3 import bounds
from source_whole_tremolo_heads import verify

STEPS="CDEFGAB"


def signature(event):
    n=event["node"]
    return (event["start"],event["duration"],n.findtext("voice","1"),n.findtext("staff","1"),
            n.findtext("pitch/step"),n.findtext("pitch/alter","0"),n.findtext("pitch/octave"))


def diatonic(note):
    return int(note.findtext("pitch/octave"))*7+STEPS.index(note.findtext("pitch/step"))


def inherited_context(part):
    fifths,meter=None,None
    for measure in part.findall("measure"):
        for a in measure.findall("attributes"):
            key=a.findall("key")
            if key:
                values={k.findtext("fifths") for k in key}
                fifths=int(next(iter(values))) if len(values)==1 and None not in values else None
            time=a.findall("time")
            if time:
                results={(n.findtext("beats"),n.findtext("beat-type")) for n in time}
                meter=next(iter(results)) if len(results)==1 else None
        yield measure.get("number"),(fifths,meter)


def engrave(note_template, pitch, duration, first_chord_member, mark=None):
    note=deepcopy(note_template)
    if pitch is not None:
        note.attrib.clear()
        for field in list(note):
            if field.tag not in {"pitch","duration","voice","type","staff"}:
                note.remove(field)
        p=note.find("pitch")
        p.clear()
        step,octave,alter=pitch
        ET.SubElement(p,"step").text=step
        if alter:
            ET.SubElement(p,"alter").text=str(alter)
        ET.SubElement(p,"octave").text=str(octave)
        if not first_chord_member:
            note.insert(0,ET.Element("chord"))
    note.find("duration").text=str(duration)
    t=ET.Element("time-modification")
    ET.SubElement(t,"actual-notes").text="2"
    ET.SubElement(t,"normal-notes").text="1"
    note.insert(list(note).index(note.find("type"))+1,t)
    if mark:
        notation=note.find("notations")
        if notation is None:
            notation=ET.SubElement(note,"notations")
        ornaments=ET.SubElement(notation,"ornaments")
        ET.SubElement(ornaments,"tremolo",{"type":mark}).text="3"
    return note


def exact_staff_groups(source,site,graph,bar,events,native,entries,key):
    """Return complete replacements only for both straight source whole-note lanes."""
    chosen={}
    for number,staff in enumerate(site["heads"],1):
        staff_number=str(number)
        glyphs=staff["columns"]
        anchors={}
        for head in glyphs[0]:
            item=entries.get((str(site["unit"]),site["system"],head["existing_head"]),[])
            if not item:
                continue
            if len(item)!=1:
                return None
            row=item[0]
            k=row["part"],row["measure"],int(row["note_index"])
            if (k[:2]!=key or row["staff"]!=staff["staff"] or row["staff_number"]!=staff_number
                    or row["source_sha256"]!=site["source_sha256"] or k not in events or k not in native
                    or signature(events[k])!=signature(native[k])):
                return None
            node=events[k]["node"]
            if signature(events[k])[:2]!=(Fraction(0),Fraction(4)) or node.findtext("type")!="whole":
                return None
            anchors[head["step"]]=node
        if not anchors:
            return None
        staff_notes=[node for node in bar.findall("note") if node.findtext("staff","1")==staff_number]
        if set(staff_notes)!=set(anchors.values()) or staff_notes[0].find("chord") is not None:
            return None
        if any(node.find(f) is not None for node in staff_notes for f in (
                "rest","tie","grace","time-modification","accidental","dot","beam","lyric","notations/tied","notations/ornaments")):
            return None
        fifths=source[key][0]
        terms={step:1 for step in "FCGDAEB"[:fifths]} if fifths>=0 else {step:-1 for step in "BEADGCF"[:-fifths]}
        if any(n.findtext("pitch/alter","0")!=str(terms.get(n.findtext("pitch/step"),0)) for n in staff_notes):
            return None
        baselines={diatonic(n)+s for s,n in anchors.items()}
        if len(baselines)!=1:
            return None
        baseline=baselines.pop()
        space=site["strokes"][("upper","lower")[number-1]]["space"]
        left,right=site["columns"]
        if any(left-3*space<=bounds(n)[0]<=right+space for n in graph.findall(
                f"sig/inters/alter[@staff='{staff['staff']}']")):
            return None
        duration=int(staff_notes[0].findtext("duration"))
        if duration%2 or any(n.findtext("duration")!=str(duration) for n in staff_notes):
            return None
        first=[engrave(n,None,duration//2,i==0,"start" if i==0 else None) for i,n in enumerate(staff_notes)]
        pitch=lambda step: (STEPS[(baseline-step)%7],(baseline-step)//7,terms.get(STEPS[(baseline-step)%7],0))
        first.extend(engrave(staff_notes[0],pitch(g["step"]),duration//2,False) for g in glyphs[0] if g["step"] not in anchors)
        second=[engrave(staff_notes[0],pitch(g["step"]),duration//2,i==0,"stop" if i==0 else None)
                for i,g in enumerate(sorted(glyphs[1],key=lambda g:-g["step"]))]
        chosen[staff_number]=(staff_notes,first+second,
                            [[pitch(g["step"]) for g in column] for column in glyphs])
    if len(chosen)!=2 or len(bar.findall("backup"))!=1 or bar.findall("forward"):
        return None
    if bar.findtext("backup/duration")!=chosen["1"][0][0].findtext("duration"):
        return None
    if any(list(bar).index(notes[-1])-list(bar).index(notes[0])!=len(notes)-1 for notes,_,_ in chosen.values()):
        return None
    return chosen


def recover(root, job):
    job=Path(job)
    receipt=job/"source-native-notation.json"
    if not receipt.is_file():
        return root,{"changes":[]}
    source_receipt=json.loads(receipt.read_text("utf-8"))
    native_path=Path(source_receipt["accepted_native_xml"])
    ledger=native_path.with_name("note-ledger.tsv")
    if not ledger.is_file() and not source_receipt["stages"]:
        native_path=job/"source-native-notation/curve-overlap-ledger/result.mxl"
        ledger=native_path.with_name("note-ledger.tsv")
    if not ledger.is_file():
        return root,{"changes":[]}
    raw=verify(job)
    sites=[site for site in raw["certified_sites"] if site["head_shape"]=="WHOLE_NOTE"]
    if not sites:
        return root,{"changes":[]}
    entries=defaultdict(list)
    with ledger.open(encoding="utf-8",newline="") as stream:
        for r in csv.DictReader(stream,delimiter="\t"):
            if r["repeat_copy"]=="false":
                entries[r["unit"],r["system"],r["head"]].append(r)
    fixed=deepcopy(root)
    events,_=_score_events(fixed)
    native,_=_score_events(score(native_path))
    contexts={(part.get("id"),m):details for part in fixed.findall("part")
              for m,details in inherited_context(part)}
    changes=[]
    with ZipFile(raw["book"]) as book:
        for site in sites:
            keys=set()
            for lane in site["heads"]:
                for head in lane["columns"][0]:
                    if head["existing_head"]:
                        for row in entries[(str(site["unit"]),site["system"],head["existing_head"])]:
                            keys.add((row["part"],row["measure"]))
            if len(keys)!=1:
                continue
            key=keys.pop()
            fifths,meter=contexts[key]
            if fifths is None or abs(fifths)>7 or meter!=("4","4"):
                continue
            bar=fixed.find(f"part[@id='{key[0]}']/measure[@number='{key[1]}']")
            if bar is None or bar.get("implicit")=="yes":
                continue
            unit=site["unit"]
            graph=ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml")).find(
                f"page/system[@id='{site['system']}']")
            lanes=exact_staff_groups(contexts,site,graph,bar,events,native,entries,key)
            if lanes is None:
                continue
            for before,replacement,_ in lanes.values():
                index=list(bar).index(before[0])
                for node in before:
                    bar.remove(node)
                for i,node in enumerate(replacement,index):
                    bar.insert(i,node)
            changes.append(dict(part=key[0],measure=key[1],source_sha256=site["source_sha256"],
                                printed_heads={s:details[2] for s,details in lanes.items()},strokes=3))
    return (fixed if changes else root),dict(changes=changes)
