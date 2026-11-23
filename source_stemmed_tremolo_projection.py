"""Resolve a paired half-note tremolo with a source-proven cadential tail."""
from collections import defaultdict
from copy import deepcopy
import csv
from fractions import Fraction
import json
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import score
from source_stemless_tremolo_projection import STEPS, diatonic, engrave, inherited_context, signature
from source_three_stroke_tremolo_ink_v3 import bounds, head_center
from source_whole_tremolo_heads import verify


def native_shape(graph,site):
    part=graph.find(f"part[@id='{site['piano']}']")
    measure=part.find(f"measure[@id='{site['stack']}']")
    nodes={node.get("id"):node for node in graph.findall("sig/inters/*")}
    references=defaultdict(list)
    for rel in graph.findall("sig/relations/*"):
        references[rel.get("source"),rel[0].tag].append(rel.get("target"))
    staffs=part.findall("staff")
    groups={}
    for order,staff in enumerate(staffs):
        sid=staff.get("id")
        ids=[i for i in measure.findtext("head-chords","").split() if nodes[i].get("staff")==sid]
        heads={i:[nodes[h] for h in references[i,"containment"]] for i in ids}
        ids.sort(key=lambda i:head_center(heads[i]))
        if len(ids)!=3 or ids[:2]!=site["chords"][order]:
            return None
        tail=ids[-1]
        if not heads[tail] or any(n.get("shape")!="NOTEHEAD_BLACK" for n in heads[tail]):
            return None
        stems=references[tail,"chord-stem"]
        flags=[i for i in measure.findtext("flags","").split() if references[i,"flag-stem"]==stems]
        if len(stems)!=1 or len(flags)!=1 or nodes[flags[0]].get("shape") not in {"FLAG_1","FLAG_1_DOWN"}:
            return None
        if any((a==stems[0] or stems[0] in b) and "beam" in kind for (a,kind),b in references.items()):
            return None
        rests=[]
        between=[]
        stroke=site["strokes"][("upper","lower")[order]]
        spac=stroke["space"]
        x0,x1=stroke["columns"]
        stroke_y=stroke["thresholds"][120]["centers"]
        for rest in measure.findtext("rest-chords","").split():
            related=references[rest,"containment"]
            if len(related)!=1:
                return None
            node=nodes[related[0]]
            if node.get("staff")!=sid:
                continue
            x,y,w,h=bounds(node)
            typ=node.get("shape")
            if (node.tag=="rest" and typ in {"HALF_REST","WHOLE_REST"} and x<=x1 and x+w>=x0
                    and any(y-.2*spac<=yy<=y+h+.2*spac for yy in stroke_y)):
                between.append(related[0])
            else:
                rests.append((x,typ,related[0]))
        rests.sort()
        if [r[1] for r in rests]!=["EIGHTH_REST","QUARTER_REST"] or not head_center(heads[tail])<rests[0][0]<rests[1][0]:
            return None
        left,right=site["columns"]
        if any(left-3*spac<=bounds(n)[0]<=head_center(heads[tail])+spac
               for n in graph.findall(f"sig/inters/alter[@staff='{sid}']")):
            return None
        groups[str(order+1)]=dict(staff=staff,heads=heads,groups=ids,stems=stems,flags=flags,
            rests=[r[2] for r in rests],ghosts=between,
            dynamic=[nodes[i].get("shape") for i in references[tail,"chord-dynamics"]],
            accent=[nodes[i].get("shape") for i in references[tail,"chord-articulation"]])
    return groups if len(groups)==2 else None


def make_note(kind,duration,voice,staff,*,pitch=None,chord=False,accent=False):
    note=ET.Element("note")
    if chord:
        ET.SubElement(note,"chord")
    if pitch:
        step,alter,octave=pitch
        element=ET.SubElement(note,"pitch")
        ET.SubElement(element,"step").text=step
        if alter:
            ET.SubElement(element,"alter").text=str(alter)
        ET.SubElement(element,"octave").text=str(octave)
    else:
        ET.SubElement(note,"rest")
    for key,value in (("duration",duration),("voice",voice),("type",kind),("staff",staff)):
        ET.SubElement(note,key).text=str(value)
    if accent:
        ET.SubElement(ET.SubElement(ET.SubElement(note,"notations"),"articulations"),"accent")
    return note


def make_dynamic(staff):
    direction=ET.Element("direction",{"placement":"below"})
    ET.SubElement(ET.SubElement(ET.SubElement(direction,"direction-type"),"dynamics"),"ff")
    ET.SubElement(direction,"staff").text=staff
    return direction


def resolve_bar(root,site,shapes,entries,events,native,contexts):
    notes={}
    all_anchors=set()
    ghosts=set()
    key=None
    for staff,shape in shapes.items():
        staff_notes=[]
        for i,chord in enumerate(shape["groups"][:2]):
            anchored={}
            for head in shape["heads"][chord]:
                candidates=entries[str(site["unit"]),site["system"],head.get("id")]
                if len(candidates)!=1:
                    return None
                row=candidates[0]
                address=row["part"],row["measure"],int(row["note_index"])
                key=key or address[:2]
                if (key!=address[:2] or row["staff_number"]!=staff or row["source_sha256"]!=site["source_sha256"]
                        or address not in events or address not in native
                        or signature(events[address])!=signature(native[address])
                        or signature(events[address])[:2]!=(Fraction(2*i),Fraction(2))):
                    return None
                anchored[address[2]]=(head,events[address]["node"])
            staff_notes.append([anchored[k] for k in sorted(anchored)])
            all_anchors.update(n for _,n in anchored.values())
        notes[staff]=staff_notes
        for ghost in shape["ghosts"]:
            for row in entries[str(site["unit"]),site["system"],ghost]:
                address=row["part"],row["measure"],int(row["note_index"])
                if (address[:2]!=key or row["staff_number"]!=staff
                        or row["source_sha256"]!=site["source_sha256"] or address not in events):
                    return None
                n=events[address]["node"]
                if n.find("rest") is None:
                    return None
                ghosts.add(n)
    if key is None or not ghosts:
        return None
    fifths,meter=contexts[key]
    if fifths is None or abs(fifths)>7 or meter!=("4","4"):
        return None
    bar=root.find(f"part[@id='{key[0]}']/measure[@number='{key[1]}']")
    if bar is None or bar.get("implicit")=="yes" or set(bar.findall("note"))!=all_anchors|ghosts:
        return None
    before=list(bar)
    if any(n.tag not in {"note","backup","forward"} for n in before):
        return None
    if any(n.findtext("type")!="half" or any(n.find(tag) is not None for tag in (
        "tie","dot","beam","grace","lyric","accidental","time-modification","notations")) for n in all_anchors):
        return None
    if any(group[0][1].find("chord") is not None or any(n.find("chord") is None for _,n in group[1:])
           for pair in notes.values() for group in pair):
        return None
    duration={int(n.findtext("duration")) for n in all_anchors}
    if len(duration)!=1:
        return None
    half=duration.pop()
    if half<=0 or half%4 or any(int(n.findtext("duration"))!=2*half for n in bar.findall("backup")):
        return None
    terms={s:(1 if fifths>=0 else -1) for s in ("FCGDAEB" if fifths>=0 else "BEADGCF")[:abs(fifths)]}
    full=[]
    report=[]
    for staff,shape in shapes.items():
        if full:
            backup=ET.Element("backup")
            ET.SubElement(backup,"duration").text=str(2*half)
            full.append(backup)
        voice=notes[staff][0][0][1].findtext("voice","1")
        line_y=[float(line.find("point").get("y")) for line in shape["staff"].findall("lines/line")]
        top=line_y[0]
        space=median(b-a for a,b in zip(line_y,line_y[1:]))
        def position(head):
            _,y,_,h=bounds(head)
            raw=2*(y+h/2-top)/space
            step=round(raw)
            if abs(step-raw)>.34:
                raise ValueError("note head does not align with a unique staff step")
            return step
        baselines={diatonic(n)+position(h) for group in notes[staff] for h,n in group}
        if len(baselines)!=1 or any(n.findtext("pitch/alter","0")!=str(terms.get(n.findtext("pitch/step"),0))
                                    for group in notes[staff] for _,n in group):
            return None
        baseline=baselines.pop()
        for i,group in enumerate(notes[staff]):
            for number,(_,n) in enumerate(group):
                new=engrave(n,None,half//2,number==0,("start" if i==0 else "stop") if number==0 else None)
                new.find("voice").text=voice
                full.append(new)
        tail=shape["heads"][shape["groups"][-1]]
        tail_pos=sorted((position(h) for h in tail),reverse=True)
        if len(tail_pos)!=len(set(tail_pos)) or shape["dynamic"] not in ([],["DYNAMICS_FF"]) or shape["accent"] not in ([],["ACCENT"]):
            return None
        if shape["dynamic"]:
            full.append(make_dynamic(staff))
        pitches=[]
        for i,step in enumerate(tail_pos):
            octave,index=divmod(baseline-step,7)
            pitch=STEPS[index],terms.get(STEPS[index],0),octave
            pitches.append(pitch)
            full.append(make_note("eighth",half//4,voice,staff,pitch=pitch,chord=i>0,accent=i==0 and bool(shape["accent"])))
        full.extend((make_note("eighth",half//4,voice,staff),make_note("quarter",half//2,voice,staff)))
        report.append(dict(staff=staff,head_ids=[h.get("id") for h in tail],pitches=pitches,
                           discarded_stroke_rests=shape["ghosts"],flag_ids=shape["flags"],rest_ids=shape["rests"]))
    bar[:]=full
    return dict(part=key[0],measure=key[1],source_sha256=site["source_sha256"],staves=report)


def recover(root,job):
    job=Path(job)
    receipt=job/"source-native-notation.json"
    if not receipt.is_file():
        return root,{"changes":[]}
    data=json.loads(receipt.read_text("utf-8"))
    native_path=Path(data["accepted_native_xml"])
    ledger=native_path.with_name("note-ledger.tsv")
    if not ledger.is_file() and not data["stages"]:
        native_path=job/"source-native-notation/curve-overlap-ledger/result.mxl"
        ledger=native_path.with_name("note-ledger.tsv")
    if not ledger.is_file():
        return root,{"changes":[]}
    raw=verify(job)
    sites=[site for site in raw["certified_sites"] if site["head_shape"]=="NOTEHEAD_VOID"]
    if not sites:
        return root,{"changes":[]}
    entries=defaultdict(list)
    with ledger.open(encoding="utf-8",newline="") as stream:
        for row in csv.DictReader(stream,delimiter="\t"):
            if row["repeat_copy"]=="false":
                entries[row["unit"],row["system"],row["head"]].append(row)
    fixed=deepcopy(root)
    events,_=_score_events(fixed)
    native,_=_score_events(score(native_path))
    contexts={(part.get("id"),m):context for part in fixed.findall("part")
              for m,context in inherited_context(part)}
    changes=[]
    with ZipFile(raw["book"]) as book:
        for site in sites:
            unit=site["unit"]
            graph=ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml")).find(
                f"page/system[@id='{site['system']}']")
            shapes=native_shape(graph,site)
            if shapes:
                result=resolve_bar(fixed,site,shapes,entries,events,native,contexts)
                if result is not None:
                    changes.append(result)
    return (fixed if changes else root),dict(changes=changes)
