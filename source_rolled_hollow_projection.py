
"""Project a source-proven rolled half-note chord without changing its clock."""
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from source_lyric_roles import _score_events
from source_native_notation import score


def pitch(note):
    return tuple(note.findtext("pitch/"+field, "0") for field in ("step","alter","octave"))


def lower_pitch(note, diatonic_steps, fifths):
    order="CDEFGAB"
    step,_,octave=pitch(note)
    octave,index=divmod(7*int(octave)+order.index(step)-diatonic_steps,7)
    step=order[index]
    alter=(1 if step in "FCGDAEB"[:fifths] else 0) if fifths>=0 else (-1 if step in "BEADGCF"[:-fifths] else 0)
    result=ET.Element("pitch")
    ET.SubElement(result,"step").text=step
    if alter:
        ET.SubElement(result,"alter").text=str(alter)
    ET.SubElement(result,"octave").text=str(octave)
    return result


def measure_key(part, target, staff):
    fifths=None
    for measure in part.findall("measure"):
        for key in measure.findall("attributes/key"):
            if key.get("number",staff)==staff:
                text=key.findtext("fifths")
                fifths=int(text) if text is not None else None
        if measure is target:
            return fifths
    return None


def is_dotted_half(event):
    note=event["node"]
    return (event["duration"]==3 and note.findtext("type")=="half" and len(note.findall("dot"))==1
            and note.findtext("stem")=="down" and note.find("tie") is None
            and note.find("time-modification") is None)


def recover(root, native_score, evidence):
    if not evidence["findings"]:
        return root, {"changes":[]}
    control,_=_score_events(score(Path(native_score)))
    proposal=deepcopy(root)
    output,_=_score_events(proposal)
    changes=[]
    for source in evidence["findings"]:
        rows=sorted(source["rows"],key=lambda row:int(row["note_index"]))
        first,second=rows
        part_id,number,staff=first["part"],first["measure"],first["staff_number"]
        old=[control.get((r["part"],r["measure"],int(r["note_index"]))) for r in rows]
        if not all(e and is_dotted_half(e) for e in old):
            continue
        old_pitches=tuple(pitch(e["node"]) for e in old)
        candidates=[e for (pid,mid,_),e in output.items()
                    if pid==part_id and mid==number and e["start"]==0 and e["node"].findtext("staff")==staff]
        if len(candidates)!=2 or tuple(pitch(e["node"]) for e in candidates)!=old_pitches:
            continue
        if not all(is_dotted_half(e) for e in candidates):
            continue
        leader,follower=(e["node"] for e in candidates)
        if (leader.find("chord") is not None or follower.find("chord") is None
                or leader.findtext("voice")!=follower.findtext("voice")
                or any(n.find("notations/arpeggiate") is not None for n in (leader,follower))):
            continue
        part=proposal.find(f"part[@id='{part_id}']")
        measure=part.find(f"measure[@number='{number}']")
        fifths=measure_key(part,measure,staff)
        if fifths is None or not -7<=fifths<=7:
            continue
        index=list(measure).index(leader)
        if list(measure)[index+1] is not follower:
            continue
        new=[]
        for i,steps in enumerate((4,2)):
            note=ET.Element("note")
            if i:
                ET.SubElement(note,"chord")
            note.append(lower_pitch(leader,steps,fifths))
            for attr in ("duration","voice","type","dot","stem","staff"):
                elem=leader.find(attr)
                if elem is not None:
                    note.append(deepcopy(elem))
            measure.insert(index+i,note)
            new.append(note)
        leader.insert(0,ET.Element("chord"))
        for node in (*new,leader,follower):
            notation=node.find("notations")
            if notation is None:
                notation=ET.Element("notations")
                position=next((i for i,c in enumerate(node) if c.tag in {"lyric","play","listen"}),len(node))
                node.insert(position,notation)
            ET.SubElement(notation,"arpeggiate",number=staff)
        changes.append(dict(part=part_id,measure=number,staff=staff,new_pitches=[pitch(n) for n in new],
                            source_sha256=source["source_sha256"],chord=source["chord"]))
    return (proposal if changes else root),{"changes":changes}

