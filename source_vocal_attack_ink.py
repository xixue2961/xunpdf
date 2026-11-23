"""Recover source-proven reattacks and overlapping vocal whole notes."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import add_mark, add_tie, current_key
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import STEPS, open_source
from source_vector_sustained_chords import anchored_site, note_value
from source_vector_vocal_lyrics import anchors, center, lyric, printed_rows


def marks(note, kind):
    return note.findall(f"tie[@type='{kind}']")


def retire(note, kind):
    for e in marks(note, kind):
        note.remove(e)
    for container in note.findall("notations"):
        for e in container.findall(f"tied[@type='{kind}']"):
            container.remove(e)
        if len(container) == 0:
            note.remove(container)


def point_lyric(phrase, x):
    hits = [g.character for g in phrase if abs(center(g) - x) < 1.1]
    return hits[0] if len(hits) == 1 else None


def false_reattack(items, coordinates, heads, phrase, curves, beams, neighboring_rows):
    notes = [event["node"] for _, event in items]
    if (len(notes) != len(heads) or len(notes) < 3 or len(coordinates) != len(notes)
            or any(n.find("lyric") is not None or n.find("chord") is not None for n in notes)
            or not any(row == "".join(g.character for g in phrase) for row in neighboring_rows)):
        return None
    by_note = {n: x for x, _, n in coordinates}
    first, second = notes[-2:]
    if (note_value(first) != note_value(second) or first.findtext("type") != "eighth"
            or second.findtext("type") != "eighth" or first.findtext("beam") != "begin"
            or second.findtext("beam") != "end" or len(marks(first, "start")) != 1
            or len(marks(second, "stop")) != 1 or marks(first, "stop") or marks(second, "start")):
        return None
    a, b = heads[-2:]
    if (a.character != "œ" or b.character != "œ" or abs(a.y-b.y) > .25
            or abs(center(a)-by_note[first]) > .3 or abs(center(b)-by_note[second]) > .3
            or not point_lyric(phrase,center(a)) or not point_lyric(phrase,center(b))
            or sum(a.left < q[0] < a.right + 1 and b.left < q[2] < b.right + 1
                   and a.y - 19 < q[1] < a.y - 7 and 1.2 < q[3] - q[1] < 5 for q in beams) != 1
            or any(a.left - 1 <= q[0] < b.left and b.left - 3 <= q[2] <= b.right + 4
                   and a.y-15 < q[1] < a.y+15 for q in curves)):
        return None
    plan = []
    for glyph in phrase:
        targets = [n for n, x in by_note.items() if abs(x - center(glyph)) < 1.1]
        if len(targets) != 1:
            return None
        plan.append((targets[0],glyph.character))
    if len(set(n for n,_ in plan)) != len(plan):
        return None
    retire(first, "start")
    retire(second, "stop")
    for note,value in plan:
        lyric(note,1,value)
    return len(plan)


def written(glyph, bounds, base):
    _, _, top, bottom = bounds
    degree = base - (glyph.y - top) * 8 / (bottom - top)
    if abs(degree - round(degree)) > .22:
        return None
    degree = round(degree)
    return STEPS[degree % 7], degree // 7


def attach_whole(part, bar, items, coords, heads, phrase, bounds, paths, curves, tokens, following):
    if (len(phrase) != 2 or len(heads) != 3 or sorted(g.character for g in heads) != ["w","˙","˙"]
            or bar.find("backup") is not None or bar.find("forward") is not None):
        return None
    whole = next(g for g in heads if g.character == "w")
    halves = sorted((g for g in heads if g.character == "˙"),key=lambda g:g.left)
    lower, ending = halves
    if (abs(lower.left-whole.left) > .3 or lower.y <= whole.bottom or
            whole.right-whole.left < lower.right-lower.left+1):
        return None
    stem = [q for q in paths if abs(q[0]-whole.left)<1 and abs(q[2]-whole.left)<1
            and 7<q[3]-q[1]<22 and abs(q[1]-whole.y)<10]
    if len(stem) != 1 or stem[0][1]<=whole.bottom+1.7 or abs(stem[0][1]-lower.y)>1:
        return None
    notes = [event["node"] for _, event in items]
    if len(notes) not in (2,3):
        return None
    first,last = notes[0],notes[-1]
    first_time, last_time = items[0][1], items[-1][1]
    if ((first_time["start"], first_time["duration"], last_time["start"], last_time["duration"]) != (0, 2, 2, 2)
            or any(n.find(tag) is not None for n in notes for tag in ("dot", "time-modification", "grace"))):
        return None
    if (not marks(first,"stop") or first.findtext("type")!="half" or last.findtext("type")!="half"
            or first.findtext("duration")!=last.findtext("duration") or first.find("chord") is not None
            or last.find("chord") is not None or not first.get("default-x")):
        return None
    col = {n:x for x,_,n in coords}
    if first not in col or last not in col or abs(col[first]-center(lower))>.4 or abs(col[last]-center(ending))>.4:
        return None
    bases = [b for b in (26,31,38) if written(lower,bounds,b)==note_value(first)[:2]
             and written(ending,bounds,b)==note_value(last)[:2]]
    if len(bases)!=1:
        return None
    pitch = written(whole,bounds,bases[0])
    key = current_key(part,bar)
    if pitch is None or key is None:
        return None
    value1, value2 = point_lyric(phrase,center(lower)),point_lyric(phrase,center(ending))
    if not value1 or not value2:
        return None
    displaced = first.findall("lyric")
    ending_lyrics = last.findall("lyric")
    for entries, character in ((displaced, value1), (ending_lyrics, value2)):
        if len(entries) > 1 or any(
                set(entry.attrib) - {"number", "placement"}
                or entry.get("number", "1") != "1"
                or [child.tag for child in entry] != ["syllabic", "text"]
                or entry.findtext("syllabic") != "single"
                or entry.findtext("text") != character
                or any(child.attrib or len(child) for child in entry)
                for entry in entries):
            return None
    if any(note.find("lyric") is not None for note in notes[1:-1]):
        return None
    full = 2 * int(first.findtext("duration"))
    voice_ids = {n.findtext("voice","1") for n in bar.findall("note")}
    if following is not None:
        voice_ids.update(n.findtext("voice","1") for n in following[0].findall("note"))
    voice = next((str(i) for i in range(1,17) if str(i) not in voice_ids),None)
    if voice is None:
        return None
    if len(notes)==3:
        existing=notes[1]
        if (existing.find("chord") is None or existing.find("notations") is not None
                or note_value(existing)[:2]!=pitch or existing.findtext("duration")!=first.findtext("duration")):
            return None
    else:
        existing=None
    attack=ET.Element("note",{"default-x":first.get("default-x")})
    tone=ET.SubElement(attack,"pitch")
    ET.SubElement(tone,"step").text=pitch[0]
    alter=key.get(pitch[0],0)
    if alter:ET.SubElement(tone,"alter").text=str(alter)
    ET.SubElement(tone,"octave").text=str(pitch[1])
    ET.SubElement(attack,"duration").text=str(full)
    ET.SubElement(attack,"voice").text=voice
    ET.SubElement(attack,"type").text="whole"
    accents = [g for g, font in tokens if font == b"Maestro" and g.character == ">"
               and abs(g.left - whole.left) < 1 and whole.y - 17 < g.y < whole.y - 8]
    if len(accents) == 1:
        articulation = ET.Element("articulations")
        ET.SubElement(articulation, "accent", placement="above")
        add_mark(attack, articulation)
    lyric(attack,1,value1)
    if not ending_lyrics:
        lyric(last,1,value2)
    for entry in displaced:
        first.remove(entry)
    if existing is not None:bar.remove(existing)
    backup=ET.Element("backup")
    ET.SubElement(backup,"duration").text=str(full)
    end=next((i for i,e in enumerate(bar) if e.tag=="barline" and e.get("location","right")=="right"),len(bar))
    bar[end:end]=[backup,attack]
    boundary_tie=False
    if following and not marks(last,"start"):
        _, next_coords, next_heads=following
        targets=[(x,n) for x,onset,n in next_coords if onset==0 and note_value(n)==note_value(last)
                 and not marks(n,"stop")]
        if len(targets)==1:
            x, target=targets[0]
            heads_after=[g for g in next_heads if abs(center(g)-x)<.5 and abs(g.y-ending.y)<.4]
            if len(heads_after)==1:
                connection=heads_after[0]
                matches=[q for q in curves if abs(q[0]-ending.right)<3 and abs(q[2]-connection.left)<5
                         and abs(connection.y-ending.y)<.4 and ending.y<q[1]<ending.y+8]
                if matches and all(max(abs(a-b) for a,b in zip(q,matches[0]))<1.0 for q in matches):
                    add_tie(last,"start")
                    add_tie(target,"stop")
                    boundary_tie=True
    return {"whole": list(pitch), "lyric_characters": 2, "boundary_tie": boundary_tie}


def recover(root, job, source):
    job=job.resolve()
    base=read_control(job) or fallback_control(job)
    vector=open_source(source,job)
    report={"state":"unchanged","changes":[]}
    if not base or vector is None:
        if vector is not None:vector.close()
        return root,report
    archive, native, refs=base
    fixed=deepcopy(root)
    by_part={p.get("id"):p for p in fixed.findall("part")}
    vocals={p.get("id") for p in fixed.findall("part-list/score-part")
            if p.findtext("part-name","").casefold() in {"tenor","bass","alto","soprano"}}
    live,_=_score_events(fixed)
    control,_=_score_events(native)
    scopes=defaultdict(list)
    for key,e in live.items():
        if key[0] in vocals:scopes[key[:2]].append((key,e))
    frames,hashes,pages={},{},{}
    with closing(vector),ZipFile(archive) as book:
        def geometry(pid,label,items):
            source_rows=[refs[k] for k,e in items if k in refs and e["node"].find("pitch") is not None]
            if len(source_rows)<2:return None
            site=anchored_site(source_rows,book,frames)
            if site is None:return None
            unit=site[0]
            if unit not in hashes:
                fn=job/f"unit-{int(unit):03d}.png"
                hashes[unit]=sha(fn) if fn.is_file() else None
            if hashes[unit]!=site[3]:return None
            view=vector.page(unit)
            bounds=scope_at(site,view,1) if view else None
            if bounds is None:return None
            left,right,top,bottom=bounds
            if any(g.character in {"b","n","#"} and left+7<g.left<right and top-3<g.y<bottom+8
                   for g in view[0]):return None
            heads=sorted([g for g in view[0] if g.character in {"œ","˙","w"} and
                          left<=g.left<right and top-8<g.y<bottom+12],key=lambda g:(g.left,g.y))
            page=vector.mapping[unit]["pdf_page"]
            if page not in pages:
                width,height=vector.mapping[unit]["pdf_page_size"]
                pages[page]=text_region(vector,page,(0,width,0,height))
            rows=printed_rows(pages[page],bounds)
            coords=anchors(items,refs,control,view,bounds,include_tied=True)
            return unit,view,bounds,heads,rows,coords
        for pid in sorted(vocals):
            part=by_part[pid]
            bars=part.findall("measure")
            for i,bar in enumerate(bars):
                label=bar.get("number")
                items=scopes[pid,label]
                notes=bar.findall("note")
                if not 2<=len(notes)<=8:continue
                if not any(marks(n,"stop") for n in notes):continue
                data=geometry(pid,label,items)
                if data is None:continue
                unit,view,bounds,heads,rows,coords=data
                if not rows or len(rows)!=1:continue
                curves=vector.curves[vector.mapping[unit]["pdf_page"]]
                peers=["".join(e.findtext("text","") for e in by_part[other].findall(f"measure[@number='{label}']/note/lyric"))
                       for other in vocals-{pid}]
                page = vector.mapping[unit]["pdf_page"]
                count=false_reattack(items,coords,heads,rows[0],curves,vector.beams[page],peers)
                if count:
                    report["changes"].append({"part":pid,"measure":label,"false_tie_removed":True,"lyric_characters":count})
                    continue
                following=None
                if i+1<len(bars):
                    nxt=bars[i+1]
                    following_data=geometry(pid,nxt.get("number"),scopes[pid,nxt.get("number")])
                    if following_data and following_data[0]==unit:
                        # Use the pitch-bearing onset's own native anchors, including a chord member if needed.
                        d=following_data
                        candidates=list(d[-1])
                        for k,event in scopes[pid,nxt.get("number")]:
                            n=event["node"]
                            if k not in refs or k not in control or n.find("pitch") is None:continue
                            if n.get("default-x")!=control[k]["node"].get("default-x"):continue
                            x,y=d[1][2](refs[k])
                            matches=[g for g in d[3] if abs(g.left-x)<1.2 and abs(g.top-y)<1.2]
                            if len(matches)==1 and all(target is not n for _,_,target in candidates):
                                candidates.append((center(matches[0]),event["start"],n))
                        following=(nxt,candidates,d[3])
                change=attach_whole(part,bar,items,coords,heads,rows[0],bounds,view[1],curves,pages[page],following)
                if change:report["changes"].append(dict(part=pid,measure=label,**change))
    if not report["changes"]:return root,report
    report["state"]="restored"
    return fixed,report
