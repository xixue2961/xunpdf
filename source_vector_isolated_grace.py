"""Recover isolated slashed grace notes from verified music-font glyphs."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control, small_note
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_scale_pitch import SIGNS, STEPS, open_source, set_pitch

HEADS = {"NOTEHEAD_VOID": "˙", "NOTEHEAD_BLACK": "œ", "WHOLE_NOTE": "w"}


def small_heads(glyphs, paths):
    for h in glyphs:
        if h.character!="œ" or not 3.1<=h.right-h.left<=4.0 or not 2.3<=h.bottom-h.top<=3.2:
            continue
        flags = [g for g in glyphs if g.character=="j" and abs(g.left-h.right)<.55 and abs(g.bottom-h.top)<.65
                 and 7<=g.bottom-g.top<=10]
        if len(flags)!=1:
            continue
        flag=flags[0]
        stems=[p for p in paths if p[4]==0 and abs((p[0]+p[2])/2-flag.left)<.7
               and p[2]-p[0]<.85 and abs(p[1]-flag.top)<1 and abs(p[3]-h.top)<1.35]
        slashes=[p for p in paths if p[4]==0 and p[0]<flag.left<p[2] and 4.6<=p[2]-p[0]<=7.1
                 and 3.1<=p[3]-p[1]<=5.6 and flag.top+.8<p[1]<p[3]<h.top-.65]
        if len(stems)==1 and len(slashes)==1:
            yield h


def glyph_at(row,glyphs,position):
    x,y=position(row)
    mark=HEADS.get(row["shape"])
    return [g for g in glyphs if g.character==mark and g.right-g.left>5.1
            and abs(g.left-x)<1.4 and abs(g.top-y)<1.4]


def diatonic(node):
    return STEPS.index(node.findtext("pitch/step"))+7*int(node.findtext("pitch/octave"))


def grace_pitch(h, anchor, anchor_glyph, spacing, key, notes, glyphs):
    delta=(anchor_glyph.y-h.y)/(spacing/2)
    shift=round(delta)
    if abs(delta-shift)>.18 or abs(shift)>5:
        return None
    n=diatonic(anchor)+shift
    step,octave=STEPS[n%7],n//7
    if not 0<=octave<=9:
        return None
    nearby = [g for g in glyphs if g.character in SIGNS and .35<=h.left-g.right<=4 and abs(g.y-h.y)<1.2]
    # Keep accidental carry conservative when another note can affect this pitch.
    if len(nearby)>1 or any(q.find("accidental") is not None and diatonic(q)==n for q in notes if q.find("pitch") is not None):
        return None
    alter,sign=SIGNS[nearby[0].character] if nearby else (key.get(step,0),None)
    return n,step,octave,alter,sign


def current_key(part, measure):
    key = None
    for m in part.findall("measure"):
        for a in m.findall("attributes"):
            keys=a.findall("key")
            if keys:
                key=keys[0].findtext("fifths") if len(keys)==1 and not keys[0].get("number") else None
        if m is measure:
            break
    if key is None or not -7<=int(key)<=7:
        return None
    count=int(key)
    return {step: 1 if count>0 else -1 for step in ("FCGDAEB" if count>0 else "BEADGCF")[:abs(count)]}


def supported_following(arc, anchor, following):
    return (abs(arc[0]-anchor.right)<2.5 and following.left-2<=arc[2]<=following.right+1
            and anchor.bottom-.7<=arc[1]<=anchor.bottom+3 and arc[3]-arc[1]<11)


def add_tie(note, kind):
    tie=ET.Element("tie",type=kind)
    voice=note.find("voice")
    note.insert(list(note).index(voice),tie)
    add_mark(note,ET.Element("tied",type=kind))


def add_mark(note, item):
    notations=note.find("notations")
    if notations is None:
        notations=ET.Element("notations")
        lyrics=note.find("lyric")
        note.insert(list(note).index(lyrics) if lyrics is not None else len(note),notations)
    notations.append(item)


def score_patch(plan,events):
    _, group, positions, pitch, following, short_slur = plan
    kept, removed=positions
    lead=events[kept[0]]["node"]
    miss=events[removed]["node"]
    bar=events[group[0]]["measure"]
    siblings=bar.findall("note")
    first=siblings.index(miss)
    if [events[k]["node"] for k in group]!=siblings[first:first+len(group)] or any(
        events[k]["node"].find("chord") is None for k in kept
    ):
        return None
    if miss.find("notations") is not None:
        if following is None or len(miss.findall("notations/*"))!=1:
            return None
    transferable={"pitch","duration","voice","type","dot","stem","staff","lyric","notations"}
    if {q.tag for q in miss}-transferable or miss.find("chord") is not None:
        return None
    if any(events[k]["node"].find("tie") is not None or events[k]["node"].find("lyric") is not None for k in kept):
        return None
    if following is None and miss.find("lyric") is not None:
        return None
    for lyric in miss.findall("lyric"):
        lead.append(deepcopy(lyric))
    lead.remove(lead.find("chord"))
    grace=small_note(lead)
    set_pitch(grace,pitch)
    if short_slur is not None:
        add_mark(grace,ET.Element("slur",type="start",number=str(short_slur),placement="below"))
        add_mark(lead,ET.Element("slur",type="stop",number=str(short_slur)))
    if following is not None:
        successor, old_stop=following
        add_tie(lead,"start")
        add_tie(successor,"stop")
        container=successor.find("notations")
        container.remove(old_stop)
    index=list(bar).index(miss)
    bar.insert(index,grace)
    bar.remove(miss)
    return dict(part=group[0][0],measure=group[0][1],grace_step=pitch[1],grace_octave=pitch[2],
                grace_alter=pitch[3],retained_principal_heads=len(kept),removed_unsupported_head=removed[2],
                restored_tie=following is not None)


def plan_one(h, groups, all_rows, glyphs, paths, curves, position, parts, directory, archive, events, references):
    candidates=[]
    for group in groups.values():
        if len(group)<2 or any(k not in references or _music_signature(events[k]) != _music_signature(references[k])
                               or events[k]["node"].get("default-x")!=references[k]["node"].get("default-x") for k in group):
            continue
        if any(all_rows[k]["shape"] not in HEADS for k in group):
            continue
        coords=[position(all_rows[k]) for k in group]
        if not 4<=min(x for x,y in coords)-h.left<=15 or min(abs(y-h.top) for x,y in coords)>14:
            continue
        aligned=[(k,glyph_at(all_rows[k],glyphs,position)) for k in group]
        if any(len(g)>1 for k,g in aligned):
            continue
        kept=[k for k,g in aligned if len(g)==1]
        missing=[k for k,g in aligned if not g]
        actual=[g[0] for k,g in aligned if g]
        if not kept or len(missing)!=1 or missing[0]!=group[0] or len(set(actual))!=len(actual):
            continue
        ghost_x,ghost_y=coords[0]
        if any(g.character in HEADS.values() and g.right-g.left>5.1
               and abs(g.left-ghost_x)<1.8 and abs(g.top-ghost_y)<1.8 for g in glyphs):
            continue
        spacing_gaps=[a.left-h.left for a in actual]
        if not all(4<=dx<=14 for dx in spacing_gaps) or max(spacing_gaps)-min(spacing_gaps)>1.45:
            continue
        anchor_k,geometry=min(zip(kept,actual,strict=True),key=lambda pair: abs(pair[1].y-h.y))
        if any(g.character in HEADS.values() and g.right-g.left>5.1 and h.right<g.left<geometry.left-1.4
               and abs(h.y-g.y)<4.2 for g in glyphs):
            continue
        row=all_rows[anchor_k]
        raster=directory/f"unit-{int(row['unit']):03d}.png"
        if not raster.is_file() or sha(raster)!=row["source_sha256"]:
            continue
        system=graph(archive,row["unit"],row["system"])
        if system is None:
            continue
        staff=system.find(f"part/staff[@id='{row['staff']}']")
        if staff is None:
            continue
        lines=[position({"x":0,"y":float(line.find("point").get("y"))})[1] for line in staff.findall("lines/line")]
        if len(lines)!=5:
            continue
        gaps=[right-left for left,right in zip(lines,lines[1:])]
        spacing=median(gaps)
        if not 3.5<=spacing<=6.2 or max(gaps)-min(gaps)>.35:
            continue
        part=parts[anchor_k[0]]
        measure=events[anchor_k]["measure"]
        key=current_key(part,measure)
        if key is None:
            continue
        pitch=grace_pitch(h,events[anchor_k]["node"],geometry,spacing,key,measure.findall("note"),glyphs)
        if pitch is None:
            continue
        following=None
        short_slur=None
        ghost=events[missing[0]]["node"]
        if ghost.find("lyric") is not None:
            if len(kept)!=1:
                continue
            original_slur=ghost.find("notations/slur[@type='start']")
            bars=part.findall("measure")
            index=bars.index(measure)
            if original_slur is None or index+1>=len(bars):
                continue
            successor_key=(anchor_k[0],bars[index+1].get("number"),0)
            if successor_key not in events or successor_key not in all_rows:
                continue
            nxt=events[successor_key]["node"]
            nxt_row=all_rows[successor_key]
            stop=nxt.find(f"notations/slur[@type='stop'][@number='{original_slur.get('number')}']")
            if (stop is None or nxt.find("tie") is not None or nxt.find("lyric") is not None
                    or nxt_row["unit"]!=row["unit"] or nxt_row["staff"]!=row["staff"]
                    or _music_signature(events[anchor_k])[2]!=_music_signature(events[successor_key])[2]):
                continue
            final=glyph_at(nxt_row,glyphs,position)
            if len(final)!=1 or len([arc for arc in curves if supported_following(arc,geometry,final[0])])!=1:
                continue
            compact=[arc for arc in curves if h.left-.2<arc[0]<h.right+1 and geometry.left-1<arc[2]<geometry.right
                     and h.bottom<arc[1]<h.bottom+3 and arc[3]-arc[1]<8]
            used_numbers={q.get("number","1") for q in part.findall(".//slur")}
            unused=next((number for number in range(16,0,-1) if str(number) not in used_numbers),None)
            if not compact or unused is None:
                continue
            following=(nxt,stop)
            short_slur=unused
        elif not any(g.character=="~" and h.right-1<=g.left<=geometry.left and abs(g.y-h.y)<4 for g in glyphs):
            # Require an independently printed ornament next to the false chord member.
            continue
        candidates.append((h,group,(kept,missing[0]),pitch,following,short_slur))
    return candidates[0] if len(candidates)==1 else None


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    book,native,rows=base
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    fixed=deepcopy(root)
    events=_score_events(fixed)[0]
    original=_score_events(native)[0]
    parts={p.get("id"):p for p in fixed.findall("part")}
    scopes=defaultdict(lambda:defaultdict(list))
    for k,item in events.items():
        if k in rows and item["node"].find("pitch") is not None and item["node"].find("grace") is None:
            r=rows[k]
            group=(k[0],k[1],r["staff"],r["chord"],item["start"],item["duration"])
            scopes[r["unit"]][group].append(k)
    with closing(vector),ZipFile(book) as archive:
        for unit,groups in scopes.items():
            try:
                page=vector.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if page is None:
                continue
            glyphs,paths,position=page
            curves=vector.curves[vector.mapping[unit]["pdf_page"]]
            for h in small_heads(glyphs,paths):
                plan=plan_one(h,groups,rows,glyphs,paths,curves,position,parts,job,archive,events,original)
                if plan is None:
                    continue
                if any((c["part"],c["measure"])==plan[1][0][:2] for c in report["changes"]):
                    continue
                change=score_patch(plan,events)
                if change is not None:
                    change["source_page"]=vector.mapping[unit]["pdf_page"]
                    change["notehead_pdf"]=[h.left,h.top,h.right,h.bottom]
                    report["changes"].append(change)
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

