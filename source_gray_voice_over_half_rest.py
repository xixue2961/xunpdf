'''Remove a fully unsupported lower-staff voice printed only in a gray overlay.'''
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_parallel_rest_gap import graph, with_bounds
from source_vocal_split_clock import coordinate, native_anchor
from source_void_heads import remaining_ink, sha
from source_piano_control import read_control


def clocks(measure):
    return [n for n in measure if n.tag in ("note","backup","forward")]


def source_ink(image,node,lines):
    return tuple(remaining_ink(image,coordinate(node),lines,[],level)["ink"]
                 for level in (96,130,155,185))


def layout(measure):
    items=clocks(measure)
    if len(items)<8 or [node.tag for node in items[-4:]]!=["backup","forward","note","note"]:
        return None
    backup,advance,first,last=items[-4:]
    if not all(n.find("pitch") is not None and n.findtext("type")=="quarter"
               and n.findtext("staff")=="2" and not n.findall("notations")
               and set(k.tag for k in n)<= {"chord","pitch","duration","voice","type","stem","staff"}
               for n in (first,last)):
        return None
    if first.find("chord") is not None or last.find("chord") is None:
        return None
    voice=first.findtext("voice")
    if not voice or any(n.findtext("voice")!=voice for n in (last,advance)):
        return None
    full=int(backup.findtext("duration"))
    voice_start=int(advance.findtext("duration"))
    if full!=4*int(first.findtext("duration")) or voice_start!=full*3//4 or full-voice_start!=int(last.findtext("duration")):
        return None
    if any(n.findtext("voice")==voice for n in items[:-4]):
        return None
    rest=items[-5]
    if (rest.tag!="note" or rest.find("rest") is None or rest.findtext("type")!="half" or rest.findtext("staff")!="2"
            or int(rest.findtext("duration"))*2!=full or rest.findtext("voice")==voice):
        return None
    others=[n for n in items[:-5] if n.tag=="note" and n.findtext("staff")=="2"]
    if not others or any(n.findtext("voice")!=rest.findtext("voice") for n in others):
        return None
    if sum(int(n.findtext("duration")) for n in others if n.find("chord") is None)*2!=full:
        return None
    tail=list(measure)[list(measure).index(backup):]
    if tail[:4]!=items[-4:] or any(n.tag!="barline" for n in tail[4:]):
        return None
    return items[-4:],rest


def ink_proof(image,g,pre,rows):
    n=with_bounds(g);r=with_bounds(pre)
    if not all(native_anchor(k,table) for k in rows for table in (n,r)):
        return None
    heads={k["head"] for k in rows}
    chord=rows[0]["chord"]
    staff=rows[0]["staff"]
    part=next((p for p in g.findall("part") if len(p.findall("staff"))==2
               and p.findall("staff")[-1].get("id")==staff),None)
    if part is None:
        return None
    source=[p for p in part.findall("measure") if chord in p.findtext("head-chords","").split()]
    if len(source)!=1:
        return None
    lines=[dict(thickness=float(l.get("thickness")),
                points=[(float(q.get("x")),float(q.get("y"))) for q in l.findall("point")])
           for l in part.findall("staff")[-1].findall("lines/line")]
    if len(lines)!=5:
        return None
    rel=g.findall("sig/relations/relation")
    def contained(parent):
        return {v.get("target") for v in rel
                if v.get("source")==parent and v.find("containment") is not None}
    if contained(chord)!=heads:
        return None
    if any((q.get("source") in heads|{chord} or q.get("target") in heads|{chord})
           and not {v.tag for v in q}<={"head-stem","head-head","chord-stem","containment"}
           for q in rel):
        return None
    source_rest_ids=set(source[0].findtext("rest-chords","").split())
    rests=[n[e] for grp in source_rest_ids for e in contained(grp) if e in n
           and n[e].get("shape")=="HALF_REST" and n[e].get("staff")==staff]
    if len(rests)!=1 or rests[0].get("id") not in r or coordinate(rests[0])!=coordinate(r[rests[0].get("id")]):
        return None
    rest_region=coordinate(rests[0])
    if rest_region[0]+rest_region[2]>=min(int(row["x"]) for row in rows):
        return None
    dots={key:source_ink(image,n[key],lines) for key in heads}
    if any(any(v[:3]) or v[-1]<.30*coordinate(n[k])[2]*coordinate(n[k])[3] for k,v in dots.items()):
        return None
    rest_trace=source_ink(image,rests[0],lines)
    if min(rest_trace[:3])<.30*rest_region[2]*rest_region[3]:
        return None
    return dict(chord=chord,false_head_ink=dots,preserved_original_half_rest=rests[0].get("id"),
                rest_dark_ink=rest_trace)


def recover(root,job):
    job=Path(job)
    verified=read_control(job)
    if verified is None:
        return root, dict(state='not_assessed', changes=[])
    archive,ref,ledger=verified
    live=deepcopy(root)
    report=[]
    with ZipFile(archive) as book, ZipFile(job/"raw/input.omr") as original:
        for part in live.findall("part"):
            pid=part.get("id")
            if not any(n.text=="2" for n in part.findall("measure/attributes/staves")):
                continue
            reference=ref.find(f"part[@id='{pid}']")
            if reference is None:
                continue
            for m in part.findall("measure"):
                measure=m.get("number")
                selection=layout(m)
                if selection is None:
                    continue
                counterpart=reference.find(f"measure[@number='{measure}']")
                if counterpart is None or layout(counterpart) is None:
                    continue
                suffix,rest=selection
                old=[*layout(counterpart)[0],layout(counterpart)[1]]
                if any(a.tag!=b.tag or a.attrib!=b.attrib or
                       [(q.tag,q.text) for q in a if q.tag!="pitch"]!=[(q.tag,q.text) for q in b if q.tag!="pitch"]
                       or [a.findtext("pitch/"+t) for t in ("step","alter","octave")]!=
                          [b.findtext("pitch/"+t) for t in ("step","alter","octave")]
                       for a,b in zip([*suffix,rest],old,strict=True)):
                    continue
                note_count=len(counterpart.findall("note"))
                targets=[ledger.get((pid,measure,i)) for i in (note_count-2,note_count-1)]
                if not all(targets) or any(q["shape"]!="NOTEHEAD_BLACK" for q in targets):
                    continue
                fields=("unit","system","staff","chord","source_sha256")
                if any(targets[0][f]!=targets[1][f] for f in fields):
                    continue
                unit,system=(targets[0][k] for k in ("unit","system"))
                p=job/f"unit-{int(unit):03d}.png"
                digest=sha(p)
                if digest!=targets[0]["source_sha256"]:
                    continue
                with Image.open(p) as img:image=np.asarray(img.convert("L"))
                solution=ink_proof(image,graph(book,unit,system),graph(original,unit,system),targets)
                if solution is None:
                    continue
                for n in suffix:m.remove(n)
                report.append(dict(part=pid,measure=measure,paper_sha256=digest,**solution))
    return (live if report else root), dict(state='restored' if report else 'unchanged', changes=report)
