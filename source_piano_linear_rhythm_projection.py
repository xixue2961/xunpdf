"""Project the verified piano lane repair into the final MusicXML."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from xml.etree import ElementTree as ET

from source_lyric_roles import _score_events


def fingerprint(v):
    note=v["node"]
    pitch=note.find("pitch")
    return (note.findtext("staff","1"),v["start"],v["duration"],
            tuple(pitch.findtext(k,"0") for k in ("step","alter","octave")) if pitch is not None else None,
            note.find("rest") is not None, note.find("grace") is not None,
            tuple(n.findtext("text") for n in note.findall("lyric")))


def _events_for(events,scope,inside=True):
    return Counter((k[:2],fingerprint(ev)) for k,ev in events.items() if (k[:2]==scope)==inside)


def clocks(root, scope):
    div=1
    meter=None
    part,n=scope
    for bar in root.find(f"part[@id='{part}']").findall("measure"):
        if value:=bar.findtext("attributes/divisions"):
            div=int(value)
        time=bar.find("attributes/time")
        if time is not None:
            p,q=time.findall("beats"),time.findall("beat-type")
            meter=Fraction(int(p[0].text)*4,int(q[0].text)) if len(p)==len(q)==1 else None
        if bar.get("number")==n:
            return div,meter
    raise ValueError("projected measure unavailable")


def project(root, control, repair, ink):
    scope=ink["part"],ink["measure"]
    received, previous_directions=_score_events(root)
    original,_=_score_events(control)
    native,_=_score_events(repair)
    if _events_for(received,scope)!=_events_for(original,scope):
        raise ValueError("cooked piano timing no longer matches valid source control")
    if _events_for(original,scope,False)!=_events_for(native,scope,False):
        raise ValueError("native processing touched note timing beyond source scope")
    div,meter=clocks(root,scope)
    targets=defaultdict(list)
    for r in ink["marks"]:
        targets[int(r["note_index"])].append(r["kind"])
    allowed_notes={i for k in native if k[:2]==scope for i in (k[2],)}
    if not targets.keys()<=allowed_notes or meter!=4:
        raise ValueError("unsupported rhythmic scope")
    then=[d for d in previous_directions if (d["part"],d["measure"])==scope]
    if {d["staff"] for d in then}!={"1","2"} or any(d["start"]!=0 or d["node"].find("direction-type/dynamics") is None for d in then):
        raise ValueError("retiming would move an unrecognized expression")
    result=deepcopy(root)
    part,number=scope
    bar=result.find(f"part[@id='{part}']/measure[@number='{number}']")
    if any(n.tag not in ("attributes","direction","note","backup","forward","barline","print") for n in bar):
        raise ValueError("unsupported measure structure")
    begin=[deepcopy(x) for x in bar if x.tag in ("print","attributes") or (x.tag=="barline" and x.get("location")=="left")]
    end=[deepcopy(x) for x in bar if x.tag=="barline" and x.get("location")!="left"]
    bar[:]=begin+[deepcopy(d["node"]) for d in then]
    for staff in ("1","2"):
        if staff=="2":
            ET.SubElement(ET.SubElement(bar,"backup"),"duration").text=str(4*div)
        at=Fraction()
        for key,value in native.items():
            n=value["node"]
            if key[:2]!=scope or n.findtext("staff")!=staff:
                continue
            copy=deepcopy(n)
            if copy.find("pitch") is None and copy.find("rest") is None or copy.find("duration") is None:
                raise ValueError("unexpected repaired native event")
            if staff=="1":
                marks=copy.findall("notations/articulations/*")
                if Counter(v.tag for v in marks)!=Counter(targets.get(key[2],())):
                    raise ValueError("printed curve proof does not cover every native articulation")
                for note in copy.findall("notations"):
                    for node in note.findall("articulations"):
                        note.remove(node)
                    if not len(note):
                        copy.remove(note)
            if copy.find("chord") is None:
                if at!=value["start"]:
                    raise ValueError("recovered piano staff contains an internal gap or overlap")
                at+=value["duration"]
            elif copy.find("pitch") is None:
                raise ValueError("a rest cannot follow a chord leader")
            ticks=value["duration"]*div
            if ticks.denominator!=1:
                raise ValueError("repair cannot be expressed in existing MusicXML divisions")
            copy.find("duration").text=str(int(ticks))
            bar.append(copy)
        if at!=meter:
            raise ValueError("one of the recovered hands exceeds printed meter")
    bar.extend(end)
    after,directions=_score_events(result)
    if _events_for(received,scope,False)!=_events_for(after,scope,False) or _events_for(native,scope)!=_events_for(after,scope):
        raise ValueError("export projection did not preserve note events")
    active=[e["node"] for key,e in after.items() if key[:2]==scope]
    notes=sum(n.find("pitch") is not None for n in active)
    rests=sum(n.find("rest") is not None for n in active)
    if (notes,rests)!=(30,3):
        raise ValueError("scope does not agree with the complete physical figure")
    return result,{"part":part,"measure":number,"pitches":notes,"rests":rests,"quarters_each_staff":4,
                   "removed_curve_fragments":len(ink["marks"])}
