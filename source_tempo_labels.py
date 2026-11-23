"""Keep verified metronome values separate from instructions and recover duplicate anchors."""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import base64
import csv
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_vocal_breath_column import rows


def read_ledger(path):
    with path.open(encoding="utf8",newline="") as f:
        return list(csv.DictReader(f,delimiter="\t"))


def recover(root,job):
    job=Path(job)
    collected=(job/"source-tempo-native/apply/tempo-ledger.tsv",job/"source-tempo-evidence/scan.json")
    if not all(p.is_file() for p in (*collected,job/"source-native-notation.json")):
        return root,dict(state="not_applicable",changes=[])
    tempo_image, scan=collected
    source_findings=json.loads(scan.read_text("utf8"))["findings"]
    source={(str(f["unit"]),str(f["system"]),str(f["id"])):f for f in source_findings
            if f["state"]=="source_symbol_proposed" and "bounds" in f}
    ledger=read_ledger(tempo_image)
    meta=json.loads((job/"source-native-notation.json").read_text("utf8"))
    if not meta.get("accepted_native_sha256") or sha(Path(meta["accepted_native_book"]))!=meta["accepted_native_sha256"]:
        return root,dict(state="not_applicable",changes=[])
    notes=defaultdict(list)
    for n in rows(Path(meta["accepted_native_xml"]).with_name("note-ledger.tsv")):
        if n["shape"].startswith("NOTEHEAD") or n["shape"]=="WHOLE_NOTE":
            notes[n["part"],n["measure"],n["staff_number"]].append(n)
    cloned=deepcopy(root)
    events,directions=_score_events(cloned)
    matched=defaultdict(list)
    signatures={}
    for record in ledger:
        if record["beat_unit"]!="quarter" or record["dots"]!="0":
            continue
        address=(record["unit"],record["system"],record["metro"])
        finding=source.get(address)
        if not finding or finding["source_sha256"]!=record["source_sha256"]:
            continue
        path=job/f"unit-{int(record['unit']):03d}.png"
        if path not in signatures:
            signatures[path]=sha(path)
        if signatures[path]!=record["source_sha256"]:
            continue
        identity=(record["part"],record["measure"],record["staff"] or "1")
        original=base64.b64decode(record["per_minute_b64"],validate=True).decode("utf8")
        compatible=[]
        for d in directions:
            if (d["part"],d["measure"],d["staff"])!=identity:
                continue
            n=d["node"]
            if (n.findtext("direction-type/metronome/beat-unit")=="quarter"
                    and n.findtext("direction-type/metronome/per-minute")==original
                    and n.find("sound") is not None and n.find("sound").get("tempo")==record["qpm"]):
                compatible.append(d)
        if len(compatible)==1:
            key=(record["unit"],record["system"],record["measure"],record["qpm"],original,record["source_sha256"])
            matched[key].append((record,finding,compatible[0]))
    changes=[]
    for group in matched.values():
        for row,finding,d in group:
            text=base64.b64decode(row["per_minute_b64"],validate=True).decode("utf8")
            mark=d["node"]
            metro=mark.find("direction-type/metronome")
            signature=re.fullmatch(r"([0-9]{2,3})\s*([㐀-鿿]{2,16})",text)
            edit=dict(part=row["part"],measure=row["measure"],source=row["metro"])
            if d["start"]>0 and mark.find("offset") is None:
                original_x=finding["bounds"][0]
                peer=[other for p,f,other in group if p["part"]!=row["part"] and other["start"]==0 and abs(f["bounds"][0]-original_x)<=3]
                pid,mid,staff=row["part"],row["measure"],row["staff"] or "1"
                visual=[]
                for n in notes[pid,mid,staff]:
                    if (n["unit"],n["system"],n["source_sha256"])!=(row["unit"],row["system"],row["source_sha256"]):
                        continue
                    v=events.get((pid,mid,int(n["note_index"])))
                    if v is not None and v["node"].find("pitch") is not None and v["node"].findtext("staff","1")==staff:
                        visual.append((int(n["x"]),v))
                earliest=[v for x,v in visual if v["start"]==0 and v["node"].find("chord") is None and abs(x-original_x)<=10]
                late=[v for x,v in visual if v["start"]==d["start"] and v["node"].find("chord") is None and x>original_x+35]
                beam=metro.get("relative-x")
                if peer and len(earliest)==len(late)==1 and beam is not None:
                    start,next_note=earliest[0],late[0]
                    m=start["measure"]
                    a=start["node"].get("default-x")
                    z=next_note["node"].get("default-x")
                    if mark in m and a is not None and z is not None:
                        metro.set("relative-x",str(Decimal(beam)+Decimal(z)-Decimal(a)))
                        m.remove(mark)
                        m.insert(list(m).index(start["node"]),mark)
                        edit["moved_from_quarter"]=str(d["start"])
                        edit["moved_to_quarter"]="0"
            if signature and Decimal(signature[1])==Decimal(row["qpm"]) and mark.find("direction-type/words") is None:
                metro.find("per-minute").text=signature[1]
                payload=ET.Element("direction-type")
                style={k:v for k,v in metro.attrib.items() if k=="default-y"}
                if metro.get("relative-x") is not None:
                    style["relative-x"]=str(Decimal(metro.get("relative-x"))+48)
                ET.SubElement(payload,"words",style).text=signature[2]
                mark.insert(list(mark).index(mark.find("direction-type"))+1,payload)
                edit["expression"]=signature[2]
            if "expression" in edit or "moved_from_quarter" in edit:
                changes.append(edit)
    if not changes:
        return root,dict(state="not_applicable",changes=[])
    return cloned,dict(state="restored",changes=changes)
