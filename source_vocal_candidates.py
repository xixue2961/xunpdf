
"""Identify cases that warrant reading original vocal-score pixels."""
from collections import Counter
from fractions import Fraction

from source_long_tuplet_context import clocks



def complete_lane(measure, divisions):
    notes = measure.findall('note')
    if (not notes or len({note.findtext('voice') for note in notes}) != 1
        or len({note.findtext('staff', '1') for note in notes}) != 1
        or measure.find('backup') is not None or measure.find('forward') is not None
        or measure.find('.//offset') is not None or len(measure.findall('attributes')) > 1
        or any(note.find('grace') is not None or not note.findtext('duration') for note in notes)):
        return [], None
    return notes, sum(Fraction(note.findtext('duration')) / divisions
                      for note in notes if note.find('chord') is None)


def chord_at(notes, indices, divisions):
    if not indices or indices != list(range(indices[0],indices[-1]+1)):
        return []
    if indices[-1] >= len(notes):
        return []
    chord = [notes[index] for index in indices]
    following = indices[-1] + 1
    if (chord[0].find('chord') is not None
        or any(note.find('chord') is None for note in chord[1:])
        or (following < len(notes) and notes[following].find('chord') is not None)):
        return []
    if any(note.find('pitch') is None or note.findtext('type') != 'quarter'
           or note.findtext('duration') != str(divisions)
           or any(note.find(tag) is not None for tag in ('dot','beam','time-modification'))
           for note in chord):
        return []
    return chord


def has_same_pitch_continuation(notes, chord, indices):
    after = indices[-1] + 1
    if after >= len(notes):
        return False
    following = [notes[after]]
    for note in notes[after+1:]:
        if note.find('chord') is None:
            break
        following.append(note)
    def pitch(note):
        return tuple(note.findtext('pitch/'+key, '0') for key in ('step','alter','octave'))
    return (all(note.findtext('type') == 'half' for note in following)
            and Counter(map(pitch, chord)) == Counter(map(pitch, following))
            and {pitch(note) for note in chord if note.find("tie[@type='start']") is not None}
                & {pitch(note) for note in following if note.find("tie[@type='stop']") is not None})


def phantom_lane(measure,divisions,capacity):
    nodes=list(measure)
    if len(nodes)<5:
        return None
    backup,forward,note=nodes[-3:]
    if (not capacity or [x.tag for x in (backup,forward,note)]!=['backup','forward','note']
        or note.findtext('type')!='half' or note.findtext('duration')!=str(2*divisions)
        or any(n.tag not in ('pitch','duration','voice','type','stem','staff') for n in note)
        or note.find('pitch') is None or forward.findtext('voice')!=note.findtext('voice')
        or any(n.tag in ('backup','forward') for n in nodes[:-3])):
        return None
    primary=[n for n in nodes[:-3] if n.tag=='note']
    if (not primary or len({p.findtext('voice') for p in primary})!=1
        or any(n.find('grace') is not None or not n.findtext('duration') for n in primary)
        or primary[0].findtext('voice')==note.findtext('voice')):
        return None
    length=sum(int(n.findtext('duration')) for n in primary if n.find('chord') is None)
    if (length!=capacity*divisions or int(backup.findtext('duration'))!=length
        or int(forward.findtext('duration'))+2*divisions<=length):
        return None
    return nodes[-3:],len(primary)


def is_han(text):
    return bool(text) and all('\u3400'<=char<='\u9fff' for char in text)



def merged_phrase(notes):
    lyrics=[(i,n.findtext('lyric/text','')) for i,n in enumerate(notes) if n.find('lyric') is not None]
    joined=[text for _,text in lyrics if len(text)>=3 and is_han(text)]
    if len(joined)!=1 or any(text not in (joined[0],'marcato') for _,text in lyrics):
        return None
    phrase=joined[0]
    indices=[i for i,n in enumerate(notes) if n.find('pitch') is not None]
    if (len(indices)!=len(phrase) or any(len(n.findall('lyric'))>1 or n.find('chord') is not None
        or n.find('grace') is not None or n.find("tie[@type='stop']") is not None for n in notes)
        or len({n.findtext('voice') for n in notes})!=1 or any(i not in indices for i,_ in lyrics)):
        return None
    return phrase,indices,lyrics


def affected_flag_candidates(bar,divisions,capacity):
    notes,length=complete_lane(bar,divisions)
    if not capacity or divisions%2 or length!=capacity+Fraction(1,2):
        return []
    starts=[i for i,n in enumerate(notes) if n.find('chord') is None]
    stops=starts[1:]+[len(notes)]
    output=[]
    for start,stop in zip(starts,stops,strict=True):
        indices=list(range(start,stop))
        chord=chord_at(notes,indices,divisions)
        if chord and has_same_pitch_continuation(notes,chord,indices):
            output.extend(indices)
    return output


def requests(root):
    flag_keys=[]
    boxed=phrases=False
    for part in root.findall('part'):
        if part.find('.//lyric') is None:
            continue
        measures=part.findall('measure')
        numbers=Counter(bar.get('number') for bar in measures)
        for bar,(divisions,capacity) in zip(measures,clocks(part),strict=True):
            number=bar.get('number')
            if numbers[number]!=1 or not capacity:
                continue
            flag_keys.extend((part.get('id'),number,i) for i in affected_flag_candidates(bar,divisions,capacity))
            boxed=boxed or phantom_lane(bar,divisions,capacity) is not None
            if bar.find('backup') is None and bar.find('forward') is None:
                phrases=phrases or merged_phrase(bar.findall('note')) is not None
    return flag_keys,boxed,phrases

