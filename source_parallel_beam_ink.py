"""Recover parallel beam timing when the original raster contains an omitted beam."""
from collections import defaultdict
from contextlib import ExitStack
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image
from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_scale_pitch import apply as apply_vector_pitch, open_source


def native_beams(system):
    nodes = {node.get("id"): node for node in system.findall("sig/inters/*")}
    children = defaultdict(list)
    stems = defaultdict(set)
    head_stems = defaultdict(set)
    mirrors = {}
    for relation in system.findall("sig/relations/relation"):
        left, right = relation.get("source"), relation.get("target")
        if relation.find("containment") is not None:
            children[left].append(right)
        elif relation.find("beam-stem") is not None:
            stems[left].add(right)
        elif relation.find("head-stem") is not None:
            head_stems[left].add(right)
        elif relation.find("mirror") is not None:
            mirrors[left], mirrors[right] = right, left
    groups = {}
    for key, node in nodes.items():
        if node.tag != "beam-group":
            continue
        beams = [nodes[b] for b in children[key] if nodes[b].tag == "beam"]
        if len(beams) not in (2, 3) or not all(stems[b.get("id")] for b in beams):
            continue
        common = stems[beams[0].get("id")]
        if all(stems[b.get("id")] == common for b in beams):
            groups[key] = beams, common
    return nodes, groups, head_stems, mirrors


def center(beam, x):
    start, end = beam.find("median/p1"), beam.find("median/p2")
    x1, y1 = float(start.get("x")), float(start.get("y"))
    x2, y2 = float(end.get("x")), float(end.get("y"))
    return y1 + (x-x1) * (y2-y1) / (x2-x1)


def prove_three_beams(gray, beams, columns):
    """Count thick parallel lines at two interior points per adjacent head pair."""
    first, last = sorted(beams, key=lambda b: center(b, columns[0]))[::len(beams)-1]
    gap = (center(last, columns[0])-center(first, columns[0]))/(len(beams)-1)
    height = float(first.get("height"))
    if len(beams) == 2 and 1.9*height <= gap <= 3.5*height:
        gap /= 2
    if not .95*height <= gap <= 1.75*height:
        return None
    votes = []
    for left, right in zip(columns, columns[1:]):
        if right-left < 2*height:
            return None
        matches = []
        for portion in (.37, .63):
            x = round(left+(right-left)*portion)
            y = center(first, x)
            top, bottom = round(y-1.1*gap), round(y+3.1*gap)
            margin = max(2, round(height/4))
            if top < 0 or bottom > gray.shape[0] or x-margin < 0 or x+margin >= gray.shape[1]:
                return None
            tile = gray[top:bottom, x-margin:x+margin+1]
            for threshold in (105, 145, 175):
                dark = np.flatnonzero((tile < threshold).mean(axis=1) >= .7)
                zones = np.split(dark, np.flatnonzero(np.diff(dark) > 1)+1)
                thick = [float(run.mean())+top-y for run in zones if .56*height <= len(run) <= 1.4*height]
                matches.append(len(thick)==3 and all(abs(pos-i*gap)<=.23*gap for i,pos in enumerate(thick)))
        votes.append(sum(matches) >= 5)
    if sum(votes) < .9*len(votes) or not all((votes[0], votes[-1])):
        return None
    return sum(votes)


def unique_lane(events, rows, groups, head_stems, voices, count):
    step = Fraction(1, 2**count)
    expected = set(range(1, count+1))
    note_type = {2: "16th", 3: "32nd"}[count]
    candidates = []
    for voice in voices:
        lane = [key for key in events if events[key]["node"].findtext("voice")==voice]
        if len(lane) < 8 or any(
            events[key]["start"]!=i*step or events[key]["duration"]!=step
            or events[key]["node"].findtext("type") != note_type
            or {int(b.get("number")) for b in events[key]["node"].findall("beam")}!=expected
            for i,key in enumerate(lane)
        ):
            continue
        for beams, stems in groups.values():
            anchors = [head_stems[rows[key]["head"]] & stems for key in lane]
            if len(beams)==count and all(len(s)==1 for s in anchors) and set.union(*anchors)==stems and len(lane)==len(stems):
                candidates.append((lane, beams))
    return candidates[0] if len(candidates)==1 else None


def prepare(measure, events, rows, directions, system):
    nodes, groups, head_stems, mirrors = native_beams(system)
    if {n.tag for n in measure} - {"note", "backup", "forward", "direction", "print", "attributes", "barline"}:
        return None
    if not set(events) <= rows.keys() or any(
        events[k]["node"].find("pitch") is None or events[k]["node"].find("chord") is not None
        or events[k]["node"].find("time-modification") is not None
        or rows[k]["shape"]!="NOTEHEAD_BLACK" or rows[k]["head"] not in nodes
        for k in events
    ):
        return None
    if any(n.tag in ("attributes","print") for n in measure) or any(
        n.get("location", "right") != "right" for n in measure.findall("barline")
    ) or any(
        n.find("direction-type/wedge") is None or n.find("offset") is not None
        for n in measure.findall("direction")
    ):
        return None
    voices = {v["node"].findtext("voice") for v in events.values()}
    slower = unique_lane(events, rows, groups, head_stems, voices, 2)
    faster = unique_lane(events, rows, groups, head_stems, voices, 3)
    if slower is None or faster is None:
        return None
    upper, upper_beams = slower
    lower, lower_beams = faster
    if len(upper)!=len(lower) or not set(upper).isdisjoint(lower):
        return None
    lower_by_head = {rows[k]["head"]:k for k in lower}
    duplicates = set(events)-set(upper)-set(lower)
    for key in duplicates:
        duplicate = events[key]
        node = duplicate["node"]
        head = rows[key]["head"]
        keeper = lower_by_head.get(mirrors.get(head))
        if (keeper is None or head_stems[head] & head_stems[rows[keeper]["head"]]
                or nodes[head].get("glyph")!=nodes[rows[keeper]["head"]].get("glyph")
                or duplicate["start"]!=events[keeper]["start"]
                or _music_signature(duplicate)[2]!=_music_signature(events[keeper])[2]
                or {n.tag for n in node}-{"pitch","duration","voice","type","stem","staff"}):
            return None
    columns = [[float(rows[k]["x"])+float(rows[k]["w"])/2 for k in lane] for lane in (upper,lower)]
    if any(abs(a-b) > .7*float(upper_beams[0].get("height")) for a,b in zip(*columns)):
        return None
    duration_ticks = int(events[lower[0]]["node"].findtext("duration"))
    if any(int(events[k]["node"].findtext("duration"))!=duration_ticks*factor
           for lane,factor in ((upper,2),(lower,1)) for k in lane):
        return None
    slots = {events[k]["start"]: i for i,k in enumerate(upper)}
    if any(direction["start"] not in slots for direction in directions):
        return None
    return upper, lower, upper_beams, lower_beams, columns, duplicates, duration_ticks, slots


def repair(measure, events, directions, plan, trailing_ticks):
    upper, lower, _, _, _, _, ticks, slots = plan
    marks = defaultdict(list)
    for direction in directions:
        marks[slots[direction["start"]]].append(deepcopy(direction["node"]))
    replacement = []
    for i,key in enumerate(upper):
        note = deepcopy(events[key]["node"])
        note.find("duration").text = str(ticks)
        note.find("type").text = "32nd"
        beam = note.findall("beam")[-1]
        extra = ET.Element("beam", number="3")
        extra.text = beam.text
        note.insert(list(note).index(beam)+1, extra)
        replacement.extend(marks[i])
        replacement.append(note)
    # Retain the existing barline and all notation attached to physical heads.
    new_backup = ET.Element("backup")
    ET.SubElement(new_backup, "duration").text = str(ticks*len(upper))
    replacement.append(new_backup)
    replacement.extend(deepcopy(events[k]["node"]) for k in lower)
    if trailing_ticks:
        forward = ET.Element("forward")
        ET.SubElement(forward, "duration").text = str(trailing_ticks)
        ET.SubElement(forward, "voice").text = replacement[-1].findtext("voice")
        ET.SubElement(forward, "staff").text = replacement[-1].findtext("staff")
        replacement.append(forward)
    replacement.extend(deepcopy(n) for n in measure.findall("barline"))
    result = deepcopy(measure)
    result[:] = replacement
    return result


def recover(root, job, source=None):
    job = Path(job)
    report = dict(state="unchanged", changes=[])
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    book, reference, ledger = base
    piano = {p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root, report
    events, directions = _score_events(root)
    reference_events = _score_events(reference)[0]
    scopes = defaultdict(dict)
    bar_lengths = defaultdict(dict)
    for key,event in events.items():
        if key[0] in piano:
            scopes[key[:2]][key] = event
        else:
            end = event["start"] + event["duration"]
            lengths = bar_lengths[key[1]]
            lengths[key[0]] = max(end, lengths.get(key[0], Fraction()))
    changes = {}
    vector = None
    attempted_vector = False
    with ZipFile(book) as accepted, ExitStack() as resources:
        for (part, bar), entries in scopes.items():
            if len(entries)<16 or any(
                k not in ledger or k not in reference_events or
                _music_signature(v)!=_music_signature(reference_events[k]) or
                v["node"].get("default-x")!=reference_events[k]["node"].get("default-x")
                for k,v in entries.items()
            ):
                continue
            source_scopes = {(ledger[k]["unit"],ledger[k]["system"],ledger[k]["source_sha256"]) for k in entries}
            if len(source_scopes)!=1:
                continue
            unit, system, digest = source_scopes.pop()
            s = graph(accepted,unit,system)
            if s is None:
                continue
            measure = next(iter(entries.values()))["measure"]
            marks = [d for d in directions if (d["part"],d["measure"])==(part,bar)]
            plan = prepare(measure,entries,ledger,marks,s)
            if plan is None:
                continue
            upper, lower, top, bottom, columns, duplicates, ticks, _ = plan
            playing_time = Fraction(len(lower),8)
            other_lengths = set(bar_lengths[bar].values())
            if len(other_lengths)>1:
                continue
            trailing = next(iter(other_lengths),playing_time) - playing_time
            trailing_ticks = trailing * ticks * 8
            if not 0 <= trailing <= 1 or trailing_ticks.denominator!=1:
                continue
            raster = job/f"unit-{int(unit):03d}.png"
            if not raster.is_file() or sha(raster)!=digest:
                continue
            with Image.open(raster) as image:
                gray = np.asarray(image.convert("L"))
            votes = [prove_three_beams(gray,b,x) for b,x in zip((top,bottom),columns,strict=True)]
            if None in votes:
                continue
            result = repair(measure,entries,marks,plan,int(trailing_ticks))
            pitch_proof = None
            try:
                if not attempted_vector:
                    attempted_vector = True
                    vector = open_source(source,job)
                    if vector is not None:
                        resources.callback(vector.close)
                if vector is not None:
                    test = deepcopy(result)
                    pitch_proof = apply_vector_pitch(vector,unit,root.find(f"part[@id='{part}']"),
                                                     measure,test,upper,lower,ledger)
                    if pitch_proof is not None:
                        result = test
            except (OSError, PdfiumError, ValueError, KeyError, TypeError, IndexError) as exc:
                report["vector_diagnostic"] = str(exc)
            changes[part,bar] = result
            report["changes"].append(dict(part=part,measure=bar,heads_per_lane=len(upper),
                                          pitch_proof=pitch_proof,
                                          note_type="32nd",source_sha256=digest,
                                          source_confirmed_gaps=votes,
                                          notes_length_in_quarters=str(playing_time),
                                          bar_length_in_quarters=str(playing_time+trailing),
                                          duplicate_heads=[ledger[k]["head"] for k in sorted(duplicates)]))
    if not changes:
        return root, report
    fixed = deepcopy(root)
    for part in fixed.findall("part"):
        for i,m in enumerate(part):
            replacement = changes.get((part.get("id"),m.get("number")))
            if replacement is not None and m.tag=="measure":
                part[i] = replacement
    report["state"] = "restored"
    return fixed,report

