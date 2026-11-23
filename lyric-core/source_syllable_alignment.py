"""Apply source-character lyrics only at uniquely matched physical note columns."""
from pathlib import Path
import sys,json,csv,copy,hashlib,zipfile,xml.etree.ElementTree as E,collections
import cv2,numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from source_lyric_roles import _score_events,_role_anchor_signature,_music_signature
from source_expression_overlay import canonical
from source_lyric_readings import matches_reference
from source_grouped_lyric_placements import apply_grouped_lyrics
from source_cjk_fonts import ALIGNED_DARK_THRESHOLDS, DARK_THRESHOLDS, THIN_INK_MODE, is_thin_glyph

def han(c):return len(c)==1 and '\u3400'<=c<='\u9fff'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def without_lyrics(root):
    root=copy.deepcopy(root)
    for note in root.findall('part/measure/note'):
        for lyric in list(note.findall('lyric')):note.remove(lyric)
    return canonical(root)
def apply_source_lyrics(original,control,book,source,ledger,font_plan,ocr_plan,*,after_font=None):
    if sha(book)!=font_plan['source_book_sha256']:raise ValueError('source book changed')
    source=Path(source);images={};nodes={};contains={}
    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            if '/sheet' not in name or not name.endswith('.xml'):continue
            u=int(name.split('/')[0][6:])
            for sy in E.fromstring(archive.read(name)).findall('page/system'):
                key=(u,sy.get('id'));nodes[key]={n.get('id'):n for n in sy.find('sig/inters')};members=collections.defaultdict(set)
                for edge in sy.find('sig/relations'):
                    if edge.find('containment') is not None:members[edge.get('source')].add(edge.get('target'))
                contains[key]=members
    rows=list(csv.DictReader(Path(ledger).open(encoding='utf8'),delimiter='\t'));by_scope=collections.defaultdict(list)
    for row in rows:
        if row['repeat_copy']=='false':by_scope[int(row['unit']),row['system'],row['staff']].append(row)
    digests={}
    for row in rows:
        key=int(row['unit']);digests.setdefault(key,set()).add(row['source_sha256'])
    def image(unit):
        if unit not in images:
            path=source/f'unit-{unit:03d}.png'
            if digests.get(unit)!={sha(path)}:raise ValueError('font reference original image changed')
            images[unit]=cv2.imdecode(np.fromfile(path,np.uint8),0)
        return images[unit]
    def verify_font(character,scope):
        other=set()
        aligned_thresholds = ALIGNED_DARK_THRESHOLDS.get(character['evidence_mode'])
        dark = aligned_thresholds is not None
        thin = character['evidence_mode'] == THIN_INK_MODE
        if thin and character['text'] != '一':
            raise ValueError('thin source glyph identity changed')
        for proof in character['font_matches']:
            ref=proof['reference']
            if ref['text']!=character['text'] or proof['text']!=character['text']:raise ValueError('font character identity changed')
            rk=(ref['unit'],ref['system'],ref['staff'])
            if rk==(scope['unit'],scope['system'],scope['staff']):raise ValueError('self-reference is not independent')
            other.add(rk)
            if dark and (rk[:2]!=(scope['unit'],scope['system']) or ref.get('source_type')!='independent_cropped_local_OCR' or abs(ref['bounds'][0]+ref['bounds'][2]/2-character['center'])>.55*scope['font_height']):
                raise ValueError('dark source reading lost its aligned independent staff witness')
            if ref.get('source_type')!='independent_cropped_local_OCR':
                n=nodes[rk[:2]].get(ref['item'])
                if n is None or n.get('value')!=ref['text'] or n.get('staff')!=ref['staff']:raise ValueError('native reference text identity changed')
            else:
                original_refs=[v for g in ocr_plan['scopes'] for v in g['characters'] if matches_reference(ref,v)]
                if len(original_refs)!=1 or original_refs[0]['confidence']<.95:raise ValueError('independent crop reference changed')
            x,y,bw,bh=ref['bounds'];tile=image(ref['unit'])[y:y+bh,x:x+bw]
            if thin:
                bx, by, bw, bh = proof['bounds']
                if (
                    not is_thin_glyph(tile, ref['font_height'])
                    or not is_thin_glyph(image(scope['unit'])[by:by+bh, bx:bx+bw], scope['font_height'])
                    or abs(by - (scope['baseline'] - (ref['baseline'] - y))) > .15 * scope['font_height']
                    or abs(bx + bw/2 - character['center']) >= .15 * scope['font_height']
                    or any(v['bounds'] != proof['bounds'] or v['scale'] != 1. for v in proof['thresholds'])
                ):
                    raise ValueError('thin glyph position or ink changed')
            if len(proof['thresholds'])!=2:raise ValueError('both font thresholds required')
            thresholds = aligned_thresholds or (DARK_THRESHOLDS if thin else (160,208))
            for threshold,finding in zip(thresholds, proof['thresholds']):
                bx,by,fw,fh=finding['bounds'];tpl=(cv2.resize(tile,None,fx=finding['scale'],fy=finding['scale'],interpolation=cv2.INTER_NEAREST)<threshold).astype(np.uint8);target=(image(scope['unit'])[by:by+fh,bx:bx+fw]<threshold).astype(np.uint8)
                if tpl.shape!=target.shape or tpl.var()==0:raise ValueError('font geometry changed')
                score=float(cv2.matchTemplate(target,tpl,cv2.TM_CCOEFF_NORMED)[0,0]);inter=np.logical_and(target,tpl).sum();precision=float(inter/max(1,target.sum()));recall=float(inter/max(1,tpl.sum()))
                if score<.95 or precision<.98 or recall<.98:raise ValueError('font pixel evidence failed')
        if not any(max(abs(a-b) for a,b in zip(character['bounds'],v['bounds']))<=1 for proof in character['font_matches'] for v in proof['thresholds']):raise ValueError('character moved away from its verified pixels')
        if thin or character['evidence_mode']=='two_other_physical_staff_fonts':
            if len(other)<2:raise ValueError('two independent reference staves required')
        elif dark or character['evidence_mode']=='independent_local_OCR_and_other_physical_staff_font':
            own=[v for g in ocr_plan['scopes'] if (g['unit'],g['system'],g['staff'])==(scope['unit'],scope['system'],scope['staff']) for v in g['characters'] if v['text']==character['text'] and abs(v['bounds'][0]+v['bounds'][2]/2-character['center'])<.3*scope['font_height']]
            if len(own)!=1 or own[0]['confidence']<.95 or not other:raise ValueError('independent local read and source font required')
        else:raise ValueError('unknown font evidence mode')
    before,_=_score_events(original);ce,_=_score_events(control);result=copy.deepcopy(original);events,_=_score_events(result);changes=[];pending=[];covered=[]
    ocr={(v['unit'],v['system'],v['staff']):v for v in ocr_plan['scopes']}
    grouped_scopes = []
    for scope in font_plan['scopes']:
        sk=(scope['unit'],scope['system'],scope['staff']);height=scope['font_height'];path=source/f"unit-{scope['unit']:03d}.png"
        if sha(path)!=scope['source_sha256']:raise ValueError('original lyric page changed')
        local_ocr=ocr.get(sk,{});desired={};candidates=[];bar_pending=collections.defaultdict(list)
        for row in by_scope.get(sk,[]):
            key=(row['part'],row['measure'],int(row['note_index']));node=nodes[sk[:2]].get(row['head'])
            if node is None or node.get('staff')!=row['staff'] or [int(node.find('bounds').get(v)) for v in ['x','y','w','h']]!=[int(row[v]) for v in ['x','y','w','h']] or row['head'] not in contains[sk[:2]][row['chord']]:raise ValueError('source physical lyric anchor changed')
            if key not in events or key not in ce or _role_anchor_signature(events[key])!=_role_anchor_signature(ce[key]):
                bar_pending[key[:2]].append('caller_source_note_rhythm_changed');continue
            n=events[key]['node']
            if n.find('pitch') is None or n.find('chord') is not None:continue
            if n.find("tie[@type='stop']") is not None and n.find("tie[@type='start']") is None:continue
            candidates.append((int(row['x'])+int(row['w'])/2,key,row))
        for char in scope['characters']:
            verify_font(char,scope)
            cx=char['bounds'][0]+char['bounds'][2]/2;rank=sorted((abs(cx-x),key,row) for x,key,row in candidates)
            if not rank or rank[0][0]>.75*height or len(rank)>1 and rank[1][0]-rank[0][0]<.25*height:
                pending.append(dict(scope=list(sk),text=char['text'],bounds=char['bounds'],reason='source_note_column_not_unique'));continue
            _,key,row=rank[0];suffix=[v for v in local_ocr.get('characters',[]) if v['text']==char['text'] and abs(v['bounds'][0]+v['bounds'][2]/2-cx)<.3*height]
            if len(suffix)>1:raise ValueError('ambiguous original character punctuation')
            punctuation = suffix[0].get('punctuation', '') if suffix else ''
            if not punctuation:
                # Preserve existing punctuation only when the same single character
                # is already attached to this exact note.
                existing = events[key]['node'].findall('lyric')
                if len(existing) == 1:
                    old_text = existing[0].findtext('text', '')
                    tail = old_text[1:]
                    if old_text[:1] == char['text'] and tail and all(mark in '，。；、！？：' for mark in tail):
                        punctuation = tail
            text = char['text'] + punctuation
            if key in desired:bar_pending[key[:2]].append('multiple_source_characters_on_one_note');continue
            desired[key]=dict(text=text,bounds=char['bounds'],evidence_mode=char['evidence_mode'])
        # An independently re-read character must not disappear in the font check.
        for char in local_ocr.get('characters',[]):
            cx=char['bounds'][0]+char['bounds'][2]/2;rank=sorted((abs(cx-x),key) for x,key,row in candidates)
            if rank and rank[0][0]<=.75*height:
                key=rank[0][1]
                if key not in desired or desired[key]['text'][0]!=char['text']:
                    existing=events[key]['node'].findall('lyric')
                    if not any(n.findtext('text','')==char['text']+char.get('punctuation','') for n in existing):bar_pending[key[:2]].append('independently_read_character_not_font_verified')
        bars={key[:2] for key in desired}
        for pid,mid in bars:
            keys=[key for key in desired if key[:2]==(pid,mid)];scope_keys={key for _,key,row in candidates if key[:2]==(pid,mid)}
            for key in scope_keys:
                old=events[key]['node'].findall('lyric')
                if len([n for n in old if any(han(c) for c in n.findtext('text',''))])>1:bar_pending[pid,mid].append('multiple_existing_lyric_verses')
                if any(any(han(c) for c in n.findtext('text','')) for n in old) and key not in desired:bar_pending[pid,mid].append('existing_source_syllable_not_covered')
                if key in desired and any(n.findtext('text','') and not any(han(c) for c in n.findtext('text','')) for n in old):bar_pending[pid,mid].append('unresolved_nonChinese_lyric_at_target')
            if bar_pending[pid,mid]:
                pending.append(dict(scope=list(sk),part=pid,measure=mid,reason=sorted(set(bar_pending[pid,mid]))));continue
            for key in keys:
                n=events[key]['node'];old=[v for v in n.findall('lyric') if any(han(c) for c in v.findtext('text',''))];text=desired[key]['text']
                if len(old)>1:
                    pending.append(dict(part=pid,measure=mid,note_index=key[2],reason='multiple_existing_lyric_verses'));continue
                if old and old[0].findtext('text','')==text:
                    lyric=copy.deepcopy(old[0]);lyric.set('number','1')
                else:
                    lyric=E.Element('lyric',number='1',placement='below');E.SubElement(lyric,'syllabic').text='single';E.SubElement(lyric,'text').text=text
                if old and canonical(old[0])==canonical(lyric):covered.append(dict(part=pid,measure=mid,note_index=key[2],text=text));continue
                prior=[v.findtext('text','') for v in old]
                for v in old:n.remove(v)
                insertion=next((i for i,v in enumerate(n) if v.tag in ['play','listen']),len(n));n.insert(insertion,lyric);changes.append(dict(part=pid,measure=mid,note_index=key[2],before=prior,after=text,source_scope=list(sk),source_bounds=desired[key]['bounds'],evidence_mode=desired[key]['evidence_mode']))
        if local_ocr.get("grouped_lyric_views"):
            grouped_scopes.append((candidates, local_ocr, sk))
    if after_font is not None:
        result = after_font(result, changes)
        events, _ = _score_events(result)
    for candidates, local_ocr, scope_key in grouped_scopes:
        apply_grouped_lyrics(events, candidates, local_ocr, scope_key, changes)
    if without_lyrics(result)!=without_lyrics(original):raise ValueError('source lyric repair changed non-lyric content')
    final,_=_score_events(result)
    if {k:_music_signature(v) for k,v in before.items()}!={k:_music_signature(v) for k,v in final.items()}:raise ValueError('source lyric repair changed musical events')
    if not changes:return original,dict(changes=[],covered=covered,pending=pending,all_nonlyric_content_exact=True)
    return result,dict(changes=changes,covered=covered,pending=pending,all_nonlyric_content_exact=True,all_pitches_onsets_durations_voices_and_notes_exact=True)
