
"""Restore lyrics misclassified as ordinary text on a parallel vocal staff."""

from collections import defaultdict
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import with_bounds
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor


def is_han(text):
    return bool(text) and all('\u3400' <= c <= '\u9fff' for c in text)


def vocal_parts(root):
    voice_names = {'soprano', 'alto', 'tenor', 'bass', 'baritone', 'mezzo-soprano'}
    named = {p.get('id') for p in root.findall('part-list/score-part')
             if p.findtext('part-name', '').strip().casefold() in voice_names}
    return {p.get('id') for p in root.findall('part')
            if (p.get('id') in named or p.find('measure/note/lyric') is not None)
            and all(n.text in (None, '1') for n in p.findall('measure/attributes/staves'))}


def same_note(current, native):
    if not current or not native:
        return False
    a, b = _music_signature(current), _music_signature(native)
    left, right = current['node'], native['node']
    if not a[2] or not b[2] or any(left.find(tag) is not None for tag in ('grace', 'chord', 'cue')):
        return False
    x = left.get('default-x')
    if x is None or x != right.get('default-x') or a[3:] != b[3:]:
        return False
    if a == b:
        return True
    if a[2][:2] != b[2][:2] or abs(int(a[2][2]) - int(b[2][2])) > 1:
        return False
    # Rhythm and tenor-octave fixes can keep the same printed head.
    return all(sum(n.get('default-x') == x and n.find('pitch') is not None
                   for n in event['measure'].findall('note')) == 1 for event in (current, native))


def read_words(system, groups, current, native, control_nodes):
    raw_nodes = with_bounds(system)
    staff_lines = {s.get('id'): [float(line.find('point').get('y'))
                               for line in s.findall('lines/line')]
                   for s in system.findall('.//staff')}
    parents = {d.get('id'): d for d in system.findall('sig/inters/sentence')
               if d.get('role') == 'Direction' and d.find('bounds') is not None}
    children = defaultdict(list)
    owners = defaultdict(list)
    for rel in system.findall('sig/relations/relation'):
        parent, child = rel.get('source'), rel.get('target')
        if parent in parents and rel.find('containment') is not None:
            children[parent].append(child)
            owners[child].append(parent)

    heads = defaultdict(list)
    for row in groups:
        key = row['part'], row['measure'], int(row['note_index'])
        if (not same_note(current.get(key), native.get(key)) or
                not native_anchor(row, control_nodes) or not native_anchor(row, raw_nodes)):
            continue
        n = current[key]['node']
        heads[row['staff']].append((key, int(row['x']) + int(row['w']) / 2,
                                   n.find("tie[@type='stop']") is not None))

    words, packed = {}, {}
    objects = system.findall('sig/inters/word') + system.findall('sig/inters/lyric-item')
    for word in objects:
        staff = word.get('staff')
        text = word.get('value', '').strip()
        identities = owners[word.get('id')]
        is_lyric = word.tag == 'lyric-item'
        lines = staff_lines.get(staff, [])
        if (not is_han(text) or (len(text)<2 if is_lyric else len(identities)!=1) or len(lines) != 5 or
                word.find('bounds') is None or staff not in heads):
            continue
        sentence = None if is_lyric else identities[0]
        if not is_lyric and parents[sentence].get('staff') != staff:
            continue
        x, y, w, h = coordinate(word)
        gap = (lines[-1] - lines[0]) / 4
        below = staff_lines.get(str(int(staff) + 1))
        if not .3 * gap <= y - lines[-1] <= 6.5 * gap or (below and y+h > below[0]-.45*gap):
            continue
        font = float(word.get('font') or h)
        hits = sorted((n for n in heads[staff] if x <= n[1] < x+w and not n[2]), key=lambda n: n[1])
        if (len(hits) != len(text) or not 30 <= font <= 90 or not .70*font <= h <= 1.5*font
                or not 27 <= w / len(text) <= 100):
            continue
        if any(not min(23, .48*h) <= b[1]-a[1] <= 3*h for a, b in zip(hits, hits[1:])):
            continue
        target = packed if is_lyric else words
        target[word.get('id')] = (staff, sentence, text, (x, y, w, h), font, hits)
    return words, parents, children, staff_lines, packed


def glyph_columns(word, raster, min_height=.60, min_ink=.09):
    _, _, text, (x,y,w,h), font, hits = word
    height, width = raster.shape
    if not (0 <= x < x+w <= width and 0 <= y < y+h <= height):
        return []
    cuts = [x, *(int((a[1]+b[1])/2) for a,b in zip(hits,hits[1:])), x+w]
    placed = []
    for (key, center, _), char, l, r in zip(hits, text, cuts, cuts[1:]):
        yy, xx = np.nonzero(raster[y:y+h,l:r] < 135)
        if not min_ink*font*font <= len(xx) <= .90*font*font:
            return []
        left, right = int(xx.min())+l, int(xx.max())+l+1
        top, bottom = int(yy.min())+y, int(yy.max())+y+1
        if abs(center-(left+right)/2)>max(12,.27*font) or right-left<.35*font or bottom-top<min_height*font:
            return []
        placed.append((key,center,char,(left,top,right,bottom)))
    return placed


def ink_similarity(raster, rect, dx, dy):
    x,y,r,b = rect
    x,y,r,b = x-3,y-3,r+3,b+3
    height, width = raster.shape
    if not (0<=min(x,x+dx)<max(r,r+dx)<=width and 0<=min(y,y+dy)<max(b,b+dy)<=height):
        return 0
    a, c = raster[y:b,x:r], raster[y+dy:b+dy,x+dx:r+dx]
    scores = []
    for cutoff in (110,155,185):
        first, second = a<cutoff, c<cutoff
        union = int((first|second).sum())
        if union<100:
            return 0
        scores.append(int((first&second).sum())/union)
    return min(scores)


def proposals(words, slots, raster, current):
    peers = defaultdict(list)
    for id_, columns in slots.items():
        peers[words[id_][2]].append((id_,columns))
    candidates = defaultdict(list)
    for group in peers.values():
        for (a, target),(b, donor) in product(group,repeat=2):
            staff, sentence, text, *_ = words[a]
            if not 1 <= abs(int(staff)-int(words[b][0])) <= 3:
                continue
            if any(abs(t[1]-d[1])>8 for t,d in zip(target,donor)):
                continue
            delta = round(median((d[3][1]+d[3][3]-t[3][1]-t[3][3])/2
                                 for t,d in zip(target,donor)))
            for (key,_,char,rect),(other,_,_,_) in zip(target,donor):
                n = current[key]['node']
                peer = current[other]['node']
                lyrics = peer.findall('lyric')
                if (key[0] == other[0] or n.find('lyric') is not None or len(lyrics)!=1
                        or lyrics[0].findtext('text')!=char or len(lyrics[0].findall('text'))!=1):
                    continue
                similarity = max(ink_similarity(raster,rect,dx,dy)
                                 for dx in range(-3,4) for dy in range(delta-3,delta+4))
                if similarity>=.88:
                    candidates[key].append(dict(part=key[0],measure=key[1],note_index=key[2],
                        glyph=char,word=a,sentence=sentence,peer_word=b,donor=other,
                        number=lyrics[0].get('number','1'),source_rect=rect,overlap=similarity,
                        multi=len(text)>1))

    accepted = {key: max(choices, key=lambda c:c['overlap'])
                for key,choices in candidates.items()
                if len({(c['glyph'],c['number'],c['word'],c['sentence']) for c in choices})==1}
    phrases = {c['sentence'] for c in accepted.values() if c['multi']}
    return {key:c for key,c in accepted.items() if c['sentence'] in phrases}



def split_with_peer(words, slots, raster, current):
    """Split a word only when an independently attached peer supplies every glyph."""
    by_text = defaultdict(list)
    for identity, columns in slots.items():
        by_text[words[identity][2]].append((identity, columns))
    proposed = defaultdict(list)
    for text, group in by_text.items():
        for (a, target),(b, donor) in product(group,repeat=2):
            if not 1<=abs(int(words[a][0])-int(words[b][0]))<=3 or target[0][0][0]==donor[0][0][0]:
                continue
            if any(abs(t[1]-d[1])>8 for t,d in zip(target,donor)):
                continue
            keys=[t[0] for t in target]
            if any(key[:2]!=keys[0][:2] for key in keys):
                continue
            notes=[current[key]['node'] for key in keys]
            anchors=[(key,lyric) for key,n in zip(keys,notes) for lyric in n.findall('lyric')]
            if len(anchors)!=1:
                continue
            owner,label=anchors[0]
            if label.findtext('text')!=text or len(label)!=2 or label.findtext('syllabic')!='single':
                continue
            peers=[current[d[0]]['node'].findall('lyric') for d in donor]
            if any(len(lyrics)!=1 or lyrics[0].findtext('text')!=char for lyrics,char in zip(peers,text)):
                continue
            if len({lyrics[0].get('number','1') for lyrics in peers})!=1:
                continue
            delta=round(median((d[3][1]+d[3][3]-t[3][1]-t[3][3])/2 for t,d in zip(target,donor)))
            quality=max(min(ink_similarity(raster,t[3],dx,dy) for t in target)
                        for dx in range(-3,4) for dy in range(delta-3,delta+4))
            if quality>=.88:
                proposed[owner].append((keys, label, dict(part=keys[0][0],measure=keys[0][1],
                    word=text, indices=[k[2] for k in keys], owner=owner[2], source_item=a, peer_item=b,
                    overlap=quality, boxes=[t[3] for t in target])))
    for owner, choices in proposed.items():
        if len({(tuple(keys),c['word'],c['source_item']) for keys,_,c in choices})!=1:
            continue
        keys,label,change=max(choices,key=lambda entry:entry[2]['overlap'])
        if any(current[k]['node'].find('lyric') is not None for k in keys if k!=owner):
            continue
        current[owner]['node'].remove(label)
        for key,char in zip(keys,change['word']):
            new=deepcopy(label)
            new.find('text').text=char
            n=current[key]['node']
            i=next((i for i,c in enumerate(n) if c.tag in ('play','listen')),len(n))
            n.insert(i,new)
        yield change


def covered_suffix(system, parent, child_ids, staff_lines, slots, current, options, ledger):
    nodes = with_bounds(system)
    if not child_ids or len(set(child_ids)) != len(child_ids):
        return None
    staff = parent.get('staff')
    items = [nodes.get(child) for child in child_ids]
    if any(n is None or n.tag!='word' or n.get('staff')!=staff for n in items):
        return None
    items.sort(key=lambda n:coordinate(n)[0])
    tokens = [''.join(n.get('value','').split()) for n in items]
    if not all(map(is_han,tokens)):
        return None
    expected = []
    for item,token in zip(items,tokens):
        cols = slots.get(item.get('id'), [])
        agrees = (len(cols)==len(token) and all(
            (options[key]['glyph'] if key in options else current[key]['node'].findtext('lyric/text'))==char
            for key,_,char,_ in cols))
        expected.append(agrees)
    if not expected[-1]:
        return None
    owners = {c['part'] for c in options.values() if c['sentence']==parent.get('id')}
    if len(owners)!=1:
        return None
    part = owners.pop()
    x,y,w,h=coordinate(parent)
    first = slots.get(items[0].get('id'), [])
    if first:
        key = first[0][0]
        if key[0]!=part:
            return None
    else:
        stacks = [(int(s.get('left')),int(s.get('right'))) for s in system.findall('stack')]
        stack = [(left,right) for left,right in stacks if left<=x<right]
        if len(stack)!=1:
            return None
        left,right = stack[0]
        anchors = [(r['part'],r['measure'],int(r['note_index'])) for r in ledger
                   if r['part']==part and r['staff']==staff
                   and left<=int(r['x'])+int(r['w'])/2<right]
        if len({k[1] for k in anchors})!=1:
            return None
        key = next((k for k in anchors if k in current),None)
        if key is None:
            return None
    bar = key[1]
    m = current[key]['measure']
    lines=staff_lines[staff]
    default_y=-(y+h-lines[0])*40/(lines[-1]-lines[0])
    matches=[]
    for d in m.findall('direction'):
        if d.attrib!={'placement':'below'} or len(d)!=1 or d[0].tag!='direction-type' or len(d[0])!=1:
            continue
        word=d[0][0]
        if word.tag!='words' or ''.join((word.text or '').split())!=''.join(tokens):
            continue
        if word.get('default-y') is not None and abs(float(word.get('default-y'))-default_y)<=2.25:
            matches.append((d,word))
    if len(matches)!=1:
        return None
    prefix = sum(map(len,tokens[:max((i+1 for i,v in enumerate(expected) if not v),default=0)]))
    direction, word = matches[0]
    retained = ''
    if prefix:
        remaining=prefix
        for i,char in enumerate(word.text):
            remaining-=not char.isspace()
            if remaining==0:
                retained=word.text[:i+1]
                break
    return m,direction,word,retained,part,bar


def recover(root, job):
    job = Path(job)
    state = job/'source-native-notation.json'
    source = job/'raw/input.omr'
    report = dict(state='insufficient_evidence', changes=[], directions=[], packed=[])
    if not state.is_file() or not source.is_file():
        return root, report
    parts = vocal_parts(root)
    if len(parts)<2:
        return root, dict(report,state='no_candidate')
    info=json.loads(state.read_text('utf8'))
    accepted, native_xml = (Path(info[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected = info.get('accepted_native_sha256')
    if not expected or sha(accepted)!=expected:
        raise ValueError('misclassified lyrics: native book hash mismatch')
    by_unit=defaultdict(lambda:defaultdict(list))
    for row in rows(native_xml.with_name('note-ledger.tsv')):
        if row['part'] in parts and row['shape'].startswith('NOTEHEAD') and row['x']:
            by_unit[row['unit']][row['system']].append(row)
    clone=deepcopy(root)
    current,_=_score_events(clone)
    native,_=_score_events(score(native_xml))
    with ZipFile(source) as original, ZipFile(accepted) as control:
        for unit, groups in by_unit.items():
            name=f'sheet#{unit}/sheet#{unit}.xml'
            raw_page=ET.fromstring(original.read(name))
            accepted_page=ET.fromstring(control.read(name))
            raster=None
            for system, ledger in groups.items():
                graph=raw_page.find(f'page/system[@id="{system}"]')
                verified=accepted_page.find(f'page/system[@id="{system}"]')
                if graph is None or verified is None:
                    continue
                words,parents,children,staff_lines,packed=read_words(graph,ledger,current,native,with_bounds(verified))
                if not (words or packed):
                    continue
                if raster is None:
                    image=job/f'unit-{int(unit):03d}.png'
                    if {r['source_sha256'] for seq in groups.values() for r in seq}!={sha(image)}:
                        raise ValueError('misclassified lyrics: source raster hash mismatch')
                    with Image.open(image) as im:
                        raster=np.asarray(im.convert('L'))
                slots={id_:col for id_,word in words.items() if (col:=glyph_columns(word,raster))}
                options=proposals(words,slots,raster,current)
                edits=[]
                for owner in sorted({c['sentence'] for c in options.values()}):
                    edit=covered_suffix(graph,parents[owner],children[owner],staff_lines,slots,current,options,ledger)
                    if edit is not None:
                        edits.append(edit)
                for key, change in options.items():
                    n=current[key]['node']
                    lyric=ET.Element('lyric',number=change['number'],placement='below')
                    ET.SubElement(lyric,'syllabic').text='single'
                    ET.SubElement(lyric,'text').text=change['glyph']
                    i=next((i for i,c in enumerate(n) if c.tag in ('play','listen')),len(n))
                    n.insert(i,lyric)
                    report['changes'].append(dict(change,unit=unit,system=system))
                for m,d,w,retained,part,bar in edits:
                    report['directions'].append(dict(part=part,measure=bar,text=w.text,retained=retained))
                    if retained:
                        w.text=retained
                    else:
                        m.remove(d)
                packed_slots={id_:col for id_,word in packed.items() if (col:=glyph_columns(word,raster,min_height=.45,min_ink=.075))}
                for change in split_with_peer(packed,packed_slots,raster,current):
                    report['packed'].append(dict(change,unit=unit,system=system))
    if report['changes'] or report['packed']:
        return clone,dict(report,state='restored')
    return root,dict(report,state='no_changes')

