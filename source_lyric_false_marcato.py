"""Remove native marcato detections that lie inside a printed lyric glyph."""
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
from source_vector_scale_pitch import open_source
from source_vector_vocal_lyrics import LYRIC_FONTS


def absorbed_by_character(bounds, tokens):
    l, t, r, b = bounds
    if not 2.5 < r-l < 8 or not 2.5 < b-t < 8:
        return None
    matches = [glyph for glyph, font in tokens if font in LYRIC_FONTS
               and "\u4e00" <= glyph.character <= "\u9fff"
               and 5.5 < glyph.right-glyph.left < 13 and 5.5 < glyph.bottom-glyph.top < 13
               and glyph.left-.45 <= l < r <= glyph.right+.45
               and glyph.top-.45 <= t < b <= glyph.bottom+.45]
    return matches[0].character if len(matches) == 1 else None


def recover(root, job, source):
    report = {"state":"not_applicable","changes":[]}
    if root.find(".//strong-accent[@type='up']") is None:
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, native, ledger = base
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    native_evs, _ = _score_events(native)
    live, _ = _score_events(fixed)
    scopes = defaultdict(list)
    rows = defaultdict(list)
    for key, item in live.items():
        if item["node"].find("notations/articulations/strong-accent[@type='up']") is not None:
            scopes[key[:2]].append(item["node"])
    for key, point in ledger.items():
        if key[:2] in scopes and key in native_evs:
            rows[point["unit"], point["system"], point["chord"]].append((key, point))
    with closing(vector), ZipFile(archive) as book:
        for unit in vector.mapping:
            name = f"sheet#{unit}/sheet#{unit}.xml"
            if name not in book.namelist():
                continue
            view = vector.page(unit)
            if view is None:
                continue
            picture = job / f"unit-{int(unit):03d}.png"
            if not picture.is_file():
                continue
            digest = sha(picture)
            page = vector.mapping[unit]["pdf_page"]
            width, height = vector.mapping[unit]["pdf_page_size"]
            tokens = text_region(vector,page,(0,width,0,height))
            position = view[2]
            source_sheet = ET.fromstring(book.read(name))
            for system in source_sheet.findall("page/system"):
                sid = system.get("id")
                markings = {a.get("id"): a.find("bounds") for a in system.findall("sig/inters/articulation")
                            if a.get("shape") == "MARCATO"}
                for relation in system.findall("sig/relations/relation"):
                    bounds = markings.get(relation.get("target"))
                    if bounds is None or relation.find("chord-articulation") is None:
                        continue
                    x,y,w,h = (int(bounds.get(k)) for k in ("x","y","w","h"))
                    area = (*position({"x": x, "y": y}), *position({"x": x+w, "y": y+h}))
                    character = absorbed_by_character(area, tokens)
                    if not character:
                        continue
                    located = set()
                    for key, point in rows[unit, sid, relation.get("source")]:
                        if point["source_sha256"] != digest:
                            continue
                        control = native_evs[key]["node"]
                        for node in scopes[key[:2]]:
                            if (node.findtext("staff","1") == control.findtext("staff","1")
                                    and node.get("default-x") == control.get("default-x")):
                                located.add((key[:2],node))
                    if len(located) != 1:
                        continue
                    (pid, measure), node = located.pop()
                    articulation = node.find("notations/articulations")
                    if articulation is None:
                        continue
                    marker = articulation.find("strong-accent[@type='up']")
                    if marker is None:
                        continue
                    articulation.remove(marker)
                    if len(articulation) == 0:
                        notation = node.find("notations")
                        notation.remove(articulation)
                        if len(notation) == 0:
                            node.remove(notation)
                    report["changes"].append({"part":pid,"measure":measure,"unit":unit,
                                              "character":character,"native_bounds":[x,y,w,h]})
    if not report["changes"]:
        return root, report
    report["state"] = "restored"
    return fixed, report
