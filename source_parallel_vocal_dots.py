"""Recover a beamed dotted eighth from source ink and simultaneous vocal peers."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image
from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import signature
from source_vocal_breath_column import rows
from source_vocal_consensus_rhythm import dot_pixels, melodic_lanes
from source_vocal_split_clock import native_anchor


def recover(root,job):
    job=Path(job)
    meta=job/"source-native-notation.json"
    if not meta.is_file():
        return root,dict(state="not_applicable",changes=[])
    a=json.loads(meta.read_text("utf8"))
    book,xml=(Path(a[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if not a.get("accepted_native_sha256") or sha(book)!=a["accepted_native_sha256"]:
        return root,dict(state="not_applicable",changes=[])
    body=deepcopy(root)
    live,ds=_score_events(body)
    native=_score_events(score(xml))[0]
    by_note={(r["part"],r["measure"],int(r["note_index"])):r for r in rows(xml.with_name("note-ledger.tsv"))
             if r["shape"]=="NOTEHEAD_BLACK"}
    found=defaultdict(list)
    for lane in melodic_lanes(body):
        if all(s is not None for s in lane["text"]):
            found[lane["number"],lane["text"]].append(lane)
    trouble={(d["part"],d["measure"]) for d in ds if d["start"]>0}
    changes=[]
    with ZipFile(book) as z:
        for (bar,_),group in found.items():
            for lane in group:
                pid=lane["part"]
                if sum(lane["rhythm"])!=F(15,4) or (pid,bar) in trouble or lane["divisions"]%4:
                    continue
                count=len(lane["leaders"])
                if len(lane["notes"])!=count or len({n.findtext("voice","1") for _,n in lane["leaders"]})!=1:
                    continue
                # A missing dot has a duration of one eighth, and a following
                # sixteenth is the second member of the same beam.
                i=count-2
                n=lane["leaders"][i][1]
                tail=lane["leaders"][-1][1]
                if (lane["rhythm"][-2:]!=(F(1,2),F(1,4)) or n.find("dot") is not None 
                    or n.find("tie") is not None or tail.find("tie") is not None
                    or n.findtext("type")!="eighth" or tail.findtext("type")!="16th"
                    or n.findtext("beam[@number='1']")!="begin" or tail.findtext("beam[@number='1']")!="end"):
                    continue
                keys=[(pid,bar,v) for v in (i,i+1)]
                if any(k not in by_note or k not in live or k not in native
                       or signature(live[k])!=signature(native[k])
                       or live[k]["node"].get("default-x")!=native[k]["node"].get("default-x") for k in keys):
                    continue
                first,last=(by_note[k] for k in keys)
                support=[]
                for peer in group:
                    if (peer is lane or len(peer["notes"])!=count
                            or peer["rhythm"][:-2]!=lane["rhythm"][:-2]
                            or peer["rhythm"][-2:]!=(F(3,4),F(1,4))
                            or peer["leaders"][i][1].find("dot") is None):
                        continue
                    other=[by_note.get((peer["part"],bar,j)) for j in (i,i+1)]
                    if all(r is not None for r in other) and all(
                        (r["unit"],r["system"],r["source_sha256"])==(t["unit"],t["system"],t["source_sha256"])
                        and abs(int(r["x"])-int(t["x"]))<=4 for r,t in zip(other,(first,last))):
                        support.append(peer["part"])
                if len(set(support))<2:
                    continue
                unit,system=first["unit"],first["system"]
                g=graph(z,unit,system)
                if g is None or not all(native_anchor(r,with_bounds(g)) for r in (first,last)):
                    continue
                page=job/f"unit-{int(unit):03d}.png"
                if sha(page)!=first["source_sha256"]:
                    continue
                with Image.open(page) as origin:
                    bounds=dot_pixels(origin,first)
                if not bounds:
                    continue
                n.insert(list(n).index(n.find("type"))+1,ET.Element("dot"))
                n.find("duration").text=str(lane["divisions"]*3//4)
                changes.append(dict(part=pid,measure=bar,index=i,paired_parts=sorted(set(support)),
                                    onset="3",duration="3/4",image_sha256=first["source_sha256"],boxes=bounds))
    return (body,dict(state="restored",changes=changes)) if changes else (root,dict(state="not_applicable",changes=[]))
