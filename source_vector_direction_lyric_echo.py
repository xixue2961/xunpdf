"""Remove OCR word objects whose source glyphs belong to the adjacent lyrics."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import re
from statistics import median
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site
from source_vector_vocal_lyrics import LYRIC_FONTS


def lyric_fragment(glyphs, text, x, y, upper_bottom, space):
    # Relative-y is positioned at the old text baseline, below the glyph center.
    letters = sorted((g for g, font in glyphs if font in LYRIC_FONTS
                      and "\u4e00" <= g.character <= "\u9fff"
                      and 1.8*space < g.y-upper_bottom < 6.6*space),key=lambda g:g.left)
    possible = []
    for g in letters:
        if (g.character != text[0] or abs(g.left-x) > 1.9*space
                or not 0 <= y-g.y <= 1.95*space):
            continue
        row = [a for a in letters if g.left <= a.left < g.left+13*space and abs(a.y-g.y) < .40*space]
        matches = 0
        skips = 0
        for mark in row:
            if mark.character == text[matches]:
                matches += 1
                if matches == len(text):
                    possible.append((g,mark))
                    break
            else:
                skips += 1
                if skips > 1:
                    break
    return possible[0] if len(possible)==1 else None


def recover(root, job, source):
    job = Path(job)
    report = dict(state="unchanged", changes=[])
    ids = {n.get("id"):n.findtext("part-name","").casefold() for n in root.findall("part-list/score-part")}
    if not {"tenor","bass"} <= set(ids.values()):
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    native,control,refs = base
    vec = open_source(source,job)
    if vec is None:
        return root, report
    fixed = deepcopy(root)
    events,directions = _score_events(fixed)
    known = _score_events(control)[0]
    groups = defaultdict(list)
    for k,event in events.items():
        groups[k[:2]].append((k,event))
    parts = {part.get("id"):part for part in fixed.findall("part")}
    frames,digests = {},{}
    with closing(vec), ZipFile(native) as archive:
        for item in directions:
            p,bar,node = item["part"],item["measure"],item["node"]
            role = ids.get(p)
            if role not in ("bass","piano") or node.get("placement")!="above" or item["staff"]!="1":
                continue
            word = node.find("direction-type/words")
            if (word is None or word.get("font-family")!="sans-serif"
                    or [c.tag for c in node] not in (["direction-type"],["direction-type","staff"])
                    or len(node[0])!=1):
                continue
            token = re.sub(r"\s+","",word.text or "")
            if not re.fullmatch(r"[\u4e00-\u9fff]{1,5}",token):
                continue
            try:
                dx,dy = float(word.get("relative-x","0")),float(word.get("default-y","0"))
            except ValueError:
                continue
            if not 0<dy<50:
                continue
            candidates=[]
            for k,event in groups[p,bar]:
                n = event["node"]
                prior = known.get(k)
                if (k in refs and prior and event["start"]==item["start"]
                        and event["start"]==prior["start"] and n.find("pitch") is not None
                        and n.findtext("staff","1")=="1" and n.get("default-x") is not None
                        and n.get("default-x")==prior["node"].get("default-x")):
                    candidates.append(refs[k])
            site = anchored_site(candidates,archive,frames) if candidates else None
            if site is None:
                continue
            unit,frame,native_id,digest,index = site
            if unit not in digests:
                raster = job/f"unit-{int(unit):03d}.png"
                digests[unit] = sha(raster) if raster.is_file() else None
            view = vec.page(unit)
            if view is None or digests[unit]!=digest:
                continue
            physical = [p.get("id") for p in frame.findall("part") if len(p.findall("staff"))]
            position = physical.index(native_id)
            if not position:
                continue
            upper_id = physical[position-1]
            current,upper = (scope_at((unit,frame,n,digest,index),view,1) for n in (native_id,upper_id))
            if current is None or upper is None:
                continue
            lo,hi,top,bottom = current
            space=(bottom-top)/4
            if not 3.9<space<5.3 or not 9*space<top-upper[2]<14*space:
                continue
            columns = [view[2](r)[0] for r in candidates]
            if max(columns)-min(columns) > 1.4*space:
                continue
            x=median(columns)+dx*space/10
            y=top-dy*space/10
            page = vec.mapping[unit]["pdf_page"]
            width = vec.mapping[unit]["pdf_page_size"][0]
            glyphs=text_region(vec,page,(max(0,x-3*space),min(width,x+15*space),upper[3],top))
            hit=lyric_fragment(glyphs,token,x,y,upper[3],space)
            if not hit:
                continue
            parts[p].find(f'measure[@number="{bar}"]').remove(node)
            report["changes"].append(dict(part=p,measure=bar,word=word.text,source_page=page,
                                          source_box=[hit[0].left,hit[0].top,hit[1].right,hit[1].bottom]))
    if report["changes"]:
        report["state"]="restored"
        return fixed, report
    return root, report
