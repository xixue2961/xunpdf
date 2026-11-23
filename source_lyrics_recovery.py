"""Source-backed recovery of a missing CJK lyric line after logical parts stabilize.
Existing lyrics are never rewritten; all note/voice/timing content is immutable.
"""
from pathlib import Path
from collections import Counter, defaultdict
from fractions import Fraction
import csv,copy,hashlib,io,json,math,re,xml.etree.ElementTree as ET,zipfile
import numpy as np
from PIL import Image
from cjk_word_pixels import refine_cjk_word, refine_cjk_word_on_heads

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def han(c):return len(c)==1 and '\u3400'<=c<='\u9fff'
def musical_signature(note,div):
    return (tuple(note.findtext('pitch/'+tag,'') for tag in ('step','alter','octave')) if note.find('pitch') is not None else None,
            Fraction(note.findtext('duration','0'))/div,note.find('chord') is not None,
            note.find('grace') is not None,note.findtext('voice'),note.findtext('staff','1'))
def repeated_measure_addresses(root):
    """A printed/exported measure label may refer to multiple physical measures."""
    return {address for address, count in Counter(
        (part.get('id'), measure.get('number'))
        for part in root.findall('part') for measure in part.findall('measure')
    ).items() if count > 1}


def addresses(root):
    result={}
    for part in root.findall('part'):
        div=1
        for measure in part.findall('measure'):
            if measure.findtext('attributes/divisions'):div=int(measure.findtext('attributes/divisions'))
            for i,note in enumerate(measure.findall('note')):result[(part.get('id'),measure.get('number'),i)]=(note,div)
    return result

def flagged_parts(root,hint):
    names=set()
    for entry in hint.split(';'):
        fields=entry.strip().split(':')
        if 'lyrics' in fields[2:]:names.update(v.strip().casefold() for v in fields[0].split('|'))
    return {part.get('id') for part in root.findall('part-list/score-part') if part.findtext('part-name','').strip().casefold() in names}

def _pixel_lines(data,text,heads=()):
    gray=np.asarray(Image.open(io.BytesIO(data)).convert('L'));blocks=[];block=None;word=None
    for line in text.splitlines():
        cols=line.split('\t')
        if cols[0]=='L' and len(cols)==7:
            block=dict(text=cols[-1],confidence=float(cols[1]),words=[]);blocks.append(block)
        elif cols[0]=='W' and len(cols)>=6 and block is not None:
            word=[];block['words'].append(word)
        elif cols[0]=='C' and len(cols)==6 and word is not None:word.append((cols[-1],*map(int,cols[1:5])))
    allhan=[c for b in blocks for w in b['words'] for c in w if han(c[0])]
    if len(allhan)<4:return [],[]
    baseline=float(np.median([c[2]+c[4] for c in allhan]));height=float(np.median([c[4] for c in allhan]));selected=[];pending=[]
    if height<8:return [],[dict(reason='source_lyric_text_too_small')]
    for block in blocks:
        chars=[c for w in block['words'] for c in w if han(c[0])]
        if not chars or abs(np.median([c[2]+c[4] for c in chars])-baseline)>.25*height:continue
        expected=''.join(c for c in block['text'] if han(c))
        decoded=''.join(c[0] for word in block['words'] for c in word if han(c[0]))
        if expected!=decoded:
            pending.append(dict(reason='source_lyric_decoded_character_sequence_mismatch',text=block['text']));continue
        if any(any(han(c[0]) for c in word) and not all(han(c[0]) for c in word) for word in block['words']):
            pending.append(dict(reason='source_mixed_script_word_needs_review',text=block['text']));continue
        if block['confidence']<.90:
            pending.append(dict(reason='source_lyric_ocr_confidence',text=block['text']));continue
        for word in block['words']:
            if not word or not all(han(c[0]) for c in word):continue
            if len(word)==1:
                ch,x,y,w,h=word[0]
                if not (0<=x<x+w<=gray.shape[1] and 0<=y<y+h<=gray.shape[0]):continue
                ink=gray[y:y+h,x:x+w]<160
                refined=word if .08<=float(ink.mean()) and .35*height<=w<=1.4*height else None
            else:refined=refine_cjk_word(gray,word)
            head_witness = None
            if refined is None and len(word) >= 4 and block['confidence'] >= .995:
                refined = refine_cjk_word_on_heads(gray, word, heads)
                head_witness = (min(c[1] for c in word), len(word)) if refined else None
            elif refined is not None and len(word) == 2 and block['confidence'] >= .997:
                evidence = refine_cjk_word_on_heads(gray, word, heads)
                if evidence is not None and all(abs(a - b) <= .13 * height
                        for (_, *before), (_, *after) in zip(refined, evidence)
                        for a, b in zip(before, after)):
                    head_witness = (min(c[1] for c in word), len(word))
            if refined is None:
                pending.append(dict(reason='source_cjk_character_boundaries_ambiguous',text=''.join(c[0] for c in word)));continue
            for ch,x,y,w,h in refined:
                selected.append(dict(text=ch,box=[x,y,w,h],confidence=block['confidence'],font_height=height,
                                     boundary_uncertainty=max(3,round(.10*height)),head_witness=head_witness))
    selected.sort(key=lambda v:v['box'][0])
    if len(selected)<4:return [],pending
    return selected,pending

def scan_missing_lyrics(root,control,book,source,ledger,hint,ocr,cancel=None,evidence=None):
    source=Path(source);book=Path(book);ledger=Path(ledger);evidence=Path(evidence) if evidence else None
    if evidence:evidence.mkdir(exist_ok=True)
    plan=dict(input_xml_sha256=hashlib.sha256(ET.tostring(root)).hexdigest(),source_book_sha256=sha(book),ledger_sha256=sha(ledger),changes=[],pending=[],scopes=[],scope='only initially missing CJK lyrics, completed logical-part mapping and physical source glyphs')
    wanted=flagged_parts(root,hint)
    if not wanted:return plan
    ambiguous = repeated_measure_addresses(root) | repeated_measure_addresses(control)
    current=addresses(root);before=addresses(control);rows=list(csv.DictReader(ledger.open(encoding='utf8'),delimiter='\t'));groups=defaultdict(list)
    for row in rows:
        if row['part'] in wanted and row['repeat_copy']=='false':groups[(int(row['unit']),row['system'],row['staff'],row['part'])].append(row)
    staff_info={}
    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r'sheet#\d+/sheet#\d+\.xml',name):continue
            unit=int(name.split('/')[0][6:]);sheet=ET.fromstring(archive.read(name))
            for system in sheet.findall('.//system'):
                staves=system.findall('.//staff')
                for index,staff in enumerate(staves):
                    lines=staff.findall('lines/line');ys=[float(line.find('point').get('y')) for line in lines]
                    if len(ys)<2:continue
                    interline=float(np.median(np.diff(ys)));bottom=ys[-1]+3;end=bottom+7.5*interline
                    if index+1<len(staves):end=min(end,float(staves[index+1].find('lines/line/point').get('y'))-2*interline)
                    staff_info[(unit,system.get('id'),staff.get('id'))]=dict(left=int(staff.get('left')),right=int(staff.get('right')),top=round(bottom),bottom=round(end),interline=interline)
    for scope,records in sorted(groups.items()):
        if cancel is not None and cancel.is_set():raise InterruptedError('歌词复核已取消')
        unit,system,staff,pid=scope;info=staff_info.get((unit,system,staff))
        if info is None:plan['pending'].append(dict(scope=scope,reason='source_staff_geometry_unavailable'));continue
        keys={(r['part'],r['measure'],int(r['note_index'])) for r in records}
        repeated = sorted({key[1] for key in keys if key[:2] in ambiguous})
        if repeated:
            plan['pending'].append(dict(scope=scope, reason='duplicate_measure_label_ambiguous', measures=repeated))
            continue
        if not keys<=current.keys() or not keys<=before.keys():plan['pending'].append(dict(scope=scope,reason='native_source_note_mapping_unavailable'));continue
        # A previously recognized Chinese line is frozen, including incomplete lines.
        if any(any(han(c) for c in (text.text or '')) for key in keys for text in current[key][0].findall('lyric/text')):
            plan['scopes'].append(dict(scope=scope,state='existing_cjk_lyrics_preserved'));continue
        chords=defaultdict(list)
        for row in records:chords[row['measure'],row['chord']].append(row)
        targets=[]
        for (measure,chord),rr in chords.items():
            index=min(int(v['note_index']) for v in rr);key=(pid,measure,index);note=current[key][0]
            if note.find('pitch') is None or note.find('grace') is not None or any(t.get('type')=='stop' for t in note.findall('tie')):continue
            targets.append(dict(key=key,chord=chord,x=float(np.mean([int(v['x'])+int(v['w'])/2 for v in rr])),heads=[v['head'] for v in rr],expected_note_sha256=hashlib.sha256(ET.tostring(note)).hexdigest()))
        if len(targets)<4:continue
        image_path=source/f'unit-{unit:03d}.png';source_hash=sha(image_path)
        if any(r['source_sha256']!=source_hash for r in records):raise ValueError('source lyric image hash differs from native ledger')
        with Image.open(image_path) as image:
            box=[info['left'],info['top'],min(image.width,info['right']),min(image.height,info['bottom'])]
            if box[2]<=box[0] or box[3]<=box[1]:continue
            stream=io.BytesIO();image.crop(box).convert('RGB').save(stream,format='PNG');data=stream.getvalue()
        stem=f'unit{unit}-system{system}-staff{staff}-{pid}'
        if evidence:(evidence/(stem+'.png')).write_bytes(data)
        text=ocr(data)
        if evidence:(evidence/(stem+'.tsv')).write_text(text,encoding='utf8')
        words,pending=_pixel_lines(data,text,[t['x']-box[0] for t in targets]);plan['pending'].extend(dict(scope=scope,**v) for v in pending)
        if not words:continue
        if any(musical_signature(*current[key])!=musical_signature(*before[key]) for key in keys):
            plan['pending'].append(dict(scope=scope,reason='caller_note_music_differs_from_control'));continue
        mapped=[];used=set();failed=[];witnessed=defaultdict(set)
        for word in words:
            x,y,w,h=word['box'];cx=box[0]+x+w/2;ranked=sorted(targets,key=lambda t:abs(t['x']-cx));target=ranked[0];distance=abs(target['x']-cx);margin=abs(ranked[1]['x']-cx)-distance
            if distance>max(6,.80*info['interline']) or margin<max(8,.45*info['interline'],2*word['boundary_uncertainty']):failed.append(dict(reason='source_lyric_head_alignment_ambiguous',text=word['text']));continue
            key=tuple(target['key'])
            if key in used:failed.append(dict(reason='multiple_characters_claim_same_note',text=word['text']));continue
            if current[key][0].find('lyric') is not None:failed.append(dict(reason='existing_note_lyric_preserved',text=word['text']));continue
            used.add(key);mapped.append(dict(scope=scope,part=pid,measure=key[1],note_index=key[2],text=word['text'],confidence=word['confidence'],source_sha256=source_hash,source_box=[box[0]+x,box[1]+y,w,h],head_ids=target['heads'],head_center_x=target['x'],alignment_distance=distance,alignment_margin=margin,expected_note_sha256=target['expected_note_sha256']))
            if word['head_witness']:witnessed[word['head_witness']].add(key)
        if failed:
            plan['pending'].extend(dict(scope=scope,**v) for v in failed)
        if failed or pending:
            # Independently verified words can survive unrelated unresolved text on the same staff.
            intact={addr for (_, count), addresses in witnessed.items() if len(addresses)==count for addr in addresses}
            mapped=[item for item in mapped if (item['part'],item['measure'],item['note_index']) in intact]
            if not mapped:continue
        if any(a['source_box'][0]>=b['source_box'][0] for a,b in zip(mapped,mapped[1:])):raise ValueError('lyric source order is not strictly monotone')
        plan['changes'].extend(mapped);plan['scopes'].append(dict(scope=scope,state='source_lyric_word_verified' if failed or pending else 'source_lyric_line_verified',characters=len(mapped)))
    if sha(book)!=plan['source_book_sha256'] or sha(ledger)!=plan['ledger_sha256']:raise ValueError('source lyric evidence changed while scanning')
    return plan

def apply_missing_lyrics(root,plan,book,source,ledger):
    if hashlib.sha256(ET.tostring(root)).hexdigest()!=plan['input_xml_sha256'] or sha(book)!=plan['source_book_sha256'] or sha(ledger)!=plan['ledger_sha256']:raise ValueError('stale source lyric evidence')
    ambiguous = repeated_measure_addresses(root)
    fixed=copy.deepcopy(root);notes=addresses(fixed);image_hashes={};used=set()
    for change in plan['changes']:
        unit=int(change['scope'][0]);path=Path(source)/f'unit-{unit:03d}.png'
        if unit not in image_hashes:image_hashes[unit]=sha(path)
        if image_hashes[unit]!=change['source_sha256']:raise ValueError('source lyric image changed')
        key=change['part'],change['measure'],int(change['note_index'])
        if key[:2] in ambiguous:
            raise ValueError('duplicate measure label cannot identify lyric target')
        note=notes[key][0]
        if key in used or note.find('lyric') is not None or not han(change['text']):raise ValueError('duplicate or nonempty target lyric')
        used.add(key)
        if hashlib.sha256(ET.tostring(note)).hexdigest()!=change['expected_note_sha256']:raise ValueError('source lyric target note changed')
        lyric=ET.Element('lyric',number='1');ET.SubElement(lyric,'syllabic').text='single';ET.SubElement(lyric,'text').text=change['text'];insert=next((i for i,e in enumerate(note) if e.tag in ('play','listen')),len(note));note.insert(insert,lyric)
    # Removing only the newly added lyrics must restore the complete caller XML tree.
    restored=copy.deepcopy(fixed);restored_notes=addresses(restored)
    for key in used:restored_notes[key][0].remove(restored_notes[key][0].find('lyric'))
    if ET.tostring(restored)!=ET.tostring(root):raise ValueError('lyric recovery changed non-lyric content')
    return fixed