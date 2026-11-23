"""Recover PDF-confirmed flats and double flats, including accidentals misread as symbols."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import glyph_at
from source_vector_scale_pitch import open_source, set_pitch
from source_vector_sustained_chords import anchored_site, note_value


def accidental_at(vec, page, head):
    kind = {"∫": 2.6,"b": 2.5,"n": 0,"#": 0,"‹": 0}
    glyphs = text_region(vec, page, (head.left-17,head.left,head.y-4,head.y+1.1))
    return [g for g,f in glyphs if f == b"Maestro" and g.character in kind
            and .55 < head.left-g.right < 8
            and abs(head.y-g.y-kind[g.character]) < .65]


def recover_flat_as_head(vec, page, view, items, directions, refs):
    part,bar = items[0][0][:2]
    chords = defaultdict(list)
    for k,e in items:
        if e["node"].findtext("staff","1")=="1":
            chords[e["start"]].append((k,e))
    for beat,group in chords.items():
        if len(group)!=3 or any(e["node"].findtext("type")!="half" or e["duration"]!=2 for _,e in group):
            continue
        heads = [glyph_at(refs[k],view[0],view[2]) for k,_ in group]
        if sorted(map(len,heads))!=[0,1,1]:
            continue
        bad = heads.index([])
        key,event = group[bad]
        fake = event["node"]
        if fake.find("chord") is None or any(fake.find(tag) is not None for tag in ("lyric","tie","notations","accidental")):
            continue
        x,y = view[2](refs[key])
        same_column = [h[0] for h in heads if h]
        if abs(same_column[0].left-same_column[1].left)>.75:
            continue
        candidates = []
        for (_,e),hit in zip(group,heads,strict=True):
            if not hit:
                continue
            sign = accidental_at(vec,page,hit[0])
            if len(sign)==1 and sign[0].character=="b" and abs(sign[0].left-x)<1 and abs(sign[0].y+2.5-y)<1:
                candidates.append(e["node"])
        if len(candidates)!=1:
            continue
        pitch = candidates[0]
        step,octave,alteration = note_value(pitch)
        if alteration not in (0,-1) or pitch.find("tie") is not None or pitch.find("accidental") is not None:
            continue
        for _,e in group:
            n = e["node"]
            if n.findtext("voice","1")!=pitch.findtext("voice","1"):
                break
        else:
            set_pitch(pitch,(None,step,octave,-1,"flat"))
            event["measure"].remove(fake)
            removed = False
            for d in directions:
                node = d["node"]
                word = node.find("direction-type/words")
                if ((d["part"],d["measure"],d["staff"],d["start"]) == (part,bar,"1",beat)
                        and word is not None and (word.text or "").strip()=="L"
                        and word.get("font-family")=="sans-serif" and word.get("relative-x")=="-11"
                        and word.get("default-y")=="8" and node.get("placement")=="above"
                        and [n.tag for n in node]==["direction-type"]):
                    event["measure"].remove(node)
                    removed = True
            return dict(part=part,measure=bar,staff="1",page=page,pitch=f"{step}{octave}",
                        removed_false_head=key[2],removed_ocr="L" if removed else None)
    return None


def recover(root, job, source):
    job = Path(job)
    report = dict(state="unchanged",changes=[])
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, original, refs = control
    vec = open_source(source, job)
    if vec is None:
        return root, report
    fixed = deepcopy(root)
    events,directions = _score_events(fixed)
    originals,_ = _score_events(original)
    eligible = defaultdict(list)
    for k,e in events.items():
        note = e["node"]
        prior = originals.get(k)
        if (k in refs and prior and note.find("pitch") is not None
                and note.get("default-x") is not None and note.get("default-x") == prior["node"].get("default-x")):
            eligible[k[:2]].append((k,e))
    frames,digests = {},{}
    with closing(vec), ZipFile(archive) as book:
        candidates = {page for page in {x["pdf_page"] for x in vec.mapping.values()}
                      if any(g.character == "∫" and f == b"Maestro"
                             for g,f in text_region(vec,page,(0,900,0,1200)))}
        for (part,bar),items in eligible.items():
            active, fixed_beats = set(),set()
            flat_page = None
            for k,e in sorted(items,key=lambda kv:(kv[1]["start"],kv[0][2])):
                unit = str(refs[k]["unit"])
                view = vec.page(unit)
                if view is None or vec.mapping[unit]["pdf_page"] not in candidates:
                    continue
                page = vec.mapping[unit]["pdf_page"]
                hits = glyph_at(refs[k],view[0],view[2])
                if len(hits)!=1 or hits[0].character not in ("œ","˙","w"):
                    continue
                note = e["node"]
                step,octave,previous = note_value(note)
                staff = note.findtext("staff","1")
                address = staff,step,octave
                marks = accidental_at(vec,page,hits[0])
                if len(marks)>1 or not marks and address not in active:
                    continue
                explicit = len(marks)==1 and marks[0].character=="∫"
                if marks and not explicit:
                    active.discard(address)
                    continue
                if previous not in (-2,-1) or note.find("tie[@type='stop']") is not None:
                    continue
                site = anchored_site([refs[k]],book,frames)
                if site is None:
                    continue
                unit, _, _, digest, _ = site
                if unit not in digests:
                    raster = job/f"unit-{int(unit):03d}.png"
                    digests[unit] = sha(raster) if raster.is_file() else None
                if digests[unit]!=digest:
                    continue
                if explicit:
                    active.add(address)
                    fixed_beats.add((staff,e["start"]))
                    flat_page = page,view
                if previous==-2 and (not explicit or note.findtext("accidental")=="flat-flat"):
                    continue
                marker = "flat-flat" if explicit else None
                set_pitch(note,(None,step,octave,-2,marker))
                if explicit:
                    note.find("accidental").text = marker
                report["changes"].append(dict(part=part, measure=bar,staff=staff,note=k[2],
                                              page=page,pitch=f"{step}{octave}",printed=explicit))
            if flat_page:
                correction = recover_flat_as_head(vec,*flat_page,items,directions,refs)
                if correction:
                    report["changes"].append(correction)
            for d in directions:
                node = d["node"]
                word = node.find("direction-type/words")
                if ((d["part"],d["measure"]) != (part,bar) or (d["staff"],d["start"]) not in fixed_beats
                        or word is None or (word.text or "").strip()!="bb" or word.get("font-family")!="sans-serif"
                        or node.get("placement")!="below" or [n.tag for n in node]!=["direction-type","staff"]):
                    continue
                if not (word.get("relative-x")=="-18" and word.get("default-y")=="-77" and d["staff"]=="2"):
                    continue
                items[0][1]["measure"].remove(node)
                report["changes"].append(dict(part=part,measure=bar,staff=d["staff"],removed_ocr="bb"))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

