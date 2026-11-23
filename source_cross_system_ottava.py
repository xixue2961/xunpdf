"""Extend a confirmed 8va across a system break using its printed continuation."""
import csv
from copy import deepcopy
import json
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_partial_chord_ties import components


def runs(mask):
    indices = np.flatnonzero(mask)
    return [(int(v[0]), int(v[-1] + 1))
            for v in np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(v)]


def continuation_geometry(gray, first_x, first_top, next_x, interline):
    """Return a label crop and terminal hook only when all pixel views agree."""
    space = interline
    x0 = max(0, round(first_x - .65 * space))
    x1 = min(gray.shape[1], round(next_x - .2 * space))
    y0 = max(4, round(first_top - 7.5 * space))
    y1 = round(first_top - 3 * space)
    found = {}
    for y in range(y0, y1):
        matches = []
        for level in (128, 160, 208):
            stripe = gray[y - 3:y + 4, x0:x1] < level
            dashes = [(x0+a, x0+b) for a, b in runs(stripe.sum(axis=0) >= 2)
                      if .32 * space <= b - a <= .81 * space]
            if len(dashes) < 3 or not -.75 * space <= dashes[0][0] - first_x <= .8 * space:
                break
            if any(not .85 * space <= b[0]-a[0] <= 1.26 * space
                   for a, b in zip(dashes, dashes[1:])):
                break
            end = dashes[-1][1]
            # The downward hook begins beside the last dash and remains
            # vertically contiguous. A dashed hairpin or missing hook fails.
            hook_start = end
            hook_end = min(x1, round(end + .6 * space))
            hook = gray[y - 3:round(y + 1.85 * space), hook_start:hook_end] < level
            columns = runs(hook.sum(axis=0) >= round(1.05 * space))
            if len(columns) != 1:
                break
            a, b = columns[0]
            vertical = runs(np.any(hook[:,a:b], axis=1))
            if not .10 * space <= b-a <= .36 * space or len(vertical) != 1:
                break
            top, bottom = vertical[0]
            if abs(top - 3) > .25 * space or not 1.05*space <= bottom-top <= 1.72*space:
                break
            matches.append((dashes, hook_start+a, y - 3 + top, y - 3 + bottom))
        if len(matches) != 3:
            continue
        middle = matches[1]
        for other in (matches[0], matches[2]):
            if len(other[0]) != len(middle[0]) or any(
                    max(abs(a-b), abs(c-d)) > 2 for (a,c),(b,d) in zip(middle[0],other[0])):
                break
            if max(abs(a-b) for a,b in zip(middle[1:],other[1:])) > 2:
                break
        else:
            label_end = middle[0][0][0] - 1
            label_start = max(0, round(label_end - 2.5 * space))
            clip = (label_start, round(y-.45*space), label_end, round(y+1.55*space))
            region = gray[clip[1]:clip[3],clip[0]:clip[2]]
            if np.count_nonzero(region<128) < .45*space*space:
                continue
            key = (tuple(d[0] for d in middle[0]), middle[1])
            found.setdefault(key, []).append(dict(dashes=middle[0], hook=middle[1:], label_crop=clip))
    if len(found) == 1:
        candidates, = found.values()
        if len(candidates) >= 2:
            return candidates[len(candidates)//2]
    return None


def read_pixels(path):
    with Image.open(path) as image:
        return np.asarray(image.convert("L"))


def glyphs(tile, threshold, min_pixels):
    result = []
    for pixels in components(tile < threshold):
        if len(pixels) < min_pixels:
            continue
        yy, xx = map(np.asarray, zip(*pixels))
        mask = np.zeros((yy.max() - yy.min() + 1, xx.max() - xx.min() + 1), dtype=bool)
        mask[yy - yy.min(), xx - xx.min()] = True
        result.append((int(xx.min()), int(yy.min()), mask))
    return sorted(result, key=lambda item: item[0])


def digit_agreement(previous_label, continuation_label, space):
    """Compare the 8 with the verified prefix and require separate mirrored parentheses."""
    scores = []
    for threshold in (128, 160, 208):
        previous = glyphs(previous_label, threshold, .03 * space**2)
        current = glyphs(continuation_label, threshold, .03 * space**2)
        if len(previous) < 2 or len(current) != 3:
            return None
        _, _, template = previous[0]
        (lx, ly, left), (mx, my, middle), (rx, ry, right) = current
        h, w = middle.shape
        if not .7 * space <= w <= 1.25 * space or not 1.1 * space <= h <= 1.85 * space:
            return None
        for x, y, sign, mark in ((lx, ly, 1, left), (rx, ry, -1, right)):
            mh, mw = mark.shape
            if not .70*h <= mh <= 1.15*h or not .16*w <= mw <= .56*w or abs(y-my) > .18*h:
                return None
            bands = np.array_split(mark, 3)
            positions = [np.mean(np.where(band)[1]) for band in bands]
            if min(sign * (tip-positions[1]) for tip in (positions[0],positions[2])) < .075*mw:
                return None
        if lx + left.shape[1] > mx or mx + w > rx:
            return None

        def normalized(mask):
            image = Image.fromarray(mask)
            return np.asarray(image.resize((36, 52), Image.Resampling.NEAREST))

        a, b = normalized(template), normalized(middle)
        overlap = np.count_nonzero(a & b) / np.count_nonzero(a | b)
        if overlap < .90:
            return None
        scores.append(round(float(overlap), 3))
    return scores


def following_system(archive, unit, system_id):
    def load(number):
        filename = f"sheet#{number}/sheet#{number}.xml"
        return ET.fromstring(archive.read(filename)) if filename in archive.namelist() else None

    current = load(unit)
    if current is None:
        return None
    systems = current.findall("page/system")
    ix = next((i for i, s in enumerate(systems) if s.get("id") == system_id), None)
    if ix is None:
        return None
    if ix + 1 < len(systems):
        return systems[ix], systems[ix+1], current, unit
    later = load(unit+1)
    if later is None:
        return None
    following = later.find("page/system")
    return (systems[ix], following, later, unit+1) if following is not None else None


def source_stacks(system):
    return [n for n in system.findall("stack")
            if not (n.get("special") == "CAUTIONARY" and n.get("duration") == "0")]


def midpoint(row):
    return int(row["x"]) + int(row["w"])/2


def end_is_open(gray, span, boundary, space):
    end = span["normal_dashes"][-1][1]
    y = span["center_y"]
    if not 0 < boundary - end <= 2.25 * space:
        return False
    # A hook beside the last dash closes this span before the system break.
    tile = gray[y-2:round(y+1.4*space), end-2:min(boundary, round(end+.65*space))]
    return tile.size > 0 and not np.any((tile < 160).sum(axis=0) >= .65*space)


def gather_evidence(job, folder, stage):
    scan = json.loads((folder/"ottava.json").read_text("utf8"))
    span = scan["span"]
    if span is None or scan["book_sha256"] != stage["source_book_sha256"]:
        return None
    archive = folder/"ottava/refined.omr"
    if sha(archive) != stage["output_book_sha256"]:
        return None
    source_unit, system_id, staff, stack, _, _, *tail = (folder/"ottava.tsv").read_text("utf8").strip().split("	")
    unit = int(source_unit)
    old_image = job/f"unit-{unit:03d}.png"
    if sha(old_image) != tail[-1]:
        return None
    with ZipFile(archive) as native:
        result = following_system(native, unit, system_id)
    if result is None:
        return None
    prior, following, sheet, following_unit = result
    old_stacks = source_stacks(prior)
    new_stacks = source_stacks(following)
    if not old_stacks or not new_stacks or old_stacks[-1].get("id") != stack:
        return None
    old_part = next((p for p in prior.findall("part") if p.find(f"staff[@id='{staff}']") is not None), None)
    if old_part is None:
        return None
    new_part = following.find(f"part[@id='{old_part.get('id')}']")
    if new_part is None:
        return None
    old_staffs, new_staffs = (p.findall("staff") for p in (old_part, new_part))
    if len(old_staffs) != 2 or len(new_staffs) != 2 or old_staffs[0].get("id") != staff:
        return None
    space = int(sheet.find("scale/interline").get("main"))
    previous = read_pixels(old_image)
    if not end_is_open(previous, span, int(old_stacks[-1].get("right")), space):
        return None
    notes = folder/"ottava/note-ledger.tsv"
    with notes.open(encoding="utf8") as stream:
        ledger = list(csv.DictReader(stream, delimiter="	"))
    def select(row, u, system, part):
        return (row["unit"],row["system"],row["native_part"],row["repeat_copy"],row["implicit"]) == (
            str(u), system.get("id"), part.get("id"), "false", "false")
    old_rows = [r for r in ledger if select(r, unit, prior, old_part) and r["chord"]==span["last_chord"]]
    if not old_rows or len({(r["part"],r["measure"]) for r in old_rows}) != 1:
        return None
    note_ids = new_part.find(f"measure[@id='{new_stacks[0].get('id')}']")
    if note_ids is None:
        return None
    chord_ids = set(note_ids.findtext("head-chords","").split())
    new_rows = [r for r in ledger if select(r, following_unit, following, new_part) and r["chord"] in chord_ids]
    staff_rows = [r for r in new_rows if r["staff"] == new_staffs[0].get("id")]
    if len(staff_rows) < 2:
        return None
    chord = min(staff_rows, key=midpoint)["chord"]
    heads = [r for r in staff_rows if r["chord"]==chord]
    subsequent = [r for r in staff_rows if r["chord"] != chord]
    if not subsequent or not all(r["shape"] in ("NOTEHEAD_BLACK", "NOTEHEAD_VOID", "WHOLE_NOTE") for r in heads):
        return None
    first_x = median(map(midpoint, heads))
    next_x = min(map(midpoint, subsequent))
    top = min(int(r["y"]) for r in heads)
    image = job/f"unit-{following_unit:03d}.png"
    digest = sha(image)
    if any(r["source_sha256"] != digest for r in new_rows):
        return None
    pixels = read_pixels(image)
    geometry = continuation_geometry(pixels, first_x, top, next_x, space)
    if geometry is None or geometry["hook"][0] <= max(map(midpoint, heads)):
        return None
    x,y,u,v = span["label_clip_xyxy"]
    a,b,c,d = geometry["label_crop"]
    agreement = digit_agreement(previous[y:v,x:u], pixels[b:d,a:c], space)
    if agreement is None:
        return None
    end_x = geometry["hook"][0]
    return old_rows[0], heads, [r for r in new_rows if midpoint(r)>end_x], dict(
        page=following_unit, system=following.get("id"), source_sha256=digest,
        chord=chord, sign_agreement=agreement, **geometry)


def address(row):
    return row["part"],row["measure"],int(row["note_index"])


def unchanged_note(before, after):
    def signature(event):
        node=event["node"]
        return (event["start"],event["duration"],node.get("default-x"),
                tuple(node.findtext("pitch/"+k,"0") for k in ("step","alter","octave")),
                node.findtext("staff","1"),node.findtext("voice"))
    return signature(before)==signature(after)


def project(root, original, evidence):
    old_row, heads, trailing, details = evidence
    current = deepcopy(root)
    live = _score_events(current)[0]
    native = _score_events(original)[0]
    part_id, bar, _ = address(heads[0])
    old_part, old_bar, _ = address(old_row)
    part = current.find(f"part[@id='{part_id}']")
    if part is None or old_part != part_id or old_bar == bar:
        return root, None
    measures = part.findall("measure")
    previous = [m for m in measures if m.get("number")==old_bar]
    following = [m for m in measures if m.get("number")==bar]
    if len(previous)!=1 or len(following)!=1:
        return root, None
    left, right = previous[0], following[0]
    if measures.index(right) != measures.index(left)+1:
        return root, None
    stops=[d for d in left.findall("direction")
           if d.find("direction-type/octave-shift[@type='stop'][@size='8']") is not None
           and d.findtext("staff","1")=="1"]
    starts = [d for d in left.findall("direction")
              if d.find("direction-type/octave-shift[@type='down'][@size='8']") is not None
              and d.findtext("staff","1")=="1"]
    prior_start = starts[0].find("direction-type/octave-shift") if len(starts)==1 else None
    prior_stop = stops[0].find("direction-type/octave-shift") if len(stops)==1 else None
    if (prior_start is None or prior_stop is None
            or prior_start.get("number","1") != prior_stop.get("number","1")
            or right.find("direction/direction-type/octave-shift") is not None):
        return root, None
    head_events=[]
    for row in heads:
        key=address(row)
        if key not in live or key not in native or not unchanged_note(native[key],live[key]):
            return root, None
        head_events.append(live[key])
    if any(event["start"]!=0 or event["duration"]!=head_events[0]["duration"] for event in head_events):
        return root, None
    nodes=[event["node"] for event in head_events]
    chord_nodes=right.findall("note")[:len(nodes)]
    if nodes!=chord_nodes or any(n.find("pitch") is None or n.find("tie") is not None for n in nodes):
        return root,None
    after_heads=[native[key]["start"] for row in trailing if (key:=address(row)) in native and native[key]["start"]>0]
    if not after_heads:
        return root, None
    div = right.findtext("attributes/divisions")
    if not div:
        return root, None
    delta=(min(after_heads)-head_events[0]["duration"])*int(div)
    if delta.denominator!=1 or delta>=0:
        return root, None
    stop=stops[0]
    if stop.find("offset") is not None:
        return root,None
    continuing=deepcopy(stop)
    continuing.find("direction-type/octave-shift").set("type","continue")
    left.remove(stop)
    right.insert(list(right).index(nodes[0]),continuing)
    offset=ET.Element("offset")
    offset.text=str(delta.numerator)
    stop.insert(list(stop).index(stop.find("staff")),offset)
    right.insert(list(right).index(nodes[-1])+1,stop)
    for n in nodes:
        o=n.find("pitch/octave")
        o.text=str(int(o.text)+1)
    return current, dict(part=part_id, preceding_measure=old_bar, measure=bar, continued_notes=len(nodes), **details)


def recover(root, job):
    job = Path(job)
    audit = job/"source-native-notation.json"
    if not audit.is_file():
        return root,dict(state="not_applicable",changes=[])
    report = json.loads(audit.read_text("utf8"))
    confirmed = [sub for step in report["stages"] if step["stage"]=="piano-polyphony" and step.get("source_verified_reload")
                 for sub in step["substeps"] if sub["stage"]=="ottava"]
    if len(confirmed)!=1:
        return root,dict(state="not_applicable",changes=[])
    folder=job/"source-native-notation/piano-polyphony"
    evidence=gather_evidence(job, folder, confirmed[0])
    if evidence is None:
        return root,dict(state="not_applicable",changes=[])
    output, change=project(root, score(folder/"ottava/result.mxl"), evidence)
    if change is None:
        return root,dict(state="unresolved",changes=[],reason="source anchors differ from final score")
    return output,dict(state="restored",changes=[change])
