"""Render a synchronized, normal fermata in the local MIDI performance."""

from collections import Counter
from fractions import Fraction
from xml.etree import ElementTree as ET

from midi_expression_positions import read_events, read_tracks
from midi_tempo_positions import MEI, XML_ID, write_vlq
from source_lyric_roles import _score_events


def piano_holds(score):
    pianos = [p.get("id") for p in score.findall("part-list/score-part")
              if p.findtext("part-name", "").casefold() == "piano"]
    if len(pianos) != 1:
        return [], []
    events,_ = _score_events(score)
    piano = score.find(f"part[@id='{pianos[0]}']")
    labels = [m.get("number") for m in piano.findall("measure")]
    if len(set(labels)) != len(labels):
        return [], []
    candidates = {}
    pitched = {}
    for (part,label,_), event in events.items():
        note = event["node"]
        pitch = note.find("pitch")
        if pitch is None:
            continue
        pitched.setdefault(label,[]).append(event)
        mark = note.find("notations/fermata")
        if part == pianos[0] and mark is not None and (mark.text or "").strip() in ("","normal"):
            candidates.setdefault((label,event["start"],event["duration"]),set()).add(note.findtext("staff","1"))

    result = []
    for (label,start,duration), staves in candidates.items():
        if staves != {"1","2"} or duration <= 0:
            continue
        end = start+duration
        held = [e for e in pitched[label] if e["start"] < end and e["start"]+e["duration"] > start]
        if not held or any(e["start"] != start or e["duration"] != duration
                           or e["node"].find("tie") is not None
                           or e["node"].find("grace") is not None for e in held):
            continue
        tones = Counter()
        for e in held:
            note = e["node"]
            step = note.findtext("pitch/step")
            semitone = {"C":0,"D":2,"E":4,"F":5,"G":7,"A":9,"B":11}[step]
            pitch = 12*(1+int(note.findtext("pitch/octave")))+semitone
            pitch += Fraction(note.findtext("pitch/alter","0"))
            if pitch.denominator != 1:
                break
            tones[int(pitch)] += 1
        else:
            result.append((labels.index(label),start,duration,tones))
    return labels,result


def fermata_windows(holds, clock, ppq, tracks):
    windows = []
    for bar,beat,duration,pitches in holds:
        offset, length = beat*ppq,duration*ppq
        if offset.denominator != 1 or length.denominator != 1 or length < 4 or length % 2:
            continue
        start = clock[bar][1]+int(offset)
        end = start+int(length)
        if bar+1 >= len(clock) or end > clock[bar+1][1]:
            continue
        attacks, releases = Counter(), Counter()
        extras, held_voices = False, set()
        for i,track in enumerate(tracks):
            for tick,status,values in track:
                if status>>4 not in (8,9) or not start <= tick < end:
                    continue
                key = i,status & 15,values[0]
                if tick == start and status>>4 == 9 and values[1]:
                    attacks[key] += 1
                    held_voices.add(key)
                elif tick == end-1 and (status>>4 == 8 or not values[1]):
                    releases[key] += 1
                elif tick != start or status>>4 == 9 and values[1]:
                    extras = True
        heard = Counter()
        for (_,_,pitch), count in attacks.items():
            heard[pitch] += count
        if not extras and heard == pitches and attacks == releases:
            windows.append((end,int(length)//2,held_voices,set()))
    return sorted(windows,key=lambda w:w[0])


def mixed_ensemble_holds(score):
    """Find a shared pause where the opening piano and vocal holds differ in length."""
    events,_ = _score_events(score)
    parts = {p.get("id"):p.findtext("part-name","").casefold()
             for p in score.findall("part-list/score-part")}
    tracks = [[m.get("number") for m in p.findall("measure")] for p in score.findall("part")]
    if not tracks or len(set(tracks[0])) != len(tracks[0]) or any(t != tracks[0] for t in tracks):
        return {}
    bars = {label:i for i,label in enumerate(tracks[0])}
    openings = {}
    for (part,label,_), event in events.items():
        if event["start"] == 0 and event["node"].find("pitch") is not None:
            openings.setdefault(label,[]).append((part,event))
    candidates = {}
    for label, opening in openings.items():
        groups = {}
        for part, e in opening:
            note = e["node"]
            staff = note.findtext("staff","1")
            groups.setdefault((part,staff),[]).append(e)
        piano = [k for k in groups if parts[k[0]]=="piano"]
        singers = [k for k in groups if parts[k[0]] in {"tenor","bass","alto","soprano"}]
        if {k[1] for k in piano}!={"1","2"} or len({k[0] for k in piano})!=1:
            continue
        if len({k[0] for k in singers})<2 or len(singers)+len(piano)!=len(groups):
            continue
        if any({v["duration"] for v in groups[k]} != {2 if k in piano else 3} for k in groups):
            continue
        if any(not any((m.text or "").strip() in ("","normal") for v in g
                       for m in v["node"].findall("notations/fermata")) for g in groups.values()):
            continue
        if any(0 < e["start"] < 2 for k,e in events.items() if k[1]==label and e["node"].find("pitch") is not None):
            continue
        sounding = Counter()
        releases = Counter()
        previous = Counter()
        for _,e in opening:
            note = e["node"]
            pitch = (int(note.findtext("pitch/octave"))+1)*12
            pitch += {"C":0,"D":2,"E":4,"F":5,"G":7,"A":9,"B":11}[note.findtext("pitch/step")]
            pitch += Fraction(note.findtext("pitch/alter","0"))
            if pitch.denominator != 1 or note.find("grace") is not None or note.find('tie[@type="start"]') is not None:
                break
            sounding[int(pitch)] += 1
            if e["duration"]==2:
                releases[int(pitch)] += 1
            if note.find('tie[@type="stop"]') is not None:
                previous[int(pitch)] += 1
        else:
            candidates[bars[label]] = (sounding,releases,previous)
    return candidates


def ensemble_windows(holds,clock,ppq,tracks):
    for bar,(pitches, releases, prior) in holds.items():
        if bar+1 >= len(clock):
            continue
        start = clock[bar][1]
        cut = start+2*ppq
        if start+3*ppq >= clock[bar+1][1]:
            continue
        live, on, off, vocal_off = Counter(),Counter(),Counter(),Counter()
        keys, keep = set(),set()
        vocal_cut = start+3*ppq-1
        extra = False
        for i, track in enumerate(tracks):
            for tick,status,data in track:
                if tick > vocal_cut:
                    break
                if status>>4 not in (8,9):
                    continue
                key=(i,status&15,data[0])
                attack=status>>4==9 and data[1]>0
                if tick >= cut:
                    if tick==vocal_cut and not attack and live[key]>0 and vocal_off[data[0]] < (pitches-releases)[data[0]]:
                        vocal_off[data[0]] += 1
                        keep.add((tick,*key))
                    continue
                if tick < start:
                    live[key] += 1 if attack else -1
                elif tick == start:
                    live[key] += 1 if attack else -1
                    if attack:
                        on[data[0]] += 1
                elif tick == cut-1 and not attack and live[key]>0:
                    off[data[0]] += 1
                    keys.add(key)
                else:
                    extra = True
        observed = Counter()
        for (_,_,pitch), count in live.items():
            if count:
                observed[pitch] += count
        if not extra and observed==pitches and off==releases and on==pitches-prior and vocal_off==pitches-releases:
            yield cut, ppq, keys, keep


def render_fermatas(midi, data, renderer):
    if "<fermata" not in data:
        return midi
    score = ET.fromstring(data)
    labels, holds = piano_holds(score)
    ensemble = mixed_ensemble_holds(score)
    if not holds and not ensemble:
        return midi
    raw_tracks = read_tracks(midi)
    ppq = int.from_bytes(midi[12:14],"big")
    if not raw_tracks or not 0 < ppq < 32768:
        return midi
    measures = ET.fromstring(renderer.getMEI()).findall(".//"+MEI+"measure")
    clock = [(e["measureOn"],round(Fraction(str(e["qstamp"]))*ppq)) for e in
             renderer.renderToTimemap({"includeMeasures":True}) if "measureOn" in e]
    if len(clock)!=len(labels) or [m.get(XML_ID) for m in measures] != [x[0] for x in clock]:
        return midi
    tracks = [list(read_events(track)) for track in raw_tracks]
    windows = fermata_windows(holds,clock,ppq,tracks) + list(ensemble_windows(ensemble,clock,ppq,tracks))
    if not windows:
        return midi

    output = bytearray(midi[:14])
    for i,track in enumerate(tracks):
        shifted = []
        for order,(tick,status,values) in enumerate(track):
            delay = 0
            for cut,extra,keys,keep in windows:
                ending = status>>4 == 8 or status>>4 == 9 and not values[1]
                if ending:
                    key = i,status & 15,values[0]
                    if (tick,*key) in keep:
                        continue
                    if tick == cut-1 and key in keys:
                        delay += extra
                        continue
                if tick >= cut:
                    delay += extra
            shifted.append((tick+delay,order,status,values))
        encoded, previous = bytearray(),0
        for tick,_,status,values in sorted(shifted):
            encoded.extend(write_vlq(tick-previous)+bytes([status])+values)
            previous=tick
        output.extend(b"MTrk"+len(encoded).to_bytes(4,"big")+encoded)
    return bytes(output)
