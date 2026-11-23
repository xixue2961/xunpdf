"""Correct piano notes interpreted with treble after a printed bass clef was missed."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from statistics import median
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, header_clefs, keep_clock, mark_rhythm
from source_vector_isolated_grace import diatonic, glyph_at
from source_vector_scale_pitch import SIGNS, STEPS, open_source, set_pitch
from source_vector_whole_chord_key import old_key, physical_bars, staff_models


def printed_bass(system, piano, lines, glyphs, position):
    low=piano.findall("staff")[-1]
    staff=low.get("id")
    if any(n.get("staff")==staff for n in system.findall("sig/inters/clef")):
        return False
    first=system.findall("stack")[0]
    lo=position({"x":first.get("left"),"y":0})[0]
    header=low.find("header")
    if header is None:
        return False
    upper_limit=position({"x":header.get("stop"),"y":0})[0]+(lines[1]-lines[0])*.75
    return header_clefs(glyphs,lo,upper_limit,lines)==["?"]


def source_quarters(source, unit, system, row, old_notes):
    page=source.page(unit)
    if page is None:
        return None
    glyphs,paths,position=page
    layout=staff_models(system,row["native_part"],position)
    if layout is None:
        return None
    piano,models,staves=layout
    staff=piano.findall("staff")[-1].get("id")
    if row["staff"]!=staff:
        return None
    lines=models[staff]
    if not printed_bass(system,piano,lines,glyphs,position):
        return None
    bar=physical_bars(system,piano,row)
    if bar is None:
        return None
    lo,hi=[position({"x":bar.get(side),"y":0})[0] for side in ("left","right")]
    space=median([b-a for a,b in zip(lines,lines[1:])])
    if not 3.8<=space<=5.3:
        return None
    local=[g for g in glyphs if lo<g.left<hi and lines[0]-3*space<g.y<lines[-1]+4*space
           and min(staves,key=lambda q:abs(q[0]-g.y))[1]==staff]
    if any(g.character in (".","w","˙","&","?") or g.character in SIGNS for g in local):
        return None
    heads=[g for g in local if g.character=="œ"]
    if not heads or len(heads)!=len(old_notes) or any(not 5.1<=g.right-g.left<=6.7 for g in heads):
        return None
    page_id=source.mapping[unit]["pdf_page"]
    minimum=min(g.top for g in heads)-2
    maximum=max(g.bottom for g in heads)+2
    if any(a<hi and b>lo and y<maximum and z>minimum for a,y,b,z in source.curves[page_id]):
        return None
    chords=assign_chords(heads,paths,lo,hi)
    if (not chords or len(chords)!=4 or not mark_rhythm(chords,local,source.beams[page_id])
        or any(c.ticks!=1 for c in chords)):
        return None
    correlation={}
    for event,r in old_notes:
        match=glyph_at(r,local,position)
        if len(match)!=1 or match[0] in correlation:
            return None
        correlation[match[0]]=event
    if set(correlation)!=set(heads):
        return None
    update=[]
    for beat,chord in enumerate(chords):
        for h in chord.heads:
            event=correlation[h]
            note=event["node"]
            index=(h.y-lines[0])/(space/2)
            bass=26-round(index)
            treble=38-round(index)
            if (event["start"]!=beat or abs(index-round(index))>.18 or diatonic(note)!=treble
                or not 0<=bass//7<=9):
                return None
            update.append((note,(bass,STEPS[bass%7],bass//7,0,None)))
    return update


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    archive,native,rows=base
    piano={n.get("id") for n in root.findall("part-list/score-part") if n.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    fixed=deepcopy(root)
    current=_score_events(fixed)[0]
    reference=_score_events(native)[0]
    groups=defaultdict(list)
    for k,e in current.items():
        if k[0] in piano and e["node"].findtext("staff")=="2":
            groups[k[:2]].append((k,e))
    with closing(vector),ZipFile(archive) as book:
        for (pid,bar),items in groups.items():
            if not 4<=len(items)<=18:
                continue
            if any(k not in rows or k not in reference or
                   _music_signature(e)!=_music_signature(reference[k]) or
                   e["node"].get("default-x")!=reference[k]["node"].get("default-x") for k,e in items):
                continue
            measure=items[0][1]["measure"]
            part=fixed.find(f"part[@id='{pid}']")
            if (old_key(part,measure)!="0" or keep_clock(part,measure) is None
                or any(measure.find(f"attributes/{t}") is not None for t in ("clef","key","time"))):
                continue
            if any(e["duration"]!=1 or e["node"].findtext("type")!="quarter" or e["node"].find("pitch") is None
                   or e["node"].findtext("pitch/alter","0")!="0" or
                   any(e["node"].find(t) is not None for t in ("accidental","notations","grace","tie","dot","lyric"))
                   for _,e in items):
                continue
            if len({e["node"].findtext("voice","1") for _,e in items})!=1:
                continue
            data=[(e,rows[k]) for k,e in items]
            context={(r["unit"],r["system"],r["staff"],r["native_part"],r["source_sha256"]) for _,r in data}
            if len(context)!=1:
                continue
            unit,system,_,_,digest=context.pop()
            raster=job/f"unit-{int(unit):03d}.png"
            if not raster.is_file() or sha(raster)!=digest:
                continue
            src=graph(book,unit,system)
            if src is None:
                continue
            try:
                plan=source_quarters(vector,unit,src,data[0][1],data)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if plan is None:
                continue
            for node,pitch in plan:
                set_pitch(node,pitch)
            report["changes"].append(dict(part=pid,measure=bar,physical_heads=len(plan),source_page=vector.mapping[unit]["pdf_page"],
                                          printed_clef="bass",written_attacks=4))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

