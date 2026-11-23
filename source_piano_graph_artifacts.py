"""Retire gray-print artifacts attached to a proven misread barline."""
from collections import defaultdict
from pathlib import Path
import json
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image
from source_void_heads import remaining_ink, sha


def nodes_of(system):
    return {n.get("id"):n for n in system.findall("sig/inters/*")}


def bounds(node):
    return [int(node.find("bounds").get(k)) for k in ("x","y","w","h")]


def evidence(system, current, image):
    nodes=nodes_of(system)
    retained=nodes_of(current)
    rel=system.findall("sig/relations/relation")
    new=current.findall("sig/relations/relation")
    surviving={(r.get("source"),r.get("target")) for r in new if r.find("beam-stem") is not None}
    pairs=defaultdict(set)
    stem_heads=defaultdict(set)
    head_parent=defaultdict(set)
    chord_heads=defaultdict(set)
    for edge in rel:
        a,b=edge.get("source"),edge.get("target")
        if edge.find("beam-stem") is not None:pairs[a].add(b)
        if edge.find("head-stem") is not None:stem_heads[b].add(a)
        if edge.find("containment") is not None and nodes[a].tag=="head-chord":
            head_parent[b].add(a)
            chord_heads[a].add(b)
    for beam,stems in pairs.items():
        if len(stems)!=2 or sum((beam,s) in surviving for s in stems)!=1:
            continue
        if any(len(stem_heads[s])!=1 for s in stems):
            continue
        heads=set().union(*(stem_heads[s] for s in stems))
        if len(heads)!=2 or len({nodes[h].get("staff") for h in heads})!=1:
            continue
        sid=nodes[next(iter(heads))].get("staff")
        staff=next((s for part in system.findall("part") if len(part.findall("staff"))==2
                    for s in part.findall("staff") if s.get("id")==sid),None)
        if staff is None:
            continue
        chords=set().union(*(head_parent[h] for h in heads))
        if len(chords)!=2 or any(len(chord_heads[c])!=1 for c in chords) or            any(h not in retained or bounds(nodes[h])!=bounds(retained[h]) for h in heads):
            continue
        # A slur linking only two false heads may be retired with them, after
        # independently verifying that its ink is also gray.
        slurs={q.get("source") for q in rel if q.find("slur-head") is not None and q.get("target") in heads}
        if len(slurs)!=1:
            continue
        slur=next(iter(slurs))
        if {q.get("target") for q in rel if q.get("source")==slur and q.find("slur-head") is not None}!=heads:
            continue
        targets=heads|chords|slurs
        if not targets<=retained.keys() or any(ET.tostring(nodes[v])!=ET.tostring(retained[v]) for v in heads|slurs):
            continue
        if any(r.get("source") in targets or r.get("target") in targets for r in rel
               if not set(c.tag for c in r)<={"head-stem","chord-stem","containment","slur-head"}):
            continue
        lines=[dict(thickness=float(line.get("thickness")),
                    points=[(float(p.get("x")),float(p.get("y"))) for p in line.findall("point")])
               for line in staff.findall("lines/line")]
        if len(lines)!=5:
            continue
        metrics={}
        for hid in sorted(heads|slurs):
            attached=[]
            for stem in stems:
                if hid not in stem_heads[stem]:
                    continue
                node=nodes[stem]
                a,b=node.find("median/p1"),node.find("median/p2")
                attached.append(dict(width=float(node.get("width")),median=[float(a.get("x")),float(a.get("y")),
                                 float(b.get("x")),float(b.get("y"))]))
            metrics[hid]=[(t,remaining_ink(image,bounds(nodes[hid]),lines,attached,t)["ink"])
                          for t in (96,130,155,185)]
        if not all(all(count==0 for _,count in ms[:3]) and ms[-1][1]>=100 for ms in metrics.values()):
            continue
        yield dict(heads=sorted(heads),chords=sorted(chords),slur=slur,beam=beam,staff=sid,gray_ink=metrics)


def revise(original, selected, source, output, proof_path):
    initial=sha(selected)
    original_hash=sha(original)
    proof=[]
    with ZipFile(original) as old, ZipFile(selected) as new, ZipFile(output,"x") as result:
        for info in new.infolist():
            data=new.read(info)
            if info.filename.startswith("sheet#") and info.filename.endswith(".xml"):
                unit=int(info.filename.split("/")[0][6:])
                page=ET.fromstring(data)
                prior=ET.fromstring(old.read(info.filename))
                image_path=source/f"unit-{unit:03}.png"
                image=np.asarray(Image.open(image_path).convert("L"))
                changed=False
                for system in page.findall("page/system"):
                    sid=system.get("id")
                    before=prior.find(f"page/system[@id='{sid}']")
                    for group in evidence(before,system,image):
                        removed=set(group["heads"]+group["chords"]+[group["slur"]])
                        sig=system.find("sig/inters")
                        connections=system.find("sig/relations")
                        for node in list(sig):
                            if node.get("id") in removed:
                                sig.remove(node)
                        for edge in list(connections):
                            if edge.get("source") in removed or edge.get("target") in removed:
                                connections.remove(edge)
                        for wrapper in system.findall("part/staff/notes")+system.findall("part/measure/head-chords"):
                            if wrapper.text:
                                wrapper.text=" ".join(v for v in wrapper.text.split() if v not in removed)
                        proof.append(dict(unit=unit,system=sid,source_sha256=sha(image_path),**group))
                        changed=True
                if changed:
                    data=ET.tostring(page,encoding="utf-8",xml_declaration=True)
            result.writestr(info,data)
    if sha(selected)!=initial or sha(original)!=original_hash:
        raise ValueError("Untouched recognition input changed")
    with proof_path.open("x",encoding="utf8") as stream:
        json.dump(proof,stream,indent=2)
    print([(x["unit"],x["system"],x["heads"]) for x in proof])


if __name__=="__main__":
    revise(*map(Path,sys.argv[1:]))
