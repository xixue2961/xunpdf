"""Keep explicit mid-measure MusicXML tempi at their written MIDI ticks."""

from collections import defaultdict
from fractions import Fraction
from io import BytesIO
from xml.etree import ElementTree as ET


MEI = "{http://www.music-encoding.org/ns/mei}"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def printed_tempi(score):
    tracks = [part.findall("measure") for part in score.findall("part")]
    if not tracks or any(len(track) != len(tracks[0]) for track in tracks):
        return 0, {}
    marks = defaultdict(set)
    for track in tracks:
        divisions = Fraction(1)
        for index, measure in enumerate(track):
            cursor = Fraction()
            for child in measure:
                if child.tag == "attributes":
                    divisions = Fraction(child.findtext("divisions", str(divisions)))
                elif child.tag in ("backup", "forward", "note"):
                    if child.find("chord") is None and child.find("grace") is None:
                        amount = Fraction(child.findtext("duration", "0")) / divisions
                        cursor += -amount if child.tag == "backup" else amount
                elif child.tag in ("direction", "sound"):
                    sound = child if child.tag == "sound" else child.find("sound")
                    if sound is None or sound.get("tempo") is None:
                        continue
                    bpm = Fraction(sound.get("tempo"))
                    if bpm <= 0:
                        continue
                    offset = sound.find("offset")
                    if offset is None and child.tag == "direction":
                        outer = child.find("offset")
                        if outer is not None and outer.get("sound") != "no":
                            offset = outer
                    onset = cursor + Fraction(offset.text) / divisions if offset is not None else cursor
                    marks[index].add((onset, bpm))
    return len(tracks[0]), marks


def read_vlq(stream):
    value = 0
    for _ in range(4):
        byte = stream.read(1)
        if not byte:
            break
        value = (value << 7) | (byte[0] & 127)
        if byte[0] < 128:
            return value
    raise ValueError("Invalid MIDI variable length value")


def write_vlq(value):
    result = bytearray([value & 127])
    value >>= 7
    while value:
        result.insert(0, (value & 127) | 128)
        value >>= 7
    return bytes(result)


def read_conductor(track):
    stream = BytesIO(track)
    tick, events = 0, []
    while stream.tell() < len(track):
        tick += read_vlq(stream)
        tag = stream.read(2)
        if len(tag) != 2 or tag[0] != 255:
            raise ValueError("Unexpected event in the MIDI conductor track")
        size = read_vlq(stream)
        value = stream.read(size)
        if len(value) != size:
            raise ValueError("Truncated MIDI conductor event")
        events.append((tick, tag, value))
    if not events or events[-1][1:] != (b"\xff\x2f", b""):
        raise ValueError("Missing MIDI conductor end event")
    return events


def preserve_tempo_positions(midi, data, renderer):
    """Use the renderer's actual bar clock, retaining every note track byte."""
    if "tempo=" not in data:
        return midi
    count, marks = printed_tempi(ET.fromstring(data))
    timed = {index: next(iter(tempi)) for index, tempi in marks.items()
             if len(tempi) == 1 and next(iter(tempi))[0] > 0}
    if not timed:
        return midi

    if midi[:10] != b"MThd\x00\x00\x00\x06\x00\x01" or midi[14:18] != b"MTrk":
        raise ValueError("无法核对 MIDI 的拍内变速。")
    ppq = int.from_bytes(midi[12:14], "big")
    if not 0 < ppq < 32768:
        raise ValueError("无法核对 MIDI 的拍内时钟。")
    end = 22 + int.from_bytes(midi[18:22], "big")
    events = read_conductor(midi[22:end])

    measures = ET.fromstring(renderer.getMEI()).findall(".//" + MEI + "measure")
    ticks = [(event["measureOn"], round(Fraction(str(event["qstamp"])) * ppq))
             for event in renderer.renderToTimemap({"includeMeasures": True}) if "measureOn" in event]
    if len(measures) != count or not ticks:
        return midi
    starts = defaultdict(list)
    for index, (measure_id, tick) in enumerate(ticks):
        following = ticks[index + 1][1] if index + 1 < len(ticks) else None
        starts[measure_id].append((tick, following))

    tempo_tag = b"\xff\x51"
    replacements = {}
    for index, (onset, bpm) in timed.items():
        measure = measures[index]
        native_tempi = measure.findall(MEI + "tempo")
        if len(native_tempi) != 1 or Fraction(native_tempi[0].get("midi.bpm", "0")) != bpm:
            continue
        wanted = round(60_000_000 / bpm)
        for start, following in starts[measure.get(XML_ID)]:
            target = start + round(onset * ppq)
            if target <= start or (following is not None and target >= following):
                continue
            candidates = [i for i, (tick, tag, value) in enumerate(events)
                          if tick == start and tag == tempo_tag
                          and abs(int.from_bytes(value, "big") - wanted) <= 1]
            conflicts = any(tick == target and tag == tempo_tag for tick, tag, _ in events)
            if len(candidates) == 1 and not conflicts:
                replacements[candidates[0]] = target
    if not replacements:
        return midi

    moved = [(replacements.get(i, tick), tag, value)
             for i, (tick, tag, value) in enumerate(events[:-1])]
    if not any(tick == 0 and tag == tempo_tag for tick, tag, _ in moved):
        moved.append((0, tempo_tag, (500_000).to_bytes(3, "big")))
    moved.sort(key=lambda item: item[0])
    moved.append((max(events[-1][0], moved[-1][0]), b"\xff\x2f", b""))
    output, previous = bytearray(), 0
    for tick, tag, value in moved:
        output.extend(write_vlq(tick - previous) + tag + write_vlq(len(value)) + value)
        previous = tick
    return midi[:18] + len(output).to_bytes(4, "big") + output + midi[end:]