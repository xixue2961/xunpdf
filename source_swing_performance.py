"""Realize explicit 2:1 swing in the score copy used by the local MIDI renderer."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from xml.etree import ElementTree as ET


SWING_TYPES = {"eighth": (Fraction(1, 2), "quarter"), "16th": (Fraction(1, 4), "eighth")}


@dataclass
class Attack:
    start: Fraction
    divisions: Fraction
    position: int
    notes: list = field(default_factory=list)


def style(swing):
    if swing.find("straight") is not None:
        return None
    kind = swing.findtext("swing-type", "eighth")
    try:
        ratio = Fraction(int(swing.findtext("first")), int(swing.findtext("second")))
    except (ValueError, TypeError, ZeroDivisionError):
        return None
    return kind if ratio == 2 and kind in SWING_TYPES else None


def scan(measures):
    divisions = None
    groups, marks = [], []
    for index, measure in enumerate(measures):
        track = []
        cursor = Fraction(0)
        previous = None
        for pos, child in enumerate(measure):
            name = child.tag
            if name == "attributes" and child.find("divisions") is not None:
                divisions = Fraction(child.findtext("divisions"))
            if divisions is None or divisions <= 0:
                if name in ("note", "backup", "forward", "direction", "sound"):
                    break
                continue
            if name in ("backup", "forward"):
                jump = Fraction(child.findtext("duration")) / divisions
                cursor += jump if name == "forward" else -jump
            if name in ("direction", "sound"):
                sound = child if name == "sound" else child.find("sound")
                swing = sound.find("swing") if sound is not None else None
                if swing is not None:
                    offset = sound.find("offset")
                    if offset is None and name == "direction":
                        outer = child.find("offset")
                        if outer is not None and outer.get("sound") != "no":
                            offset = outer
                    at = cursor + Fraction(offset.text or "0") / divisions if offset is not None else cursor
                    marks.append(((index, at), style(swing)))
            if name != "note":
                previous = None
                continue
            if child.find("chord") is not None:
                if previous is not None:
                    previous.notes.append(child)
                continue
            previous = Attack(cursor, divisions, pos, [child])
            track.append(previous)
            if child.find("grace") is None:
                cursor += Fraction(child.findtext("duration", "0")) / divisions
        groups.append(track)
    return groups, marks


def eligible(attack, nominal, kind):
    for n in attack.notes:
        if (n.findtext("type") != kind or Fraction(n.findtext("duration", "0")) / attack.divisions != nominal
                or "attack" in n.attrib or "release" in n.attrib
                or any(n.find(tag) is not None for tag in
                       ("grace", "cue", "dot", "tie", "notations/tied", "time-modification",
                        "notations/ornaments", "notations/tuplet", "notations/fermata",
                        "notations/arpeggiate", "rest[@measure='yes']"))):
            return False
    return True


def set_playback_group(attack, written, amount, multiplier, boundary):
    length = int(amount * attack.divisions * multiplier)
    for n in attack.notes:
        n.find("type").text = written
        n.find("duration").text = str(length)
        stamp = ET.Element("time-modification")
        ET.SubElement(stamp, "actual-notes").text = "3"
        ET.SubElement(stamp, "normal-notes").text = "2"
        at = next((i for i, child in enumerate(n) if child.tag in
                   ("stem", "notehead", "notehead-text", "staff", "beam", "notations", "lyric", "play", "listen")), len(n))
        n.insert(at, stamp)
    first = attack.notes[0]
    notations = first.find("notations")
    if notations is None:
        notations = ET.Element("notations")
        at = next((i for i, child in enumerate(first) if child.tag in ("lyric", "play", "listen")), len(first))
        first.insert(at, notations)
    ET.SubElement(notations, "tuplet", type=boundary, number="16", bracket="no")


def realize_swing(root):
    """Mutate a private playback tree and return the number of adjusted pulses."""
    tracks = [p.findall("measure") for p in root.findall("part")]
    if not tracks or any([m.get("number") for m in track] != [m.get("number") for m in tracks[0]] for track in tracks):
        return 0
    markers, groups = defaultdict(set), []
    for track in tracks:
        attacks, marks = scan(track)
        groups.append(attacks)
        for when, kind in marks:
            markers[when].add(kind)
    if not markers or any(len(modes) != 1 for modes in markers.values()):
        return 0
    timing = sorted(markers)
    choices = {when: next(iter(markers[when])) for when in timing}

    selected = []
    for track, part in zip(tracks, groups, strict=True):
        for index, attacks in enumerate(part):
            for j, (a, b) in enumerate(zip(attacks, attacks[1:])):
                here = bisect_right(timing, (index, a.start)) - 1
                kind = choices[timing[here]] if here >= 0 else None
                if kind is None:
                    continue
                unit, long_type = SWING_TYPES[kind]
                pulse = 2 * unit
                if a.start < 0 or a.start % pulse or b.start != a.start + unit or a.divisions != b.divisions or a.divisions.denominator != 1:
                    continue
                if (here + 1 < len(timing) and timing[here + 1] < (index, a.start + pulse)):
                    continue
                if a.position + len(a.notes) != b.position:
                    between = track[index][a.position+len(a.notes):b.position]
                    if not (a.notes[0].find("rest") is not None
                            and any(c.start == 0 and c.notes[0].find("notations/fermata") is not None
                                    and c.notes[0].find('tie[@type="stop"]') is not None for c in attacks)
                            and len(between) == 1 and between[0].tag == "direction"
                            and between[0].find("direction-type/dynamics") is not None
                            and set(c.tag for c in between[0]) <= {"direction-type", "staff", "sound"}
                            and (between[0].find("sound") is None or
                                 (set(between[0].find("sound").attrib) == {"dynamics"} and
                                  not len(between[0].find("sound"))))):
                        continue
                if j and attacks[j - 1].start == a.start and attacks[j - 1].notes[0].find("grace") is not None:
                    continue
                voice_staff = {(n.findtext("voice", "1"), n.findtext("staff", "1")) for n in a.notes + b.notes}
                if len(voice_staff) != 1 or not (eligible(a, unit, kind) and eligible(b, unit, kind)):
                    continue
                selected.append((a, b, kind, long_type, pulse))
    if not selected:
        return 0

    multiplier = 3
    for child in root.iter():
        if child.tag in ("duration", "divisions", "offset") and child.text:
            child.text = format(Decimal(child.text) * multiplier, "f")
        for field_name in {"note": ("attack", "release"), "grace": ("make-time",), "sound": ("divisions",)}.get(child.tag, ()):
            if field_name in child.attrib:
                child.set(field_name, format(Decimal(child.get(field_name)) * multiplier, "f"))
    for sound in root.iter("sound"):
        swing = sound.find("swing")
        if swing is not None:
            sound.remove(swing)
    for a, b, kind, long_type, pulse in selected:
        set_playback_group(a, long_type, pulse * Fraction(2, 3), multiplier, "start")
        set_playback_group(b, kind, pulse / 3, multiplier, "stop")
    return len(selected)
