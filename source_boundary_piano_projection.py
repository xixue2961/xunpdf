from collections import Counter
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import csv
import hashlib
import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_boundary_arc import edge_arc

from source_lyric_roles import _score_events
from source_native_notation import score

TIMELINE = {"note", "backup", "forward"}


def add_tie(note, mode):
    tie=ET.Element('tie',type=mode)
    at=list(note).index(note.find('duration'))+1
    note.insert(at,tie)
    notation=note.find('notations')
    if notation is None:
        notation=ET.Element('notations')
        index=next((i for i,n in enumerate(note) if n.tag in ('lyric','play','listen')),len(note))
        note.insert(index,notation)
    notation.insert(0,ET.Element('tied',type=mode,**({'orientation':'over'} if mode=='start' else {})))


def tsv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def divisions(measure):
    value = measure.findtext("attributes/divisions")
    if value is None:
        raise ValueError("source boundary lacks explicit MusicXML divisions")
    return int(value)


def meter(measure):
    native = measure.find("attributes/time")
    return Fraction(native.findtext("beats")) * 4 / Fraction(native.findtext("beat-type"))


def pitch(note):
    return tuple(note.findtext(f"pitch/{k}", "0") for k in ("step", "alter", "octave"))


def staff(note):
    return note.findtext("staff", "1")


def score_bar(root, part, number):
    bars = root.findall(f"part[@id='{part}']/measure[@number='{number}']")
    if len(bars) != 1:
        raise ValueError("ambiguous exported bar location")
    return bars[0]


def real_row(row):
    return row["repeat_copy"] == "false" and row["implicit"] == "false"


def signature(event):
    node = event["node"]
    return staff(node), event["start"], event["duration"], pitch(node), node.findtext("type")


def replace_timeline(current, repaired):
    if any(elem.tag not in TIMELINE | {"print", "attributes", "barline"} for elem in current):
        raise ValueError("timed caller context requires independent projection")
    attributes = lambda m: [ET.tostring(n) for n in m.findall("attributes/*") if n.tag != "divisions"]
    if attributes(current) != attributes(repaired):
        raise ValueError("native recovery and caller have different score context")
    ratio = Fraction(divisions(current), divisions(repaired))
    for child in list(current):
        if child.tag in TIMELINE:
            current.remove(child)
    at = next((i for i, n in enumerate(current) if n.tag == "barline"), len(current))
    for node in repaired:
        if node.tag not in TIMELINE:
            continue
        copied = deepcopy(node)
        ticks = Fraction(copied.findtext("duration", "0")) * ratio
        if ticks.denominator != 1:
            raise ValueError("repaired timing cannot be represented on caller tick grid")
        if copied.find("duration") is not None:
            copied.find("duration").text = str(ticks.numerator)
        current.insert(at, copied)
        at += 1


def source_staff(book, row, direction):
    unit = row["unit"]
    system = row["system"]
    sheet = ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml"))
    systems = sheet.findall("page/system")
    edge = systems[-1] if direction == "out" else systems[0]
    if edge.get("id") != system:
        raise ValueError("native notes do not lie on the required page edge")
    staff_nodes = edge.findall(f"part/staff[@id='{row['staff']}']")
    if len(staff_nodes) != 1:
        raise ValueError("native staff is ambiguous")
    return staff_nodes[0]


def erase_centerline(image, observation):
    erased = image.copy()
    xs = np.rint(np.linspace(*observation["columns"], len(observation["centerline"]))).astype(int)
    if observation["direction"] == "in":
        xs = xs[::-1]
    for x, y_float in zip(xs, observation["centerline"]):
        y = round(y_float)
        erased[max(0, y-5):y+6, x] = 255
    return erased


def source_pair(archive, images, left, right):
    if int(right["unit"]) != int(left["unit"]) + 1:
        raise ValueError("native endpoints are on non-adjacent pages")
    sides = []
    for row, direction in ((left, "out"), (right, "in")):
        path = images / f"unit-{int(row['unit']):03d}.png"
        if sha(path) != row["source_sha256"]:
            raise ValueError("source page differs from native ledger")
        with Image.open(path) as original:
            pixels = np.asarray(original.convert("L"))
        sides.append((row, direction, source_staff(archive, row, direction), pixels))
    evidence = [edge_arc(pixels, at, row, direction) for row, direction, at, pixels in sides]
    signs = {e["curve_sign"] for e in evidence if e}
    if len(signs) != 1:
        raise ValueError("paired source tie orientation is ambiguous")
    sign = signs.pop()
    for row, direction, at, pixels in sides:
        arc = edge_arc(pixels, at, row, direction, expected_sign=sign)
        if arc is None or edge_arc(erase_centerline(pixels, arc), at, row, direction, expected_sign=sign):
            raise ValueError("source tie lacks isolated arc evidence on one page")
    return "over" if sign < 0 else "under"


def project_boundary_repair(caller, control_dir, repaired_dir, meter_decisions, source):
    """Project a verified native endpoint repair atomically onto the cooked score."""
    control_dir, repaired_dir, meter_decisions, source = map(Path, (control_dir, repaired_dir, meter_decisions, source))
    removals = [r for r in tsv(meter_decisions) if r["status"] == "retired"]
    recovered_rests = tsv(repaired_dir / "rest-recovery.tsv")
    if not recovered_rests:
        return caller, []
    control_rows = [r for r in tsv(control_dir / "note-ledger.tsv") if real_row(r)]
    repaired_rows = [r for r in tsv(repaired_dir / "note-ledger.tsv") if real_row(r)]
    control_events, _ = _score_events(score(control_dir / "result.mxl"))
    repaired_tree = score(repaired_dir / "result.mxl")
    repaired_events, _ = _score_events(repaired_tree)
    caller_events, _ = _score_events(caller)
    repaired_caller = deepcopy(caller)
    reports = []

    for rest in recovered_rests:
        coord = rest["unit"], rest["system"], rest["staff"]
        rows = [r for r in repaired_rows if (r["unit"], r["system"], r["staff"]) == coord
                and r["head"] == rest["new_rest"] and r["shape"] == "EIGHTH_REST"]
        if len(rows) != 1:
            raise ValueError("native restored rest is not uniquely exported")
        part, number = rows[0]["part"], rows[0]["measure"]
        target_part = caller.find(f"part[@id='{part}']")
        scope = part, number
        control_bar = score_bar(caller, part, number)
        if control_bar.find("print[@new-page='yes']") is None:
            raise ValueError("exported target is not at the boundary")
        retired = {head for row in removals if (row["unit"], row["system"], row["staff"]) == coord
                   and row["stack"] == rest["stack"] for head in row["heads"].split(",")}
        old_rows = [r for r in control_rows if (r["part"], r["measure"]) == scope and r["native_note_class"] == "HeadInter"]
        if not retired or {r["head"] for r in old_rows} != retired:
            raise ValueError("caller region also contains genuine exported pitches")
        old_music = Counter(signature(control_events[part, number, int(r["note_index"])]) for r in old_rows)
        caller_music = Counter(signature(e) for k,e in caller_events.items() if k[:2] == scope and e["node"].find("pitch") is not None)
        if old_music != caller_music:
            raise ValueError("caller false-note identity differs from native control")
        for note in control_bar.findall("note"):
            quiet = note.find("rest")
            if quiet is not None and (quiet.get("measure") != "yes" or note.attrib or len(quiet)):
                raise ValueError("caller contains a real rest not covered by the repair")
        if control_bar.findall(".//lyric"):
            raise ValueError("piano projection would overwrite lyrics")

        exact_bar = score_bar(repaired_tree, part, number)
        updated = score_bar(repaired_caller, part, number)
        span = meter(exact_bar)
        if meter(control_bar) != span:
            raise ValueError("caller meter differs from recovered meter")
        new_bar_events = {k: e for k,e in repaired_events.items() if k[:2] == scope}
        for staff_number in ("1","2"):
            events = [e for e in new_bar_events.values() if staff(e["node"]) == staff_number and e["node"].find("chord") is None]
            clock = Fraction(0)
            for e in events:
                if e["start"] != clock:
                    raise ValueError("restored staff has overlapping notes or a gap")
                clock += e["duration"]
            if clock != span:
                raise ValueError("restored staff does not close at the printed meter")

        measures = target_part.findall("measure")
        previous = measures[measures.index(control_bar) - 1]
        previous_number = previous.get("number")
        previous_scope = part, previous_number
        previous_events = {k: e for k,e in caller_events.items() if k[:2] == previous_scope}
        end = max(e["start"] + e["duration"] for e in previous_events.values())
        tail = {(staff(e["node"]),pitch(e["node"])): (k,e) for k,e in previous_events.items()
                if e["start"] + e["duration"] == end and e["node"].find("pitch") is not None}
        head = {(staff(e["node"]),pitch(e["node"])): (k,e) for k,e in new_bar_events.items()
                if e["start"] == 0 and e["node"].find("pitch") is not None}
        if not tail or tail.keys() != head.keys():
            raise ValueError("source boundary pitch sets differ")

        replace_timeline(updated, exact_bar)
        updated_previous = score_bar(repaired_caller, part, previous_number)
        original_native_endpoints = {
            signature(repaired_events[r["part"],r["measure"],int(r["note_index"])]):r
            for r in repaired_rows if (r["part"],r["measure"]) == previous_scope and r["native_note_class"]=="HeadInter"}
        next_rows = {int(r["note_index"]):r for r in repaired_rows if (r["part"],r["measure"]) == scope}
        confirmed = []
        with ZipFile(repaired_dir / "refined.omr") as archive:
            for key,(k0,e0) in tail.items():
                k1,e1 = head[key]
                orientation = source_pair(archive, source, original_native_endpoints[signature(e0)], next_rows[k1[2]])
                a,b=updated_previous.findall("note")[k0[2]], updated.findall("note")[k1[2]]
                if any(n.find("tie") is not None or n.find("notations/tied") is not None for n in (a,b)):
                    raise ValueError("existing tie must be preserved")
                add_tie(a,"start")
                add_tie(b,"stop")
                for n in (a,b):
                    n.find("notations/tied").set("orientation",orientation)
                confirmed.append(dict(staff=key[0],pitch=key[1],orientation=orientation))
        reports.append(dict(part=part,previous=previous_number,measure=number,
                            retired_heads=len(retired),restored_heads=sum(e["node"].find("pitch") is not None for e in new_bar_events.values()),
                            source_rests=len([e for e in new_bar_events.values() if e["node"].find("rest") is not None]),
                            tied=confirmed))
    return repaired_caller,reports


