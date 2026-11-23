"""Project a source-verified piano rhythm repair into the polished MusicXML."""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from source_lyric_roles import _score_events


def _scope(events, target):
    return {k:v for k,v in events.items() if k[:2] in target}


def _fingerprint(event):
    note=event["node"]
    pitch=note.find("pitch")
    return (event["start"],event["duration"],note.findtext("staff","1"),
            tuple(pitch.findtext(k,"0") for k in ("step","alter","octave")) if pitch is not None else None,
            note.find("rest") is not None,note.find("grace") is not None,
            tuple(n.findtext("text") for n in note.findall("lyric")))


def _clocks(score, targets):
    output={}
    for part in score.findall("part"):
        divisions=1
        meter=None
        for measure in part.findall("measure"):
            if value:=measure.findtext("attributes/divisions"):
                divisions=int(value)
            time=measure.find("attributes/time")
            if time is not None:
                terms=time.findall("beats")
                units=time.findall("beat-type")
                meter=Fraction(int(terms[0].text)*4,int(units[0].text)) if len(terms)==len(units)==1 else None
            key=part.get("id"),measure.get("number")
            if key in targets:
                output[key]=(divisions,meter)
    return output


def _signature(group):
    return Counter(_fingerprint(event) for event in group.values())


def _mark(note, values):
    for notations in note.findall("notations"):
        for art in notations.findall("articulations"):
            for child in list(art):
                if child.tag in ("accent","staccato"):
                    art.remove(child)
            if not len(art):
                notations.remove(art)
        if not len(notations):
            note.remove(notations)
    if not values:
        return
    notation=note.find("notations")
    if notation is None:
        notation=ET.SubElement(note,"notations")
    art=notation.find("articulations")
    if art is None:
        art=ET.SubElement(notation,"articulations")
    for value in ("accent","staccato"):
        if value in values:
            ET.SubElement(art,value)


def _direction(template, tick, staff):
    if template.find("offset") is not None:
        raise ValueError("prior position encoded ambiguously")
    item=deepcopy(template)
    if tick:
        after_types=1+max(i for i,child in enumerate(item) if child.tag=="direction-type")
        item.insert(after_types,ET.Element("offset"))
        item.find("offset").text=str(tick)
    element=item.find("staff")
    if element is None:
        raise ValueError("original piano direction does not name a staff")
    element.text=staff
    return item


def project(cooked, control, native, expression):
    """Apply a checked repair to its two piano measures and retain the cooked score context."""
    if expression is None:
        raise ValueError("source expression evidence unavailable")
    source_refs={(r["part"],r["measure"],int(r["note_index"])):r for r in expression["articulations"]}
    targets={key[:2] for key in source_refs}
    if len(targets)!=2 or len({key[0] for key in targets})!=1:
        raise ValueError("unexpected reported piano target scope")
    original, prior_marks=_score_events(cooked)
    raw_original,_=_score_events(control)
    raw_corrected,_=_score_events(native)
    if {k:v for k,v in original.items() if k[:2] in targets}.keys() != _scope(raw_original,targets).keys():
        raise ValueError("piano scope changed before projection")
    if _signature(_scope(original,targets))!=_signature(_scope(raw_original,targets)):
        raise ValueError("prior piano score lost agreement with its native control")
    off=lambda e:Counter((key[:2],_fingerprint(v)) for key,v in e.items() if key[:2] not in targets)
    if off(raw_original)!=off(raw_corrected):
        raise ValueError("native reprocess changed unrelated note events")

    first=min(targets,key=lambda key:int(key[1]))
    corrected=deepcopy(cooked)
    times=_clocks(cooked,targets)
    for part,number in sorted(targets,key=lambda key:int(key[1])):
        xml_part=corrected.find(f"part[@id='{part}']")
        measure=xml_part.find(f"measure[@number='{number}']")
        division, meter=times[part,number]
        if meter!=4:
            raise ValueError("cooked meter changed")
        previous=[d for d in prior_marks if (d["part"],d["measure"])==(part,number)]
        if any(e.tag not in ("attributes","barline","print","note","direction","backup","forward") for e in measure):
            raise ValueError("unsupported measure annotations; no projection")
        mark_nodes=[]
        discovered=[x for x in expression["wedges"] if (x["part"],x["measure"])==(part,number)]
        prior_wedges={}
        for entry in previous:
            node=entry["node"]
            wedge=node.find("direction-type/wedge")
            if wedge is not None and len(node.findall("direction-type"))==1 and len(node.find("direction-type"))==1:
                kind=wedge.get("type")
                if kind in prior_wedges or entry["staff"]!="1":
                    raise ValueError("multiple existing wedge candidates; no projection")
                prior_wedges[kind]=node
                continue
            tempo=node.find("direction-type/metronome")
            sounding=node.find("sound[@tempo]")
            if (part,number)==first and tempo is not None and sounding is not None and expression["tempo_before_first_attack"]:
                value=sounding.get("tempo")
                global_match=[d for d in prior_marks if d["part"]!=part and d["measure"]==number and d["start"]==0
                                      and d["node"].findtext("direction-type/metronome/beat-unit")==tempo.findtext("beat-unit")
                                      and d["node"].find("sound[@tempo]") is not None
                                      and d["node"].find("sound").get("tempo")==value]
                if len(global_match)!=1:
                    raise ValueError("printed tempo has no matching beat-zero score direction")
                anchored=_direction(node,0,"1")
                mark_nodes.append(anchored)
                continue
            raise ValueError("unhandled expression in projection scope")
        if prior_wedges and set(prior_wedges)!={"crescendo","stop"}:
            raise ValueError("piano wedge pair is incomplete")
        if bool(prior_wedges)!=bool(discovered) or (discovered and {v["staff"] for v in discovered}!={"1","2"}):
            raise ValueError("original and destination wedge scope differs")
        for v in discovered:
            staff=v["staff"]
            first_note=raw_corrected[(part,number,int(v["first_note_index"]))]
            if first_note["node"].findtext("staff")!=staff or not v["stop_at_measure_end"]:
                raise ValueError("source wedge onset or stop invalid")
            for kind,pos in (("crescendo",first_note["start"]),("stop",meter)):
                tick=pos*division
                if tick.denominator!=1:
                    raise ValueError("wedge placement does not fit divisions")
                item=_direction(prior_wedges[kind],int(tick),staff)
                item.find("direction-type/wedge").set("number",staff)
                mark_nodes.append(item)

        start=[deepcopy(n) for n in measure if n.tag in ("print","attributes") or (n.tag=="barline" and n.get("location")=="left")]
        end=[deepcopy(n) for n in measure if n.tag=="barline" and n.get("location")!="left"]
        measure[:]=start+mark_nodes
        for staff in ("1","2"):
            if staff=="2":
                ET.SubElement(ET.SubElement(measure,"backup"),"duration").text=str(int(meter*division))
            at=Fraction()
            last=None
            for key,event in raw_corrected.items():
                n=event["node"]
                if key[:2]!=(part,number) or n.findtext("staff")!=staff:
                    continue
                note=deepcopy(n)
                if note.find("pitch") is None or note.find("duration") is None or note.find("lyric") is not None:
                    raise ValueError("projected piano scope contains a non-pitched event")
                if note.find("chord") is None:
                    proof=source_refs.get(key)
                    if proof is None or proof["staff"]!=staff or event["start"]!=at:
                        raise ValueError("source visual note anchor differs from the expected score")
                    _mark(note,proof["marks"])
                    last=at
                    at+=event["duration"]
                elif event["start"]!=last or key in source_refs:
                    raise ValueError("piano chord follower lost its source anchor")
                else:
                    _mark(note,{})
                value=event["duration"]*division
                if value.denominator!=1:
                    raise ValueError("new rhythm does not fit MusicXML divisions")
                note.find("duration").text=str(int(value))
                measure.append(note)
            if at!=meter:
                raise ValueError("projected staff must independently fill the printed meter")
        measure.extend(end)
    after,directions=_score_events(corrected)
    if off(original)!=off(after) or _signature(_scope(after,targets))!=_signature(_scope(raw_corrected,targets)):
        raise ValueError("projected piano score differs from validated native repair")
    report={"measures":sorted(targets),"note_count":len(_scope(after,targets)),
            "source_sha256":expression["source_sha256"],"expression_count":sum(len(v["marks"]) for v in source_refs.values()),
            "wedges":len(expression["wedges"]),"tempo_before_first_attack":expression["tempo_before_first_attack"]}
    return corrected,report
