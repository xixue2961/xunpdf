from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import json
import re

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control


NORMAL = {3: 2, 5: 4, 6: 4}
BEATS = {"eighth": Fraction(1, 2), "16th": Fraction(1, 4), "32nd": Fraction(1, 8)}


def _reading_agrees(reading, count):
    reliable = {item.get("mode") for item in reading.get("validated_readings", ())
                if item.get("text") == str(count) and item.get("confidence", 0) >= .95}
    return len(reliable - {None}) >= 2


def _same_source_head(note, native_note):
    return (note.find("pitch") is not None and native_note.find("pitch") is not None
            and note.get("default-x") is not None and note.get("default-x") == native_note.get("default-x")
            and all(note.findtext(f"pitch/{tag}", "0") == native_note.findtext(f"pitch/{tag}", "0")
                    for tag in ("step", "octave", "alter"))
            and note.findtext("staff", "1") == native_note.findtext("staff", "1")
            and (note.find("chord") is None) == (native_note.find("chord") is None))


def _exact_tuplet(keys, output, control, count, kind, side):
    if not keys or any(k not in output or k not in control for k in keys):
        return False
    events = [output[k] for k in keys]
    if not all(_same_source_head(e["node"], control[k]["node"]) for k, e in zip(keys, events, strict=True)):
        return False
    notes = [e["node"] for e in events]
    if len({(n.findtext("staff", "1"), n.findtext("voice", "1")) for n in notes}) != 1:
        return False
    normal = NORMAL[count]
    duration = BEATS[kind] * Fraction(normal, count)
    leads = [e for e in events if e["node"].find("chord") is None]
    if len(leads) != count or [e["start"] for e in leads] != [
        leads[0]["start"] + i * duration for i in range(count)
    ]:
        return False
    if any(e["duration"] != duration or n.findtext("type") != kind
           or n.find("grace") is not None or n.find("dot") is not None
           or n.findtext("time-modification/actual-notes") != str(count)
           or n.findtext("time-modification/normal-notes") != str(normal)
           or n.findtext("time-modification/normal-type", kind) != kind
           for e, n in zip(events, notes, strict=True)):
        return False
    if any(e["start"] not in {a["start"] for a in leads} for e in events):
        return False
    first, last = leads[0]["node"], leads[-1]["node"]
    marks = [(n, t) for n in notes for t in n.findall("notations/tuplet")]
    if len(marks) != 2 or marks[0][0] is not first or marks[1][0] is not last:
        return False
    start, end = marks[0][1], marks[1][1]
    if (start.get("type") != "start" or end.get("type") != "stop"
            or start.get("number", "1") != end.get("number", "1")
            or start.get("placement") != side or start.get("show-number") == "none"):
        return False
    return [e["node"].findtext("beam[@number='1']") for e in leads] == (
        ["begin"] + ["continue"] * (count - 2) + ["end"]
    )


def confirm_exported_tuplets(root, job, raster_source, findings):
    """Corroborate an old native warning against the original ink and final output."""
    if not findings:
        return findings, []
    job, raster_source = Path(job), Path(raster_source)
    evidence_file = job / "source-long-tuplets-pixels.json"
    if not evidence_file.is_file():
        return findings, []
    evidence = json.loads(evidence_file.read_text("utf8"))
    base = read_control(job) or fallback_control(job)
    if base is None:
        return findings, []
    archive, control, rows = base
    if sha(archive) != evidence.get("source_book_sha256"):
        return findings, []

    reads = defaultdict(list)
    for hit in evidence.get("beamed_findings", ()):
        reads[str(hit["unit"]), str(hit["system"]), str(hit["group"])].append(hit)
    native_anchors = defaultdict(list)
    for key, row in rows.items():
        native_anchors[row["unit"], row["system"], row["chord"]].append((key, row))
    piano = {part.get("id") for part in root.findall("part-list/score-part")
             if part.findtext("part-name", "").casefold() == "piano"}
    output, _ = _score_events(root)
    control_events, _ = _score_events(control)
    hashes, pending, confirmed = {}, [], []
    for finding in findings:
        sheet = re.fullmatch(r"sheet#([1-9][0-9]*)/sheet#\1\.xml", finding["source_sheet"])
        selected = _confirm_one(finding, sheet, reads, native_anchors, hashes, raster_source,
                                piano, output, control_events)
        if selected is None:
            pending.append(finding)
        else:
            confirmed.append({**finding, **selected, "status": "confirmed_in_final_export", "timing_changed": False})
    return pending, confirmed


def _confirm_one(finding, sheet, reads, anchors, hashes, raster_source, piano, output, control):
    if sheet is None:
        return None
    unit, system = sheet[1], str(finding["system"])
    matches = reads[unit, system, str(finding["beam_group"])]
    if len(matches) != 1:
        return None
    hit = matches[0]
    count, kind, side = hit.get("count"), hit.get("note_type"), hit.get("digit_placement")
    if (count not in NORMAL or str(count) != str(finding["digit"]) or kind not in BEATS
            or side not in ("above", "below") or str(hit.get("staff")) != str(finding["staff"])
            or sorted(hit.get("chords", ())) != sorted(finding["chords"]) or not _reading_agrees(hit, count)):
        return None
    if unit not in hashes:
        path = raster_source / f"unit-{int(unit):03d}.png"
        hashes[unit] = sha(path) if path.is_file() else None
    digest = hashes[unit]
    if not digest or digest != hit.get("source_sha256"):
        return None
    groups = [anchors[unit, system, chord] for chord in finding["chords"]]
    if any(not group for group in groups) or any(
        row["source_sha256"] != digest or row["staff"] != str(finding["staff"])
        for group in groups for _, row in group
    ):
        return None
    keys = sorted(key for group in groups for key, _ in group)
    if len({k[:2] for k in keys}) != 1 or keys[0][0] not in piano:
        return None
    if [k[2] for k in keys] != list(range(keys[0][2], keys[0][2] + len(keys))):
        return None
    if not _exact_tuplet(keys, output, control, count, kind, side):
        return None
    return {"part": keys[0][0], "measure": keys[0][1], "actual": count, "normal": NORMAL[count]}
