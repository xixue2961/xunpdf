"""Recover source-proven paired piano rhythm in the final score."""
import copy
import csv
from collections import defaultdict
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dot_at_threshold(image, bounds, threshold, scale):
    x, y, right, bottom = bounds
    patch = image.crop(bounds)
    pixels = patch.load()
    remaining = {(i, j) for j in range(patch.height) for i in range(patch.width)
                 if pixels[i, j] < threshold}
    dots = []
    while remaining:
        component = [remaining.pop()]
        for a, b in component:
            for i in range(a - 1, a + 2):
                for j in range(b - 1, b + 2):
                    if (i, j) in remaining:
                        remaining.remove((i, j))
                        component.append((i, j))
        xs, ys = zip(*component)
        w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if (0.24 * scale <= w <= 0.5 * scale and 0.2 * scale <= h <= 0.5 * scale
                and len(component) >= 0.50 * w * h and max(w, h) <= 1.55 * min(w, h)
                and min(xs) > 0 and max(xs) < patch.width - 1
                and min(ys) > 0 and max(ys) < patch.height - 1):
            dots.append((x + min(xs) + w / 2, y + min(ys) + h / 2))
    return dots[0] if len(dots) == 1 else None


def _paired_dots(image, rows):
    if len(rows) != 2:
        return None
    heads = sorted((tuple(int(row[k]) for k in ("x", "y", "w", "h")) for row in rows),
                   key=lambda rect: rect[1])
    px = max(x + w for x, y, w, h in heads)
    scale = sum(w for x, y, w, h in heads) / 2
    if abs(heads[0][0] - heads[1][0]) > .15 * scale or heads[1][1] - heads[0][1] < 1.7 * scale:
        return None
    centers = []
    for x, y, w, h in heads:
        bounds = (px + round(.10 * scale), y - round(.23 * h),
                  px + round(.91 * scale), y + h + round(.23 * h))
        if not (0 <= bounds[0] < bounds[2] <= image.width and 0 <= bounds[1] < bounds[3] <= image.height):
            return None
        samples = [_dot_at_threshold(image, bounds, t, scale) for t in (150, 175, 195)]
        if any(v is None for v in samples):
            return None
        if any(abs(x - samples[1][0]) > .10 * scale or abs(y - samples[1][1]) > .10 * scale
               for x, y in samples):
            return None
        centers.append(samples[1])
    return centers if abs(centers[0][0] - centers[1][0]) <= .12 * scale else None


def _x(group):
    return sum(int(row["x"]) + int(row["w"]) / 2 for row in group) / len(group)


def _rhythm(nodes, events, keys, staff, wanted):
    leaders = [(i, n) for i, n in enumerate(nodes)
               if n.findtext("staff") == staff and n.find("chord") is None]
    running = Fraction()
    for index, node in leaders:
        event = events[keys[index]]
        if event["start"] != running:
            return False
        running += event["duration"]
    return running == wanted


def _trial(root, events, native, address, groups, flag, source, meter, divisions):
    part, number, unit, system = address
    keys = [(part, number, i) for i in range(sum(map(len, groups)))]
    if any(k not in events or k not in native or _music_signature(events[k]) != _music_signature(native[k]) for k in keys):
        return None
    measure = events[keys[0]]["measure"]
    nodes = measure.findall("note")
    if len(nodes) != len(keys) or measure.find("attributes/time") is not None:
        return None
    indices = [[int(row["note_index"]) for row in group] for group in groups]
    if [i for batch in indices for i in batch] != list(range(len(keys))):
        return None
    for group, batch in zip(groups, indices):
        staff = group[0]["staff_number"]
        if not all(row["staff_number"] == staff and row["shape"] == "NOTEHEAD_BLACK" for row in group):
            return None
        if any(n.find("pitch") is None or n.findtext("staff") != staff
               or (n.find("chord") is not None) != (pos > 0)
               for pos, n in enumerate(nodes[i] for i in batch)):
            return None
    upper = [i for i, g in enumerate(groups) if g[0]["staff_number"] == "1"]
    lower = [i for i, g in enumerate(groups) if g[0]["staff_number"] == "2"]
    flag_groups = [i for i in upper if groups[i][0]["chord"] == flag["chord"]]
    if len(flag_groups) != 1 or len(lower) < 4 or upper + lower != list(range(len(groups))):
        return None
    target = flag_groups[0]
    dotted, first, last = lower[-3:]
    head_width = int(groups[dotted][0]["w"])
    tolerance = head_width * .55
    if abs(_x(groups[target]) - _x(groups[dotted])) > tolerance:
        return None
    if any(abs(_x(groups[a]) - _x(groups[b])) > tolerance for a, b in zip(upper[-2:], lower[-2:])):
        return None
    if any(_x(groups[b]) <= _x(groups[a]) for staff in (upper, lower) for a, b in zip(staff, staff[1:])):
        return None
    if sorted(row["head"] for row in groups[target]) != sorted(row["id"] for row in flag["heads"]):
        return None
    leads = [nodes[batch[0]] for batch in indices]
    starts = [events[keys[batch[0]]]["start"] for batch in indices]
    times = [events[keys[batch[0]]]["duration"] for batch in indices]
    if starts[target] != starts[dotted] or times[target] != 1 or times[dotted] != 1:
        return None
    if any(nodes[n].findtext("type") != "quarter" or nodes[n].find("dot") is not None
           or nodes[n].find("beam") is not None or nodes[n].find("time-modification") is not None
           for group in (target, dotted) for n in indices[group]):
        return None
    if not all(times[i] == Fraction(1, 4) and leads[i].findtext("type") == "16th"
               and leads[i].find("beam") is not None for i in (first, last)):
        return None
    if starts[first] != meter or starts[last] != meter + Fraction(1, 4) or starts[dotted] + Fraction(3, 2) != meter - Fraction(1, 2):
        return None
    beats = meter + Fraction(1, 2)
    if not _rhythm(nodes, events, keys, "1", beats):
        return None
    main_voice = leads[dotted].findtext("voice")
    tail_voice = leads[first].findtext("voice")
    if not main_voice or not tail_voice or main_voice == tail_voice:
        return None
    if any(nodes[n].findtext("voice") != (main_voice if i < first else tail_voice)
           for i in lower for n in indices[i]):
        return None
    cursor = Fraction()
    for i in lower[:-2]:
        if starts[i] != cursor:
            return None
        cursor += times[i]

    backups = measure.findall("backup")
    forwards = measure.findall("forward")
    if len(backups) != 2 or len(forwards) != 1:
        return None
    change = copy.deepcopy(measure)
    cnotes = change.findall("note")
    cb = change.findall("backup")
    cf = change.find("forward")
    items = list(change)
    lower_section = items[items.index(cb[0]) + 1:]
    if any(item.tag not in ("note", "backup", "forward", "barline") for item in lower_section):
        return None
    if any(nodes[n].find("tie") is not None or nodes[n].find("notations/tied") is not None
           for i in lower for n in indices[i]):
        return None
    if items.index(cb[0]) != items.index(cnotes[indices[lower[0]][0]]) - 1:
        return None
    if items.index(cb[1]) != items.index(cnotes[indices[first][0]]) - 2 or items[items.index(cb[1]) + 1] is not cf:
        return None
    durations = [Fraction(e.findtext("duration")) / divisions for e in (*cb, cf)]
    if durations != [beats, cursor, meter]:
        return None
    # Require independent source pixels on both staves before changing timing.
    source_bytes = (source / f"unit-{unit:03d}.png").read_bytes()
    source_hash = sha256(source_bytes).hexdigest()
    if {row["source_sha256"] for group in groups for row in group} != {source_hash}:
        return None
    with Image.open(BytesIO(source_bytes)) as original:
        dots = _paired_dots(original.convert("L"), groups[dotted])
    if not dots:
        return None

    for n in indices[target]:
        cnotes[n].find("type").text = "eighth"
        cnotes[n].find("duration").text = str(divisions // 2)
    for n in indices[dotted]:
        node = cnotes[n]
        node.find("duration").text = str(3 * divisions // 2)
        node.insert(list(node).index(node.find("type")) + 1, ET.Element("dot"))
    for n in indices[first] + indices[last]:
        cnotes[n].find("voice").text = main_voice
    cb[0].find("duration").text = str(int(meter * divisions))
    change.remove(cb[1])
    change.remove(cf)
    p = root.find(f"part[@id='{part}']")
    location = list(p).index(measure)
    p.remove(measure)
    p.insert(location, change)
    checked, _ = _score_events(root)
    if not all(_rhythm(cnotes, checked, keys, staff, meter) for staff in ("1", "2")):
        p.remove(change)
        p.insert(location, measure)
        return None
    return {"part": part, "measure": number, "flag_chord": flag["chord"],
            "printed_dot_centers": dots, "quarter_beats": str(meter)}


def refine_paired_duration(root, job):
    """Reuse same-job flag, meter and physical note evidence, leaving unsupported bars intact."""
    job = Path(job)
    flags_path = job / "source-native-rhythm/flags/missing-flags.json"
    native_path = job / "source-native-notation.json"
    meters_path = job / "source-tuplet-evidence/meter/source-meters.json"
    if not all(p.is_file() for p in (flags_path, native_path, meters_path)):
        return root, {"state": "insufficient_evidence", "changes": []}
    flags = _load(flags_path)
    candidates = {}
    for f in flags["findings"]:
        match = f["matches"][0]
        if match["reference"]["shape"] not in ("FLAG_1", "FLAG_1_DOWN"):
            continue
        alternatives = f["matches"][1:]
        decisive_single_page = (
            len(flags["source_images"]) == 1
            and len(alternatives) >= 2
            and match["quality"] >= .92
            and match["mask_fraction"] >= .60
            and all(match["quality"] - item["quality"] >= .12 for item in alternatives)
        )
        if (match["quality"] >= .98 and match["mask_fraction"] >= .55) or decisive_single_page:
            candidates[(f["unit"], f["system"], f["chord"])] = f
    if not candidates:
        return root, {"state": "no_changes", "changes": []}
    native_info = _load(native_path)
    control = native_info.get("accepted_native_xml")
    if not control:
        return root, {"state": "insufficient_evidence", "changes": []}
    control = Path(control)
    ledger = control.with_name("head-ledger.tsv")
    if not control.is_file() or not ledger.is_file():
        return root, {"state": "insufficient_evidence", "changes": []}

    by_measure = defaultdict(list)
    with ledger.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row["staff_number"] in ("1", "2") and row["repeat_copy"] == "false":
                by_measure[row["part"], row["measure"], int(row["unit"]), row["system"]].append(row)
    meter_info = _load(meters_path)
    printed = {(s["unit"], s["system"]): s["source_columns"] for s in meter_info["results"]}
    image_hashes = {s["unit"]: s["image_sha256"] for s in meter_info["source_images"]}
    native, _ = _score_events(score(control))
    fixed = copy.deepcopy(root)
    events, _ = _score_events(fixed)
    clocks = {}
    for p in fixed.findall("part"):
        meter, divisions = None, 1
        for m in p.findall("measure"):
            n = m.findtext("attributes/divisions")
            if n:
                divisions = int(n)
            time = m.find("attributes/time")
            if time is not None:
                beats, kind = time.findall("beats"), time.findall("beat-type")
                meter = f"{beats[0].text}/{kind[0].text}" if len(beats) == len(kind) == 1 else None
            clocks[p.get("id"), m.get("number")] = meter, divisions

    changes = []
    for address, rows in by_measure.items():
        part, number, unit, system = address
        if not any(row["staff_number"] == "2" for row in rows):
            continue
        meters, divisions = clocks.get((part, number), (None, 0))
        if not meters or not divisions or divisions % 2 or image_hashes.get(unit) != flags["source_images"].get(str(unit)):
            continue
        left = min(int(r["x"]) for r in rows)
        columns = [c for c in printed.get((unit, system), []) if c["x"] < left]
        if not columns:
            continue
        active_column = max(columns, key=lambda c: c["x"])
        if active_column["time"] != meters or len({v["staff"] for v in active_column["votes"]}) < 2:
            continue
        numerator, denominator = meters.split("/")
        if not numerator.isdecimal() or not denominator.isdecimal() or not int(denominator):
            continue
        bar_length = Fraction(int(numerator) * 4, int(denominator))
        if (bar_length * divisions).denominator != 1:
            continue
        clusters = defaultdict(list)
        for row in sorted(rows, key=lambda r: int(r["note_index"])):
            clusters[row["chord"]].append(row)
        targets = [candidates[unit, system, chord] for chord in clusters if (unit, system, chord) in candidates]
        if len(targets) != 1:
            continue
        change = _trial(fixed, events, native, address, list(clusters.values()), targets[0], job, bar_length, divisions)
        if change:
            changes.append(change)
            events, _ = _score_events(fixed)
    return (fixed if changes else root), {"state": "restored" if changes else "no_changes", "changes": changes}
