
"""Restore source-confirmed markings on the intended grand staff."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from source_expression_overlay import events
from source_lyric_roles import _score_events
from source_native_notation import score
from source_wedge_integrity import audit_wedge_integrity


def mark(event):
    node=event["node"]
    return (event["start"], event["duration"],
            tuple(node.findtext(k) for k in ("staff","type","pitch/step","pitch/alter","pitch/octave")))


def put_word(case,native,export,directions):
    row=case["row"]
    address=row["part"],row["measure"],int(row["note_index"])
    control=native.get(address)
    if control is None:
        return None
    matches=[e for k,e in export.items() if k[:2]==address[:2] and mark(e)==mark(control)
             and e["node"].findtext("beam")=="begin"]
    if len(matches)!=1 or any(d["part"]==address[0] and d["measure"]==address[1]
                              and (d["node"].findtext("direction-type/words") or "").strip().lower()=="rit."
                              for d in directions):
        return None
    [event]=matches
    bar=event["measure"]
    node=ET.Element("direction",placement="above")
    ET.SubElement(ET.SubElement(node,"direction-type"),"words").text=case["text"]
    ET.SubElement(node,"staff").text=row["staff_number"]
    bar.insert(list(bar).index(event["node"]),node)
    return dict(part=address[0],measure=address[1],staff=row["staff_number"],start=str(event["start"]),text=case["text"])


def wedge_list(directions,key):
    found=[]
    for d in directions:
        if (d["part"],d["measure"])!=key:
            continue
        if (w:=d["node"].find("direction-type/wedge")) is not None:
            found.append((d,w))
    return found


def wedge_pair(pair,kind,staff,placement):
    if len(pair)!=2:
        return False
    (first,start),(last,stop)=pair
    return (start.get("type")==kind and stop.get("type")=="stop"
            and start.get("number","1")==stop.get("number","1") and first["staff"]==last["staff"]==staff
            and first["node"].get("placement")==placement and first["start"]<last["start"]
            and all(set(n.tag for n in d["node"])<={"direction-type","staff","offset"}
                    and len(d["node"].findall("direction-type"))==len(d["node"].find("direction-type"))==1
                    for d,w in pair))


def allocate(part, retired):
    retired={e["node"] for e,w in retired}
    numbers={w.get("number","1") for m in part.findall("measure") for n in m.findall("direction")
             if n not in retired for w in n.findall("direction-type/wedge")}
    return [str(i) for i in range(1,17) if str(i) not in numbers][:2]


def make_wedge(staff,number,kind,spread,placement=False):
    node=ET.Element("direction",placement="below") if placement else ET.Element("direction")
    ET.SubElement(ET.SubElement(node,"direction-type"),"wedge",
                  type=kind,number=number,spread=str(spread))
    ET.SubElement(node,"staff").text=staff
    return node


def put_wedges(case,root,control,export,directions,bars):
    key=case["part"],case["measure"]
    info=bars.get(key)
    if info is None or info["meter"] is None or info["meter"]!=info["end_cursor"]:
        return None
    meter=info["meter"]
    for staff in ("1","2"):
        ns=[v for k,v in export.items() if k[:2]==key and v["node"].findtext("staff")==staff]
        if not ns or min(v["start"] for v in ns)!=0 or max(v["start"]+v["duration"] for v in ns)!=meter:
            return None
        anchor=[control.get((*key,int(r["note_index"]))) for r in case["anchor_rows"]
                if r["staff_number"]==staff]
        sig={mark(v)[2] for v in anchor if v and v["start"]==0 and v["node"].find("pitch") is not None}
        if not sig or not sig<={mark(v)[2] for v in ns if v["start"]==0}:
            return None
    part=root.find(f"part[@id='{key[0]}']")
    bar=info["node"]
    found=wedge_list(directions,key)
    used=defaultdict(list)
    for item in found:
        used[item[1].get("number","1")].append(item)
    pairs=list(used.values())
    kind=case["kind"]
    normal=lambda p,staff: wedge_pair(p,kind,staff,"below") and p[0][0]["start"]==0
    if len(pairs)==1 and normal(pairs[0],"1"):
        reason="missing_lower_staff"
    elif (len(pairs)==2 and sum(normal(p,"2") for p in pairs)==1
          and sum(wedge_pair(p,kind,"2","above") for p in pairs)==1
          and pairs[0][1][0]["start"]==pairs[1][1][0]["start"]):
        reason="upper_staff_linked_to_lower"
    else:
        return None
    if any(e["start"]>meter for e,w in found):
        return None
    numbers=allocate(part,found)
    if len(numbers)!=2:
        return None
    for e,w in found:
        bar.remove(e["node"])
    first=next(i for i,n in enumerate(bar) if n.tag in {"note","backup","forward"})
    opened=case["spread_tenths"]
    for offset,(staff,number) in enumerate(zip(("1","2"),numbers)):
        bar.insert(first+offset,make_wedge(staff,number,kind,0 if kind=="crescendo" else opened,True))
    end=next((i for i,n in enumerate(bar)
              if n.tag=="barline" and n.get("location","right")=="right"),len(bar))
    for offset,(staff,number) in enumerate(zip(("1","2"),numbers)):
        bar.insert(end+offset,make_wedge(staff,number,"stop",opened if kind=="crescendo" else 0))
    return dict(part=key[0],measure=key[1],start="0",end=str(meter),native_ids=case["native_ids"],
                kind=kind,reason=reason)


def restore(root,source,evidence):
    if not evidence["words"] and not evidence["wedges"]:
        return root,dict(words=[],wedges=[])
    native,_=_score_events(score(Path(source)))
    new=deepcopy(root)
    out,directions=_score_events(new)
    _,bars=events(new)
    words=[change for case in evidence["words"] if (change:=put_word(case,native,out,directions))]
    wedges=[change for case in evidence["wedges"] if (change:=put_wedges(case,new,native,out,directions,bars))]
    if audit_wedge_integrity(root)["export_issues"]!=audit_wedge_integrity(new)["export_issues"]:
        raise ValueError("source markings created an unmatched or overlapping wedge")
    return (new if words or wedges else root),dict(words=words,wedges=wedges)

