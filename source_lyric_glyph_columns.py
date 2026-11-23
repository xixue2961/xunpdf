"""Expand native packed lyrics using independently read glyphs and note columns."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor


def is_han(text):
    return bool(text) and all('\u3400' <= c <= '\u9fff' for c in text)


def lyric_number(node):
    return node.get('number','1')


def choose_glyphs(frame,item,raw_word,measure_rows,raster):
    x,y,w,h=coordinate(item)
    baseline=frame['baseline']
    font=frame['font_height']
    if not 20<=font<=52 or not y-.13*font <= baseline <= y+h+.16*font:
        return None
    glyphs={}
    for q in frame['characters']:
        tx=q.get('text','')
        if not is_han(tx) or len(tx)!=1 or q.get('confidence',0)<.992 or q.get('punctuation'):
            continue
        gx,gy,gw,gh=tuple(q['bounds'])
        if (q.get('source_sha256')!=frame['source_sha256'] or gx<x-3 or gx+gw>x+w+3
                or gy<y-4 or gy+gh>y+h+4 or abs(gy+gh-baseline)>.32*font
                or not .62*font<=gw<=1.25*font or not .65*font<=gh<=1.21*font):
            continue
        where=tuple(q['bounds'])
        if where in glyphs and glyphs[where]['text']!=tx:
            return None
        glyphs[where]=q
    chars=[glyphs[k] for k in sorted(glyphs)]
    if len(chars)<2:
        return None
    places=[tuple(q['bounds']) for q in chars]
    if any(a[0]+a[2]>=b[0] for a,b in zip(places,places[1:])):
        return None

    actual=''.join(q['text'] for q in chars)
    kind='original_text_split'
    if actual!=raw_word:
        # A low-confidence native group may contain a single invented interior character.
        if (float(item.get('grade','1'))>=.65 or len(actual)+1!=len(raw_word)
                or not actual[0]==raw_word[0] or not actual[-1]==raw_word[-1]):
            return None
        deleted=[i for i in range(1,len(raw_word)-1) if raw_word[:i]+raw_word[i+1:]==actual]
        if len(deleted)!=1:
            return None
        pos=deleted[0]
        a,b=places[pos-1:pos+1]
        left,right=a[0]+a[2]+3,b[0]-3
        if not .9*font<=right-left or int((raster[y:y+h,left:right]<160).sum())!=0:
            return None
        kind='native_ocr_extra_character'

    targets=[]
    for q in chars:
        left,top,width,height=q['bounds']
        ref=left+width/2
        if int((raster[top:top+height,left:left+width]<128).sum())<.16*width*height:
            return None
        hits=[r for r in measure_rows if r['shape'] in ('NOTEHEAD_BLACK','NOTEHEAD_VOID')
              and abs(ref-int(r['x'])-int(r['w'])/2)<=.49*int(r['w'])]
        if len(hits)!=1:
            return None
        targets.append(int(hits[0]['note_index']))
    if targets!=list(range(targets[0],targets[0]+len(targets))):
        return None
    return actual,targets,places,kind


def project(notes,owner,lyric,chars,indices):
    number=lyric_number(lyric)
    if owner not in indices:
        return False
    candidates=[]
    for i in indices:
        n=notes[i]['node']
        if (n.find('pitch') is None or any(n.find(k) is not None for k in ('chord','grace','cue'))
                or any(v.get('type')=='stop' for v in n.findall('tie'))):
            return False
        others=[x for x in n.findall('lyric') if lyric_number(x)==number]
        if others != ([lyric] if i==owner else []):
            return False
        candidates.append(n)
    if any(a['start']>=b['start'] for a,b in zip((notes[i] for i in indices),(notes[i] for i in indices[1:]))):
        return False
    notes[owner]['node'].remove(lyric)
    for n,char in zip(candidates,chars):
        L=deepcopy(lyric)
        L.find('text').text=char
        at=next((i for i,c in enumerate(n) if c.tag in ('play','listen') or
                 (c.tag=='lyric' and lyric_number(c)>number)),len(n))
        n.insert(at,L)
    return True


def recover(root,job):
    job=Path(job)
    evidence=job/'source-syllable-evidence/source-characters.json'
    context=job/'source-native-notation.json'
    if not evidence.is_file() or not context.is_file():
        return root,dict(state='insufficient_evidence',changes=[])
    meta=json.loads(context.read_text('utf8'))
    book,xml=(Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if not meta.get('accepted_native_sha256') or sha(book)!=meta['accepted_native_sha256']:
        raise ValueError('lyric glyph column archive hash mismatch')
    data=json.loads(evidence.read_text('utf8'))
    frames={(str(v['unit']),str(v['system']),str(v['staff'])):v for v in data['scopes']}
    source_rows=defaultdict(list)
    for r in rows(xml.with_name('note-ledger.tsv')):
        if r['repeat_copy']=='false' and r['implicit']=='false':
            source_rows[r['part'],r['measure']].append(r)

    clone=deepcopy(root)
    ev,_=_score_events(clone)
    by_bar=defaultdict(list)
    for k,v in ev.items():
        by_bar[k[:2]].append(v)
    changes=[]
    current=None
    with ZipFile(book) as accepted, ZipFile(job/'raw/input.omr') as raw:
        for (part,bar),notes in by_bar.items():
            candidates=[(i,lyric) for i,n in enumerate(notes) for lyric in n['node'].findall('lyric')
                        if len(lyric)==2 and len(lyric.findall('text'))==1 and lyric.findtext('syllabic')=='single'
                        and is_han(lyric.findtext('text','')) and len(lyric.findtext('text'))>1
                        and lyric.get('placement','below')=='below']
            if not candidates:
                continue
            ledger=sorted(source_rows[part,bar],key=lambda r:int(r['note_index']))
            if len(ledger)!=len(notes) or [int(r['note_index']) for r in ledger]!=list(range(len(notes))):
                continue
            addr={(r['unit'],r['system'],r['staff']) for r in ledger}
            if len(addr)!=1:
                continue
            unit,system,staff=addr.pop()
            frame=frames.get((unit,system,staff))
            if frame is None or not all(r['source_sha256']==frame['source_sha256'] for r in ledger):
                continue
            ctl=graph(accepted,unit,system)
            source=graph(raw,unit,system)
            ctl_nodes={n.get('id'):n for n in ctl.findall('sig/inters/*') if n.find('bounds') is not None}
            raw_nodes={n.get('id'):n for n in source.findall('sig/inters/*') if n.find('bounds') is not None}
            if any(not native_anchor(r,ctl_nodes) or not native_anchor(r,raw_nodes) for r in ledger):
                continue
            if current!=unit:
                image=job/f'unit-{int(unit):03d}.png'
                if sha(image)!=frame['source_sha256']:
                    raise ValueError('lyric glyph column raster hash mismatch')
                with Image.open(image) as im:
                    raster=np.asarray(im.convert('L'))
                current=unit
            for i,lyric in candidates:
                packed=lyric.findtext('text')
                note_x=int(ledger[i]['x'])+int(ledger[i]['w'])/2
                native=[]
                for item in source.findall('sig/inters/lyric-item'):
                    if item.get('staff')!=staff or item.get('value')!=packed:
                        continue
                    x,y,w,h=coordinate(item)
                    if x-4<=note_x<=x+w+4 and 0<=x<x+w<=raster.shape[1] and 0<=y<y+h<=raster.shape[0]:
                        native.append(item)
                if len(native)!=1:
                    continue
                placement=choose_glyphs(frame,native[0],packed,ledger,raster)
                if not placement:
                    continue
                letters,indices,boxes,kind=placement
                if project(notes,i,lyric,letters,indices):
                    changes.append(dict(part=part,measure=bar,packed=packed,letters=letters,note_indices=indices,
                                        boxes=boxes,source_item=native[0].get('id'),mode=kind))
    return (clone if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)