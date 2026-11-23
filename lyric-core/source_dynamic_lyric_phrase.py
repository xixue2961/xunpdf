"""Recover a joined lyric when a proven, repeated dynamic interrupted alignment."""
import copy
import csv
import re
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from source_lyric_roles import _role_anchor_signature, _score_events, sha


def _connected_marks(plan):
    marks = {(item['unit'], item['system'], item['item']): item
             for item in plan['proposals'] if item['state'] == 'source_proven_repeated_p_dynamic'}
    links = defaultdict(set)
    for address, item in marks.items():
        peer = (*address[:2], item['peer_role_item'])
        if peer in marks:
            links[address].add(peer)
            links[peer].add(address)
    while marks:
        first = next(iter(marks))
        path = [first]
        group = []
        while path:
            address = path.pop()
            if address in marks:
                group.append(marks.pop(address))
                path.extend(sorted(links[address]))
        if len(group) > 1:
            yield group


def _read_phrase(scope, notes, phrase):
    if scope is None:
        return None
    readings = []
    for text, (center, _, _) in zip(phrase, notes, strict=True):
        candidates = [v for v in scope['characters'] if
                      abs(v['bounds'][0] + v['bounds'][2] / 2 - center) <= .45 * v['bounds'][3]]
        if len(candidates) != 1:
            return None
        reading = candidates[0]
        if reading['confidence'] < .95 or reading['text'] + (reading.get('punctuation') or '') != text:
            return None
        readings.append(reading)
    return readings


def _match_phrase(image, readings, ref_mark, target_mark, target_notes):
    delta_x, delta_y = [a-b for a,b in zip(target_mark['full_bounds'], ref_mark['full_bounds'])][:2]
    positions = []
    for ref, (center, _, _) in zip(readings, target_notes, strict=True):
        x,y,w,h = ref['bounds']
        if w > 1.2*h:
            # Oversized OCR boxes can include descenders from the dynamic.
            x += round(.24*w)
            w -= round(.30*w)
        y += round(.29*h)
        h -= round(.35*h)
        tolerance = round(.18*h)
        x_slack = max(1, round(.08*h))
        x0,y0 = x+delta_x-x_slack,y+delta_y-tolerance
        if min(x0,y0) < 0 or abs(x+w/2+delta_x-center)>.45*ref['bounds'][3]:
            return None
        source = image[y:y+h,x:x+w]
        search = image[y0:y0+h+2*tolerance,x0:x0+w+2*x_slack]
        if source.shape!=(h,w) or search.shape!=(h+2*tolerance,w+2*x_slack):
            return None
        dual = []
        for threshold in (160,208):
            template = (source < threshold).astype(np.uint8)
            target = (search < threshold).astype(np.uint8)
            if template.var()==0:
                return None
            _, score, _, (i,j) = cv2.minMaxLoc(cv2.matchTemplate(target,template,cv2.TM_CCOEFF_NORMED))
            found = target[j:j+h,i:i+w]
            shared = np.logical_and(found,template).sum()
            if score<.95 or shared/max(1,found.sum())<.975 or shared/template.sum()<.972:
                return None
            dual.append((x0+i,y0+j))
        if max(abs(a-b) for a,b in zip(*dual))>1:
            return None
        positions.append(list(dual[0])+[w,h])
    return positions


def repair_joined_words(score, control, book, source, ledger, readings, plan):
    groups = list(_connected_marks(plan))
    if not groups:
        return score, []
    if sha(book)!=plan['source_book_sha256'] or sha(ledger)!=plan['head_ledger_sha256']:
        raise ValueError('dynamic evidence source or ledger changed')
    source = Path(source)
    scopes = {(g['unit'],g['system'],g['staff']):g for g in readings['scopes']}
    with Path(ledger).open(encoding='utf8') as stream:
        rows=defaultdict(list)
        for row in csv.DictReader(stream,delimiter='\t'):
            if row['repeat_copy']=='false':
                rows[(row['part'],row['measure'],int(row['note_index']))].append(row)

    result = copy.deepcopy(score)
    events, directions = _score_events(result)
    anchors, _ = _score_events(control)
    changes = []
    for group in groups:
        trial = []
        source_digest = {mark['source_sha256'] for mark in group}
        img_file = source / f"unit-{group[0]['unit']:03d}.png"
        if source_digest != {sha(img_file)}:
            raise ValueError('original source page changed')
        raster = cv2.imdecode(np.fromfile(img_file,np.uint8),0)
        for mark in group:
            address = mark['target_note']
            pid, mid, start = address['part'],address['measure'],int(address['note_index'])
            lead = events.get((pid,mid,start))
            next_note = events.get((pid,mid,start+1))
            if lead is None or next_note is None:
                continue
            pp = [d for d in directions if (d['part'],d['measure'],d['start'])==(pid,mid,lead['start'])
                  and d['node'].find('direction-type/dynamics/pp') is not None]
            if len(pp)!=1:
                continue
            lyrics = lead['node'].findall('lyric')+next_note['node'].findall('lyric')
            if len(lyrics)!=1:
                continue
            original = lyrics[0]
            match = re.fullmatch(r'([\u3400-\u9fff]{2,8})([，。；、！？：]?)',original.findtext('text',''))
            if not match or [n.tag for n in original] != ['syllabic','text']:
                continue
            phrase = list(match[1])
            phrase[-1] += match[2]
            notes = []
            for idx in range(start,start+len(phrase)):
                key=(pid,mid,idx)
                entry=events.get(key)
                if entry is None or key not in anchors or _role_anchor_signature(entry)!=_role_anchor_signature(anchors[key]):
                    break
                node=entry['node']
                if (node.find('pitch') is None or node.find('chord') is not None or node.find('grace') is not None
                    or node.findtext('voice','1')!=lead['node'].findtext('voice','1')
                    or node.find("tie[@type='stop']") is not None
                    or any(item is not original for item in node.findall('lyric'))):
                    break
                mapped=[row for row in rows[key] if row['staff']==address['staff'] and row['system']==address['system']
                        and row['unit']==address['unit'] and row['source_sha256']==address['source_sha256']]
                if len(mapped)!=1:
                    break
                row=mapped[0]
                center=float(row['x'])+float(row['w'])/2
                notes.append((center,key,node))
            if len(notes)!=len(phrase) or any(a[0]>=b[0] for a,b in zip(notes,notes[1:])):
                continue
            scope=scopes.get((mark['unit'],mark['system'],mark['source_staff']))
            trial.append((mark,notes,phrase,original,_read_phrase(scope,notes,phrase)))

        for mark,notes,phrase,original,_ in trial:
            evidence = [(ref_mark['source_staff'], positions) for ref_mark,_,ref_phrase,_,pixels in trial
                        if pixels and ref_phrase==phrase and ref_mark is not mark
                        and (positions:=_match_phrase(raster,pixels,ref_mark,mark,notes)) is not None]
            independent_readings = sum(pixels is not None and ref_phrase == phrase
                                       for _,_,ref_phrase,_,pixels in trial)
            if independent_readings < 2 or not evidence:
                continue
            owner=next(node for _,_,node in notes if original in node)
            owner.remove(original)
            for (_,key,node),text,region in zip(notes,phrase,evidence[0][1],strict=True):
                lyric=ET.Element('lyric',{'number':'1','placement':'below'})
                ET.SubElement(lyric,'syllabic').text='single'
                ET.SubElement(lyric,'text').text=text
                insertion=next((i for i,v in enumerate(node) if v.tag in ('play','listen')),len(node))
                node.insert(insertion,lyric)
                changes.append(dict(part=key[0],measure=key[1],note_index=key[2],after=text,
                                    source_bounds=region,physical_reference_staves=[v[0] for v in evidence],
                                    evidence_mode='aligned_phrase_below_repeated_dynamic'))
    return (result,changes) if changes else (score,[])


