"""Measured double-note tremolo playback expansion; preserve the engraved source."""
import copy
from decimal import Decimal
from fractions import Fraction as F
from math import gcd
import xml.etree.ElementTree as E

def expand_double_tremolos(source):
    root=copy.deepcopy(source)
    factor=1
    for part in root.findall('part'):
        divisions=None
        for measure in part.findall('measure'):
            div=measure.findtext('attributes/divisions')
            if div is not None:divisions=int(div)
            if divisions is None:continue
            for mark in measure.findall('note/notations/ornaments/tremolo'):
                if mark.get('type')!='start':continue
                try:strokes=int(mark.text)
                except (ValueError,TypeError):continue
                if strokes not in (2,3,4):continue
                required=(2**strokes)//gcd(divisions,2**strokes)
                factor=factor*required//gcd(factor,required)
    if factor!=1:
        for element in root.iter():
            if element.tag in ('duration','divisions','offset') and element.text:
                element.text=format(Decimal(element.text)*factor,'f')
    changes=[]
    unresolved=[]
    for part in root.findall('part'):
        divisions=None
        meter=None
        for measure in part.findall('measure'):
            div=measure.findtext('attributes/divisions')
            if div is not None:divisions=int(div)
            tm=measure.find('attributes/time')
            if tm is not None:
                try:
                    beats=tm.findall('beats');types=tm.findall('beat-type')
                    if len(beats)!=len(types) or not beats:raise ValueError('unmeasured')
                    meter=sum(F(sum(map(int,b.text.split('+')))*4,int(t.text)) for b,t in zip(beats,types))
                except (ValueError,TypeError,ZeroDivisionError):meter=None
            children=list(measure);groups=[];group=None
            cursor=F(0);previous=F(0);voice_intervals={}
            for element in children:
                if element.tag in ('backup','forward') and divisions:
                    cursor+=(-1 if element.tag=='backup' else 1)*F(element.findtext('duration'))/divisions
                if element.tag!='note':group=None;continue
                if element.find('chord') is None:group=[];groups.append(group)
                if group is not None:group.append(element)
                if divisions and element.find('grace') is None:
                    start=previous if element.find('chord') is not None else cursor
                    duration=F(element.findtext('duration','0'))/divisions
                    key=(element.findtext('voice','1'),element.findtext('staff','1'))
                    voice_intervals.setdefault(key,set()).add((start,start+duration))
                    if element.find('chord') is None:previous=start;cursor+=duration
            used=set()
            for index,first in enumerate(groups):
                marks=first[0].findall('notations/ornaments/tremolo')
                if not any(mark.get('type')=='start' for mark in marks):continue
                voice=first[0].findtext('voice','1');staff=first[0].findtext('staff','1')
                loc=dict(part=part.get('id'),measure=measure.get('number'),voice=voice,staff=staff)
                try:
                    if not divisions or divisions<=0:raise ValueError('unknown divisions')
                    if meter is None or measure.get('implicit')=='yes':raise ValueError('explicit source meter required for playback validation')
                    end=F(0)
                    for start,finish in sorted(voice_intervals.get((voice,staff),())):
                        if start!=end:raise ValueError('target voice has a gap or conflicting rhythm')
                        end=finish
                    if end!=meter:raise ValueError('target voice does not fill the printed meter; source recognition remains unresolved')
                    if len(marks)!=1:raise ValueError('ambiguous tremolo start')
                    strokes=int(marks[0].text)
                    if strokes not in (2,3,4):raise ValueError('unsupported stroke count')
                    second=groups[index+1] if index+1<len(groups) else None
                    if second is None:raise ValueError('missing stop chord')
                    stops=second[0].findall('notations/ornaments/tremolo')
                    if len(stops)!=1 or stops[0].get('type')!='stop' or int(stops[0].text)!=strokes:raise ValueError('missing or mismatched stop')
                    pair=first+second;positions=[children.index(n) for n in pair]
                    k0,k1=positions[0],positions[-1]
                    midway=children[positions[len(first)-1]+1:positions[len(first)]]
                    # Allow existing staff/tempo/dynamic directions at the original midpoint.
                    # Any other interleaving needs separate musical interpretation.
                    timeline=children[k0:k1+1]
                    if (any(n.tag!='direction' for n in midway) or
                        timeline!=first+midway+second):
                        raise ValueError('intervening music outside the tremolo midpoint')
                    written=first[0].findtext('type');nominal={'whole':F(4),'half':F(2)}.get(written)
                    if nominal is None:raise ValueError('unsupported written note type')
                    actual=nominal/2
                    for n in pair:
                        if id(n) in used:raise ValueError('overlapping tremolo')
                        if n.find('pitch') is None or n.findtext('voice','1')!=voice or n.findtext('staff','1')!=staff:raise ValueError('pitch, voice or staff mismatch')
                        if n.findtext('type')!=written or any(n.find(k) is not None for k in ('dot','grace','beam','lyric')):raise ValueError('unsupported written rhythm or lyric')
                        if (n.findtext('time-modification/actual-notes'),n.findtext('time-modification/normal-notes'))!=('2','1'):raise ValueError('unsupported ratio')
                        if F(n.findtext('duration'))/divisions!=actual:raise ValueError('written/sounding duration mismatch')
                        allowed_tie='stop' if n in first else 'start'
                        if any(t.get('type')!=allowed_tie for t in n.findall('tie')):raise ValueError('unsupported tie across tremolo chord interior')
                        for notation in n.findall('notations'):
                            for item in notation:
                                if item.tag=='ornaments' and all(v.tag=='tremolo' for v in item):continue
                                if item.tag=='articulations' and all(v.tag in ('accent','strong-accent','tenuto','staccato') for v in item):continue
                                if item.tag=='tied' and item.get('type')==allowed_tie:continue
                                raise ValueError('other notation requires explicit playback interpretation')
                    tick=F(divisions,2**strokes);attacks=actual*2*(2**strokes)
                    if tick.denominator!=1 or attacks.denominator!=1 or attacks%2:raise ValueError('unrepresentable alternating rhythm')
                    tick=int(tick);attacks=int(attacks);replacement=[]
                    for attack in range(attacks):
                        chord=first if attack%2==0 else second
                        for original in chord:
                            n=copy.deepcopy(original)
                            for attribute in ('default-x','default-y','id'):n.attrib.pop(attribute,None)
                            n.find('duration').text=str(tick);n.find('type').text={2:'16th',3:'32nd',4:'64th'}[strokes]
                            for element in list(n.findall('time-modification'))+list(n.findall('accidental')):n.remove(element)
                            keep_tie=(attack==0 and chord is first) or (attack==attacks-1 and chord is second)
                            if not keep_tie:
                                for element in list(n.findall('tie')):n.remove(element)
                            for notation in list(n.findall('notations')):
                                for item in list(notation):
                                    if item.tag=='ornaments' or (item.tag=='tied' and not keep_tie) or (item.tag=='articulations' and attack not in (0,1)):notation.remove(item)
                                if len(notation)==0:n.remove(notation)
                            replacement.append(n)
                    assert sum(F(n.findtext('duration'))/divisions for n in replacement if n.find('chord') is None)==actual*2
                    start=list(measure).index(first[0])
                    if midway:
                        half=attacks//2
                        n0=(half//2)*(len(first)+len(second))+(len(first) if half%2 else 0)
                        replacement[n0:n0]=midway
                    for n in timeline:measure.remove(n)
                    for n in pair:used.add(id(n))
                    for offset,n in enumerate(replacement):measure.insert(start+offset,n)
                    changes.append(dict(loc,strokes=strokes,written_type=written,chord_attacks=attacks,original_heads=len(pair),expanded_heads=len(replacement),duration_quarters=str(actual*2),outgoing_ties_only_on_final_attack=True))
                except (ValueError,TypeError,ZeroDivisionError) as exc:unresolved.append(dict(loc,reason=str(exc)))
    return root,dict(changes=changes,unresolved=unresolved,divisions_multiplier=factor,engraving_tree_modified=False,scope='Separate playback score; unsupported or rhythmically incomplete source remains pending')


def expand_rolled_tremolo_chords(root):
    """Subdivide measured rolled tremolos in the MIDI copy of the score."""
    widths = {1: "eighth", 2: "16th", 3: "32nd", 4: "64th"}
    written = {"quarter": 1, "half": 2, "whole": 4}
    plans = []
    factor = 1
    for part in root.findall("part"):
        divisions = 0
        for measure in part.findall("measure"):
            value = measure.findtext("attributes/divisions")
            if value is not None:
                divisions = int(value)
            groups = []
            chord = None
            for node in measure:
                if node.tag != "note":
                    chord = None
                    continue
                if node.find("chord") is None:
                    chord = [node]
                    groups.append(chord)
                elif chord is not None:
                    chord.append(node)
            for chord in groups:
                leader = chord[0]
                marks = leader.findall("notations/ornaments/tremolo")
                if not divisions or len(chord)<2 or len(marks)!=1 or marks[0].get("type","single")!="single":
                    continue
                if marks[0].text not in {"1","2","3","4"} or leader.findtext("type") not in written:
                    continue
                strokes = int(marks[0].text)
                dots = len(leader.findall("dot"))
                if dots>2:
                    continue
                duration = F(written[leader.findtext("type")]) * sum(F(1,2**d) for d in range(dots+1))
                subdivision_ticks = F(divisions,2**strokes)
                count = duration * (2**strokes)
                if count.denominator!=1 or count<2:
                    continue
                rolls = []
                admissible = True
                for note in chord:
                    ars = note.findall("notations/arpeggiate")
                    if (len(ars)!=1 or note.find("pitch") is None or note.findtext("duration") is None
                        or F(note.findtext("duration"))!=duration*divisions or note.findtext("type")!=leader.findtext("type")
                        or note.findtext("voice","1")!=leader.findtext("voice","1")
                        or note.findtext("staff","1")!=leader.findtext("staff","1")
                        or len(note.findall("dot"))!=dots
                        or any(note.find(tag) is not None for tag in ("grace","cue","tie","time-modification","beam","lyric"))
                        or (note is not leader and note.find("notations/ornaments/tremolo") is not None)):
                        admissible = False
                        break
                    rolls.append((ars[0].get("number","1"),ars[0].get("direction")))
                if not admissible or len(set(rolls))!=1:
                    continue
                plans.append((measure,chord,subdivision_ticks,int(count),widths[strokes]))
                divisor = subdivision_ticks.denominator
                factor = factor*divisor//gcd(factor,divisor)
    if not plans:
        return 0
    if factor!=1:
        for tag in ("duration","divisions","offset"):
            for node in root.iter(tag):
                if node.text:
                    node.text = format(Decimal(node.text)*factor,"f")
    for measure,chord,tick,count,kind in plans:
        replacement = []
        for attack in range(count):
            for original in chord:
                note = copy.deepcopy(original)
                for key in ("default-x","default-y","id"):
                    note.attrib.pop(key,None)
                note.find("duration").text = str(int(tick*factor))
                note.find("type").text = kind
                for item in note.findall("dot"):
                    note.remove(item)
                if attack:
                    for item in note.findall("accidental"):
                        note.remove(item)
                for notation in note.findall("notations"):
                    for item in list(notation):
                        if item.tag=="ornaments":
                            for mark in item.findall("tremolo"):
                                item.remove(mark)
                            if not len(item):
                                notation.remove(item)
                        if attack and item.tag!="arpeggiate" and item in notation:
                            notation.remove(item)
                    if not len(notation):
                        note.remove(notation)
                replacement.append(note)
        start = list(measure).index(chord[0])
        measure[start:start+len(chord)] = replacement
    return len(plans)
