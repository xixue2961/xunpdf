"""Restore common SATB timing only when source ink and independent voices agree."""
from copy import deepcopy
from fractions import Fraction as F
from xml.etree import ElementTree as ET
from source_lyric_roles import _score_events

TIMINGS=(F(1),F(1),F(1,2),F(1,2),F(3,4),F(1,4))
ONSETS=(F(0),F(1),F(2),F(5,2),F(3),F(15,4))

def _notegroup(root,pid,number):
    p=root.find(f"part[@id='{pid}']")
    div=1
    while_measure=None
    for m in p.findall("measure"):
        if ticks:=m.findtext("attributes/divisions"):
            div=int(ticks)
        if m.get("number")==number:
            while_measure=m
            break
    if while_measure is None:
        raise ValueError("source vocal part or measure not present")
    notes=while_measure.findall("note")
    if any(n.find("pitch") is None or n.find("chord") is not None or n.find("grace") is not None
           or n.find("tie") is not None or n.find("time-modification") is not None
           or n.find("notations") is not None or n.find("accidental") is not None for n in notes):
        raise ValueError("vocal part is no longer the verified independent line")
    backups=while_measure.findall("backup")
    if while_measure.findall("forward") or (backups and not (
            len(backups)==1 and while_measure[-1] is backups[0] and len(notes)==5
            and int(backups[0].findtext("duration"))==sum(int(n.findtext("duration")) for n in notes))):
        raise ValueError("parallel voice position changes need independent evidence")
    return while_measure,notes,div

def _time(n,value,div):
    number=value*div
    if number.denominator!=1:
        raise ValueError("confirmed vocal rhythm cannot fit current divisions")
    n.find("duration").text=str(int(number))

def _lyric(nodes):
    values=[]
    for n in nodes:
        if len(n.findall("lyric"))!=1:
            raise ValueError("lyric requires a unique syllable")
        s=n.findtext("lyric/text","")
        if len(s)!=1 or not "\u3400"<=s<="\u9fff":
            raise ValueError("lyric is not a unique printed Han character")
        values.append(s)
    return values

def _beam(n,number,kind):
    if n.find(f"beam[@number='{number}']") is not None:
        raise ValueError("unexpected old beam")
    where=next((i for i,c in enumerate(n) if c.tag in ("notations","lyric","play")),len(n))
    beam=ET.Element("beam",number=str(number))
    beam.text=kind
    n.insert(where,beam)

def _ordered_pitch(n):
    return tuple(n.findtext("pitch/"+tag,"0") for tag in ("step","alter","octave"))

def restore(root,evidence):
    number=evidence["measure"]
    parts=evidence["parts"]
    if len(parts)!=4 or len(set(parts))!=4:
        raise ValueError("four independent aligned voices required")
    top,mid_a,mid_b,bottom=parts
    result=deepcopy(root)
    top_bar,upper,updiv=_notegroup(result,top,number)
    _,lower,downdiv=_notegroup(result,bottom,number)
    _,guide,guide_div=_notegroup(result,mid_a,number)
    _,peer,peer_div=_notegroup(result,mid_b,number)
    def times(notes,division):
        return tuple(F(n.findtext("duration"))/division for n in notes)

    if len(upper)!=5 or times(guide,guide_div)!=TIMINGS or times(peer,peer_div)!=TIMINGS:
        raise ValueError("two independently aligned reference voices are required")
    sequence=_lyric(guide)
    if (_lyric(peer)!=sequence or _lyric(lower)!=sequence or _lyric(upper)!=sequence[:-1]
            or sequence[-1]!=evidence["source_lyric_character"]):
        raise ValueError("cooked lyric and source word positions disagree")
    if times(upper,updiv)!=(*TIMINGS[:4],F(3,2)) or times(lower,downdiv)!=(*TIMINGS[:4],F(1,2),F(1,4)):
        raise ValueError("none of the two previously proved vocal rhythm errors found")
    if (upper[-1].findtext("type")!="quarter" or len(upper[-1].findall("dot"))!=1
            or lower[-2].findtext("type")!="eighth" or lower[-2].find("dot") is not None):
        raise ValueError("vocal duration type has changed")
    marks=evidence["neighbor_indices"]
    neighbors=[upper[int(i)] for i in marks]
    if len(neighbors)<2 or len({_ordered_pitch(n) for n in neighbors})!=1:
        raise ValueError("missing note pitch does not agree with independent same-staff heads")
    if any(len(n.findall("pitch"))!=1 for n in neighbors):
        raise ValueError("cannot establish one original note pitch")

    upper[-1].find("type").text="eighth"
    _time(upper[-1],TIMINGS[-2],updiv)
    _beam(upper[-1],1,"begin")
    added=deepcopy(guide[-1])
    added.remove(added.find("pitch"))
    added.insert(0,deepcopy(neighbors[0].find("pitch")))
    added.find("voice").text=upper[-1].findtext("voice")
    _time(added,TIMINGS[-1],updiv)
    top_bar.insert(list(top_bar).index(upper[-1])+1,added)
    tail=top_bar.find('backup')
    if tail is not None:
        top_bar.remove(tail)
    _time(lower[-2],TIMINGS[-2],downdiv)
    n=lower[-2]
    n.insert(list(n).index(n.find("type"))+1,ET.Element("dot"))

    before,_=_score_events(root)
    after,_=_score_events(result)
    outside={(p,measure,i) for (p,measure,i) in before if not (measure==number and p in (top,bottom))}
    if any((before[k]["start"],before[k]["duration"],ET.tostring(before[k]["node"]))!=
           (after[k]["start"],after[k]["duration"],ET.tostring(after[k]["node"])) for k in outside):
        raise ValueError("an unrelated note was modified")
    for p in parts:
        active=[v for k,v in after.items() if k[:2]==(p,number)]
        if tuple(v["start"] for v in active)!=ONSETS or _lyric([e["node"] for e in active])!=sequence:
            raise ValueError("restored lyric syllables are not all on the verified rhythm")
    return result,{"measure":number,"restored_vocal_note_part":top,"restored_dot_part":bottom,
                   "source_head":evidence["missing_note"],"recovered_lyric":sequence[-1],"aligned_voices":len(parts)}
