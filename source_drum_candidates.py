
"""Identify single-lane drum measures with source-verifiable timing errors."""
from fractions import Fraction as F
from source_long_tuplet_context import clocks


def classify(bar,divisions,capacity):
    if capacity!=4 or not divisions:
        return None
    notes=bar.findall('note')
    if len(notes)<4 or len({n.findtext('voice') for n in notes})!=1:
        return None
    if any(n.find('chord') is not None or n.find('time-modification') is not None
           or n.find('grace') is not None or not n.findtext('duration')
           or (n.find('unpitched') is None and n.find('rest') is None) for n in notes):
        return None
    lengths=[F(n.findtext('duration'))/divisions for n in notes]
    if sum(lengths)<=capacity:
        return None
    rests=[n for n in notes if n.find('rest') is not None]
    if len(rests)!=1:
        return None
    rest=rests[0]
    if (rest.findtext('type')=='16th' and all(n.tag=='note' for n in bar)
        and not any(n.find('dot') is not None for n in notes) and divisions%4==0):
        return 'beams'
    if (len(notes)==10 and rest is notes[4] and lengths[:4]==[F(3,4),F(1,4),F(1,2),F(1,2)]
        and lengths[4:]==[F(1,2)]*6 and all(n.findtext('type')=='eighth' for n in notes[4:])
        and all(n.tag in ('note','barline','attributes') for n in bar)):
        return 'tuplets'
    return None


def percussion_targets(root):
    targets={part.get('id') for part in root.findall('part-list/score-part')
             if any(inst.findtext('midi-channel')=='10' and inst.find('midi-unpitched') is not None
                    for inst in part.findall('midi-instrument'))}
    for part in root.findall('part'):
        if part.get('id') not in targets:
            continue
        for i,(bar,(div,cap)) in enumerate(zip(part.findall('measure'),clocks(part),strict=True)):
            mode=classify(bar,div,cap)
            if mode:
                yield part,i,bar,div,mode

