"""Remove a lyric transcription only when it shares its source ink with a real flat."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import csv
import hashlib
import re

from PIL import Image
import numpy as np

from source_lyric_roles import _score_events, _music_signature
from source_native_notation import score


def bounds(node):
    return tuple(int(node.find("bounds").get(k)) for k in "xywh")


def matches_ink(image,text,flat):
    x,y,w,h=bounds(text)
    ax,ay,aw,ah=bounds(flat)
    patch=image[y:y+h,x:x+w]
    if patch.shape!=(h,w) or patch.size==0:
        return None
    coverage=[]
    for threshold in (160,208):
        yy,xx=np.nonzero(patch<threshold)
        if len(xx)<12:
            return None
        inside=(ax<=xx+x)&(xx+x<ax+aw)&(ay<=yy+y)&(yy+y<ay+ah)
        coverage.append(float(inside.mean()))
    return coverage if min(coverage)>=.98 else None


def restore_flat_roles(root, control_dir, source_dir):
    control_dir,source_dir=map(Path,(control_dir,source_dir))
    with (control_dir/"head-ledger.tsv").open(encoding="utf8",newline="") as stream:
        ledger=defaultdict(list)
        for row in csv.DictReader(stream,delimiter="\t"):
            if row["repeat_copy"]=="false":
                ledger[row["unit"],row["system"],row["head"]].append(row)
    original,_=_score_events(score(control_dir/"result.mxl"))
    output=deepcopy(root)
    events,_=_score_events(output)
    reports=[]

    def event(row):
        address=row["part"],row["measure"],int(row["note_index"])
        native=original.get(address)
        candidate=events.get(address)
        if native is None or candidate is None or _music_signature(native)!=_music_signature(candidate):
            return None
        return candidate

    with ZipFile(control_dir/"refined.omr") as archive:
        for name in archive.namelist():
            match=re.fullmatch(r"sheet#([1-9][0-9]*)/sheet#\1\.xml",name)
            if not match:continue
            unit=match[1]
            image=None
            for system in ET.fromstring(archive.read(name)).findall("page/system"):
                sid=system.get("id")
                ids={n.get("id"):n for n in system.findall("sig/inters/*")}
                edges=system.findall("sig/relations/relation")
                candidates=[r for r in edges if r[0].tag=="chord-syllable"
                    and ids[r.get("target")].tag=="lyric-item" and ids[r.get("target")].get("value")=="b"]
                flat_relations=[r for r in edges if r[0].tag=="alter-head"
                    and ids[r.get("source")].get("shape")=="FLAT"]
                for relation in candidates:
                    lid=relation.get("target")
                    text=ids[lid]
                    chord=relation.get("source")
                    lyric_heads=[r.get("target") for r in edges if r.get("source")==chord and r[0].tag=="containment"]
                    if len(lyric_heads)!=1:
                        continue
                    owners=ledger[unit,sid,lyric_heads[0]]
                    if len(owners)!=1:
                        continue
                    own=owners[0]
                    vocal=event(own)
                    if vocal is None:
                        continue
                    lyrics=[v for v in vocal["node"].findall("lyric") if v.findtext("text")=="b"
                            and v.findtext("syllabic","single")=="single"]
                    if len(lyrics)!=1:
                        continue
                    matched=[]
                    for edge in flat_relations:
                        flat=ids[edge.get("source")]
                        rhead=edge.get("target")
                        their=ledger[unit,sid,rhead]
                        if len(their)!=1 or their[0]["part"]==own["part"] or int(flat.get("staff"))!=int(text.get("staff"))+1:
                            continue
                        pitch=event(their[0])
                        if pitch is None or pitch["node"].findtext("pitch/alter")!="-1":
                            continue
                        tx,ty,tw,th=bounds(text)
                        ax,ay,aw,ah=bounds(flat)
                        if min(tx+tw,ax+aw)<=max(tx,ax) or min(ty+th,ay+ah)<=max(ty,ay):
                            continue
                        if image is None:
                            path=source_dir/f"unit-{int(unit):03d}.png"
                            source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
                            with Image.open(path) as data:image=np.asarray(data.convert("L"))
                        if any(row["source_sha256"]!=source_hash for row in (own,their[0])):
                            raise ValueError("accidental source image changed")
                        support=matches_ink(image,text,flat)
                        if support is not None:
                            matched.append((edge.get("source"),support))
                    if len(matched)!=1:
                        continue
                    vocal["node"].remove(lyrics[0])
                    reports.append(dict(part=own["part"],measure=own["measure"],note_index=int(own["note_index"]),
                        source_lyric=lid,unchanged_flat=matched[0][0],ink_coverage=matched[0][1],source_sha256=source_hash))
    return (output if reports else root),reports

