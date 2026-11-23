"""Realize a verified synchronized crescendo as MIDI channel expression."""
from collections import defaultdict
from fractions import Fraction
from io import BytesIO
from xml.etree import ElementTree as ET

from midi_tempo_positions import MEI, XML_ID, read_vlq, write_vlq
from source_lyric_roles import _score_events


def synchronized_crescendos(score):
    parts = {n.get("id"):n.findtext("part-name", "")
             for n in score.findall("part-list/score-part")}
    roles = ("Tenor", "Bass", "Piano")
    if any(tuple(parts.values()).count(role) != 1 for role in roles):
        return {}, 0
    measures = {p.get("id"):[m.get("number") for m in p.findall("measure")] for p in score.findall("part")}
    labels = next(iter(measures.values()),[])
    if any(seq != labels for seq in measures.values()):
        return {}, 0
    indices = {label:i for i,label in enumerate(labels)}
    starts, pending, spans, forte = defaultdict(list), {}, defaultdict(dict), set()
    _, directions = _score_events(score)
    for item in directions:
        name = parts.get(item["part"])
        if name not in roles:
            continue
        n = item["node"]
        where = indices[item["measure"]], item["start"]
        if name == "Piano" and n.find("direction-type/dynamics/f") is not None and n.find("sound[@dynamics='100']") is not None:
            forte.add(where)
        for wedge in n.findall("direction-type/wedge"):
            key = name, item["staff"], wedge.get("number","1")
            kind = wedge.get("type")
            if kind in ("crescendo", "diminuendo"):
                pending[key] = where if kind == "crescendo" else None
            elif kind == "stop":
                at = pending.pop(key,None)
                if at and at[0]+1 == where[0] and at[1] == 0 and where[1]>0:
                    spans[at].setdefault(name,[]).append(where)
    sections = defaultdict(list)
    for at, part in spans.items():
        if not all(len(part.get(role,[])) == 1 for role in roles):
            continue
        tenor,bass,piano = (part[role][0] for role in roles)
        if tenor != bass or tenor not in forte or not tenor < piano:
            continue
        for role in roles:
            sections[role].append((at,tenor))
    return sections,len(labels)


def read_tracks(payload):
    if payload[:10] != b"MThd\x00\x00\x00\x06\x00\x01":
        return []
    stream = BytesIO(payload[14:])
    tracks = []
    for _ in range(int.from_bytes(payload[10:12],"big")):
        if stream.read(4) != b"MTrk":
            raise ValueError("MIDI track marker missing")
        size = int.from_bytes(stream.read(4),"big")
        track = stream.read(size)
        if len(track) != size:
            raise ValueError("MIDI track data truncated")
        tracks.append(track)
    if stream.read(1):
        raise ValueError("Unexpected data after MIDI tracks")
    return tracks


def read_events(content):
    stream = BytesIO(content)
    when, running = 0, None
    while stream.tell() < len(content):
        when += read_vlq(stream)
        status = stream.read(1)[0]
        if status < 128:
            if running is None:
                raise ValueError("MIDI running status missing")
            stream.seek(-1,1)
            status = running
        if status in (255,240,247):
            subtype = stream.read(1) if status == 255 else b""
            length = read_vlq(stream)
            data = stream.read(length)
            if len(data) != length:
                raise ValueError("Truncated MIDI system event")
            yield when,status,subtype+write_vlq(length)+data
        elif 128 <= status < 240:
            running = status
            length = 1 if status>>4 in (12,13) else 2
            data = stream.read(length)
            if len(data) != length:
                raise ValueError("Truncated MIDI channel event")
            yield when,status,data
        else:
            raise ValueError("Unsupported MIDI system event")


def track_identity(events):
    names, channels, broad = set(), set(), set()
    for _,status,data in events:
        if status == 255 and data[:1]==b"\x03":
            stream = BytesIO(data[1:])
            names.add(stream.read(read_vlq(stream)).decode("utf8", errors="replace"))
        if status>>4 == 9 and data[1] > 0:
            channels.add(status & 15)
        if status>>4 == 11 and data[0] == 11:
            broad.add(status & 15)
    return names,channels,broad


def terminal_fortissimo(score):
    """Find a shared four-beat crescendo immediately before a written piano ff."""
    roles = ("Tenor", "Bass", "Piano")
    names = {p.get("id"): p.findtext("part-name", "") for p in score.findall("part-list/score-part")}
    if any(list(names.values()).count(role) != 1 for role in roles):
        return {}, 0
    parts = [p for p in score.findall("part") if names.get(p.get("id")) in roles]
    labels = [m.get("number") for m in parts[0].findall("measure")] if parts else []
    if (not labels or len(set(labels)) != len(labels)
            or any([m.get("number") for m in p.findall("measure")] != labels for p in parts)):
        return {}, 0
    positions = {label:i for i,label in enumerate(labels)}
    _, directions = _score_events(score)
    pending, spans = {}, defaultdict(list)
    dynamics, targets = set(), defaultdict(list)
    for d in directions:
        name = names.get(d["part"])
        if name not in roles:
            continue
        index, beat = positions[d["measure"]], d["start"]
        node = d["node"]
        written = node.find("direction-type/dynamics")
        if written is not None:
            dynamics.add((index, beat))
            sound = node.find("sound")
            level = sound.get("dynamics", "") if sound is not None else ""
            if (name == "Piano" and d["staff"] == "2" and beat == 0 and written.find("ff") is not None
                    and level.isdecimal() and 100 < int(level) <= 142):
                targets[index].append(round(Fraction(90 * int(level), 100)))
        for wedge in node.findall("direction-type/wedge"):
            key = name, d["staff"], wedge.get("number", "1")
            kind = wedge.get("type")
            if kind in ("crescendo", "diminuendo"):
                pending[key] = (index, beat) if kind == "crescendo" else None
            elif kind == "stop":
                start = pending.pop(key, None)
                if start == (index, 0) and beat == 4 and d["staff"] == ("2" if name == "Piano" else "1"):
                    spans[index].append(name)

    for climax, levels in targets.items():
        if (not 0 < len(labels)-climax <= 4 or len(levels) != 1
                or sorted(spans[climax-1]) != sorted(roles)
                or any(i == climax-1 for i, _ in dynamics)):
            continue
        next_dynamic = min(((i,b) for i,b in dynamics if (i,b) > (climax,0)), default=None)
        return dict(start=climax-1, climax=climax, level=levels[0], stop=next_dynamic), len(labels)
    return {}, len(labels)


def raise_terminal_velocities(events, channel, name, window, clock, ppq):
    start, climax = clock[window["start"]][1], clock[window["climax"]][1]
    if climax-start != 4*ppq:
        return events
    stop = window["stop"]
    end = clock[stop[0]][1]+round(stop[1]*ppq) if stop else None
    changed = []
    for tick, status, values in events:
        if status == (0x90 | channel) and values[1] and tick >= start:
            if tick < climax:
                progress = Fraction(tick-start, climax-start)
            elif name == "Piano" and (end is None or tick < end):
                progress = Fraction(1)
            else:
                progress = Fraction(0)
            velocity = values[1]+round(progress*max(0,window["level"]-values[1]))
            values = bytes((values[0],velocity))
        changed.append((tick,status,values))
    return changed


def add_crescendo_expression(midi, data, renderer):
    if "<wedge" not in data:
        return midi
    score = ET.fromstring(data)
    groups,count = synchronized_crescendos(score)
    closing,closing_count = terminal_fortissimo(score)
    if closing:
        count = closing_count
    if not groups and not closing:
        return midi
    tracks = read_tracks(midi)
    ppq = int.from_bytes(midi[12:14],"big")
    if not tracks or not 0 < ppq < 32768:
        return midi
    measures = ET.fromstring(renderer.getMEI()).findall(".//"+MEI+"measure")
    clock = [(e["measureOn"],round(Fraction(str(e["qstamp"]))*ppq)) for e in
             renderer.renderToTimemap({"includeMeasures":True}) if "measureOn" in e]
    if len(clock)!=count or len(measures)!=count or any(e.get(XML_ID)!=m[0] for e,m in zip(measures,clock)):
        return midi

    by_channel = defaultdict(set)
    by_name = defaultdict(list)
    authored = set()
    for index,track in enumerate(tracks):
        events = list(read_events(track))
        names, channels, expression = track_identity(events)
        authored.update(expression)
        for ch in channels:
            by_channel[ch].update(names or {None})
        if len(names)==len(channels)==1:
            by_name[next(iter(names))].append((index,next(iter(channels)),events))

    if closing:
        for role in ("Tenor", "Bass", "Piano"):
            candidates = by_name[role]
            channels = {ch for _,ch,_ in candidates}
            if len(channels) != 1 or by_channel[next(iter(channels))] != {role} or channels & authored:
                closing = {}
                break

    piano_attacks = sorted({tick for _, _, events in by_name["Piano"]
                            for tick,status,values in events if status>>4 == 9 and values[1]})
    for name in sorted(set(groups) | ({"Tenor", "Bass", "Piano"} if closing else set())):
        candidates = by_name[name]
        channels = {ch for _,ch,_ in candidates}
        if len(channels)!=1:
            continue
        channel = next(iter(channels))
        if by_channel[channel]!={name} or channel in authored:
            continue
        index, _, events = candidates[0]
        control = []
        for (begin,pos), (end,last) in groups.get(name, []):
            start,stop = clock[begin][1]+round(pos*ppq), clock[end][1]+round(last*ppq)
            # The written forte can precede the rendered offbeat by one swing subdivision.
            stop = next((tick for tick in piano_attacks if stop <= tick <= stop+round(Fraction(ppq,6))),stop)
            if stop<=start or end+1>=count or stop>=clock[end+1][1]:
                continue
            # The notation specifies change, not absolute controller values.
            # Ramp to neutral without altering subsequent expression or note data.
            for step in range(14):
                tick = start+round(Fraction(step,13)*(stop-start))
                level = 100+round(Fraction(step,13)*27)
                control.append((tick,0xb0|channel,bytes([11,level])))
        for track_index,_,original in candidates:
            extra = control if track_index == index else []
            transformed = raise_terminal_velocities(original, channel, name, closing, clock, ppq) if closing else original
            if not extra and transformed == original:
                continue
            output,previous = bytearray(),0
            for tick,status,values in sorted(extra+transformed,key=lambda item:item[0]):
                output.extend(write_vlq(tick-previous)+bytes([status])+values)
                previous=tick
            tracks[track_index]=bytes(output)
    return midi[:14]+b"".join(b"MTrk"+len(track).to_bytes(4,"big")+track for track in tracks)
