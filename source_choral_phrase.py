
"""Split an OCR-merged Chinese phrase using source character and note coordinates."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile
import csv
import hashlib
import json
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from source_lyric_roles import _music_signature, _score_events
from source_vocal_candidates import merged_phrase


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box(node):
    return [int(node.find('bounds').get(k)) for k in 'xywh']


def independent_characters(phrase, columns, scope, font_row, ocr_row):
    selected=[]
    if len(columns)<3 or any(a>=b for a,b in zip(columns,columns[1:])):
        return []
    tolerance=min(13,.24*min(b-a for a,b in zip(columns,columns[1:])))
    for letter,column in zip(phrase,columns,strict=True):
        rendered=[v for v in font_row['characters'] if abs(v['center']-column)<=tolerance]
        raw=[v for v in ocr_row['characters'] if abs(v['bounds'][0]+v['bounds'][2]/2-column)<=tolerance]
        if len(rendered)!=1 or len(raw)!=1 or rendered[0]['text']!=letter or raw[0]['text']!=letter:
            return []
        refs={(v['reference']['unit'],v['reference']['system'],v['reference']['staff'])
              for v in rendered[0].get('font_matches',[]) if v['text']==letter and v['quality']>=.965}
        if raw[0]['confidence']<.97 or not (refs-{scope}):
            return []
        selected.append(dict(text=letter,bounds=raw[0]['bounds'],confidence=raw[0]['confidence'],
                             note_x=round(column,2),peer_font_references=sorted(refs-{scope})))
    return selected


def above_staff_word(system,staff,column):
    line=staff.find('lines/line')
    top=next(float(v.get('y')) for v in sorted(line.findall('point'),
              key=lambda v: abs(float(v.get('x'))-column)))
    possible=[]
    for word in system.findall('sig/inters/*'):
        if word.tag not in ('word','lyric-item') or word.get('value','').casefold()!='marcato':
            continue
        x,y,width,height=box(word)
        if abs(x-column)<=11 and 0<=top-y-height<=75 and 2.5<=width/height<=3.5:
            possible.append(word)
    return possible[0] if len(possible)==1 else None


def word_pixels(image,evidence,reference):
    x,y,w,h=box(evidence)
    rx,ry,rw,rh=box(reference)
    if min(x-9,y-9,rx,ry)<0 or x+w+14>image.shape[1] or y+h+9>image.shape[0]:
        return []
    scores=[]
    for threshold in (160,208):
        ink=(image[ry:ry+rh,rx:rx+rw]<threshold).astype('uint8')
        sample=(image[y-9:y+h+9,x-9:x+w+14]<threshold).astype('uint8')
        if not 0.12<ink.mean()<.70 or any(a<b for a,b in zip(sample.shape,ink.shape)):
            return []
        matches=cv2.matchTemplate(sample,ink,cv2.TM_CCOEFF_NORMED)
        _,score,_,(dx,dy)=cv2.minMaxLoc(matches)
        if score<.95 or abs(dx-9)>4 or abs(dy-9)>5:
            return []
        scores.append(dict(threshold=threshold,correlation=score,offset=[dx-9,dy-9]))
    return scores


def link_false_lyric(system,note_row,native_index,text):
    chord=note_row[native_index]['chord']
    ids={rel.get('target') for rel in system.findall('sig/relations/relation')
         if rel.get('source')==chord and rel.find('chord-syllable') is not None}
    return any(n.get('id') in ids and n.get('value','').casefold()==text
               for n in system.findall('sig/inters/lyric-item'))


def add_marcato(bar,note):
    direction=ET.Element('direction',placement='above')
    dt=ET.SubElement(direction,'direction-type')
    ET.SubElement(dt,'words',{'font-style':'italic'}).text='marcato'
    bar.insert(list(bar).index(note), direction)


def split_lyrics(bar,notes,glyphs):
    for note,glyph in zip(notes,glyphs,strict=True):
        current=note.find('lyric')
        if current is not None:
            note.remove(current)
        lyric=ET.Element('lyric',number='1',placement='below')
        ET.SubElement(lyric,'syllabic').text='single'
        ET.SubElement(lyric,'text').text=glyph['text']
        at=next((i for i,element in enumerate(note) if element.tag in ('play','listen')),len(note))
        note.insert(at,lyric)


def repair(root,native,book,source,ledger,font_report,ocr_report):
    if font_report['source_book_sha256']!=digest(book):
        raise ValueError('Character evidence belongs to a different native score')
    def keyed(report):
        return {(v['unit'],v['system'],v['staff']):v for v in report['scopes']}
    fonts,ocr=keyed(font_report),keyed(ocr_report)
    before,_=_score_events(root)
    native_events,_=_score_events(native)
    address={}
    with ledger.open(encoding='utf8') as file:
        for row in csv.DictReader(file,delimiter='\t'):
            if row['repeat_copy']!='false':
                continue
            key=(row['part'],row['measure'],int(row['note_index']))
            address.setdefault(key,[]).append(row)
    accepted=deepcopy(root)
    pages={}
    systems={}
    changes=[]
    with ZipFile(book) as archive:
        for part in accepted.findall('part'):
            bars=part.findall('measure')
            counts=Counter(v.get('number') for v in bars)
            for physical,bar in enumerate(bars,1):
                measure=bar.get('number')
                if counts[measure]!=1 or any(bar.find(t) is not None for t in ('backup','forward')):
                    continue
                notes=bar.findall('note')
                content=merged_phrase(notes)
                if content is None:
                    continue
                phrase,indices,lyrics=content
                keys=[(part.get('id'),measure,i) for i in indices]
                if any(len(address.get(k,[]))!=1 or k not in before or k not in native_events
                       or _music_signature(before[k])!=_music_signature(native_events[k])
                       or before[k]['node'].get('default-x')!=native_events[k]['node'].get('default-x') for k in keys):
                    continue
                rows={key[2]:address[key][0] for key in keys}
                scopes={(int(v['unit']),v['system'],v['staff']) for v in rows.values()}
                if len(scopes)!=1:
                    continue
                scope=next(iter(scopes))
                if scope not in fonts or scope not in ocr:
                    continue
                unit,system_id,staff_id=scope
                if unit not in pages:
                    image=(source/f'unit-{unit:03d}.png').read_bytes()
                    pages[unit]=(hashlib.sha256(image).hexdigest(),cv2.imdecode(np.frombuffer(image,np.uint8),0))
                sha,image=pages[unit]
                if image is None or {sha}!={fonts[scope]['source_sha256'],ocr[scope]['source_sha256'],
                                            *(v['source_sha256'] for v in rows.values())}:
                    continue
                columns=[int(rows[i]['x'])+int(rows[i]['w'])/2 for i in indices]
                glyphs=independent_characters(phrase,columns,scope,fonts[scope],ocr[scope])
                if not glyphs:
                    continue
                if scope[:2] not in systems:
                    systems[scope[:2]]=ET.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml')).find(
                        f"page/system[@id='{system_id}']")
                system=systems[scope[:2]]
                staff=system.find(f"part/staff[@id='{staff_id}']")
                word=above_staff_word(system,staff,columns[0])
                reference=[v for v in system.findall('sig/inters/word') if v.get('value','').casefold()=='marcato'
                           and abs(box(v)[0]-columns[0])<=11]
                if word is None or len(reference)!=1:
                    continue
                matches=word_pixels(image,word,reference[0])
                if not matches or any(not link_false_lyric(system,rows,i,text) for i,text in lyrics if text=='marcato'):
                    continue
                old=[v for v in bar.findall('direction') if v.findtext('direction-type/words','').casefold()=='marcato']
                if old and (len(old)!=1 or old[0].find('offset') is not None
                            or list(bar).index(old[0])+1!=list(bar).index(notes[indices[0]])):
                    continue
                if not old:
                    add_marcato(bar,notes[indices[0]])
                split_lyrics(bar,[notes[i] for i in indices],glyphs)
                changes.append(dict(part=part.get('id'),measure=measure,physical_measure=physical,
                                    glyphs=glyphs,word_bbox=box(word),word_match=matches,
                                    word_already_at_first_note=bool(old),
                                    note_indices=indices,word_native_id=word.get('id')))
    return (accepted if changes else root),changes

