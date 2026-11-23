"""Prepare and apply source-pixel lyric evidence; invoked by the standalone GUI."""
from pathlib import Path
import sys,os,json,csv,hashlib,base64,io,copy,argparse,zipfile
import numpy as np
import xml.etree.ElementTree as E
from PIL import Image
BASE=Path(__file__).resolve().parent.parent
sys.path[:0] = [str(Path(__file__).resolve().parent), str(BASE)]
from source_cjk_fonts import scan
from source_lyric_readings import resolve_contained_packed_fragments
from source_band_characters import refine_band
from source_lyric_retry import retry_regions
from source_lyric_spacing import pack_lyric_columns
from source_syllable_alignment import apply_source_lyrics
from source_grouped_lyric_placements import source_view_transcripts
from cache_support import scope_key,scope_signature,validate_entry,sha_file
from source_tempo_context import _score
from source_lyrics_recovery import flagged_parts

def filesystem_path(path):
    """Use the Unicode extended namespace for nested Windows lyric evidence."""
    if os.name != 'nt':
        return path
    prefix = '\\\\?\\'
    absolute = os.path.abspath(path)
    if absolute.startswith(prefix):
        return Path(absolute)
    if absolute.startswith('\\\\'):
        return Path(prefix + 'UNC\\' + absolute[2:])
    return Path(prefix + absolute)


def save(path,data):Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
def prepare(a):
    a.evidence = filesystem_path(a.evidence)
    a.cache = filesystem_path(a.cache)
    root=E.parse(a.input).getroot();wanted=flagged_parts(root,a.hint);rows=list(csv.DictReader(a.ledger.open(encoding='utf8'),delimiter='\t'));groups=scan(a.book,a.source,scope=set());scopes=[];pending=list(groups['pending']);a.evidence.mkdir(parents=True,exist_ok=True)
    for group in groups['staff_groups']:
        physical=[r for r in rows if (int(r['unit']),r['system'],r['staff'])==(group['unit'],group['system'],group['staff']) and r['repeat_copy']=='false' and r['part'] in wanted]
        if not physical:continue
        key=scope_key(group,a.fingerprint);cache=a.cache/key/'evidence.json';entry=None
        if cache.is_file():
            try:
                if cache.stat().st_size>12*1024*1024:raise ValueError('source cache entry too large')
                entry=validate_entry(json.loads(cache.read_text(encoding='utf8')),group,a.fingerprint,a.source)
            except Exception as exc:pending.append(dict(unit=group['unit'],system=group['system'],staff=group['staff'],reason='cache_requires_rebuild',detail=str(exc)))
        if entry is None:
            with Image.open(a.source/f"unit-{group['unit']:03d}.png") as image:
                x0=group['left'];x1=min(group['right'],image.width);y0=max(0,int(group['baseline']-1.12*group['font_height']));y1=min(image.height,int(group['baseline']+.18*group['font_height']));crop=image.crop((x0,y0,x1,y1))
            h=group['font_height'];minimum=min(int(r['x']) for r in physical)-x0-round(.75*h);maximum=max(int(r['x'])+int(r['w']) for r in physical)-x0+round(1.5*h);packed,columns=pack_lyric_columns(crop,h,minimum,maximum);views=[]
            for kind,image in [('original',crop),('packed',packed)]:
                buf=io.BytesIO();image.save(buf,format='PNG');views.append(dict(kind=kind,image_base64=base64.b64encode(buf.getvalue()).decode('ascii'),column_map=columns if kind=='packed' else None,ocr_text=None))
            entry=dict(signature=scope_signature(group,a.fingerprint),crop=[x0,y0,x1,y1],views=views,status='algorithmic_original_source_evidence_not_user_acceptance')
        folder=a.evidence/key;folder.mkdir(exist_ok=True)
        for view in entry['views']:
            (folder/(view['kind']+'.png')).write_bytes(base64.b64decode(view['image_base64']))
        scopes.append(dict(group,cache_key=key,cache_hit=cache.is_file() and all(v.get('ocr_text') is not None for v in entry['views']),entry=entry))
    data=dict(source_book_sha256=groups['source_book_sha256'],source_ledger_sha256=sha_file(a.ledger),input_sha256=sha_file(a.input),recognizer_fingerprint=a.fingerprint,scopes=scopes,pending=pending)
    save(a.evidence/'prepared.json',data);print('LYRIC_SOURCE_PREPARED',len(scopes),'cache_hits',sum(v['cache_hit'] for v in scopes),flush=True)

def decode_views(scope,evidence,source):
    allchars=[];pending=[];g=scope;entry=scope['entry'];folder=evidence/scope['cache_key']
    for view in entry['views']:
        if view.get('ocr_text') is None:continue
        meta=dict(g,crop=entry['crop'],ocr_file=scope['cache_key']+'/'+view['kind']+'.tsv');(folder/(view['kind']+'.tsv')).write_text(view['ocr_text'],encoding='utf8')
        if view['kind']=='packed':meta['crop']=[0,entry['crop'][1],len(view['column_map']),entry['crop'][3]]
        letters,unresolved=refine_band(folder/(view['kind']+'.png'),view['ocr_text'],meta);pending.extend(dict(view=view['kind'],**v) for v in unresolved)
        for char in letters:
            if view['kind']=='packed':
                x,y,bw,bh=char['bounds'];columns=view['column_map']
                if not 0<=x<x+bw<=len(columns) or columns[x+bw-1]-columns[x]+1!=bw:pending.append(dict(text=char['text'],reason='character_spans_removed_whitespace'));continue
                char['bounds']=[entry['crop'][0]+columns[x],y,bw,bh];char['reading_view']='spacing_normalized_original_columns'
            center=char['bounds'][0]+char['bounds'][2]/2;similar=[v for v in allchars if v['text']==char['text'] and abs(v['bounds'][0]+v['bounds'][2]/2-center)<.25*g['font_height']]
            if similar:
                if char['confidence']>similar[0]['confidence']:allchars.remove(similar[0]);allchars.append(char)
            else:allchars.append(char)
    retries = scope.get('retry_views', [])
    if retries:
        original = next(v for v in entry['views'] if v['kind']=='original')
        permitted = set(retry_regions(scope, original['ocr_text']))
        seen = set()
        with Image.open(source/f"unit-{scope['unit']:03d}.png") as image:
            for index, retry in enumerate(retries):
                region = tuple(retry['region'])
                if region not in permitted or region in seen or len(retry['ocr_text'].encode('utf8'))>2*1024*1024:
                    raise ValueError('short lyric source crop changed')
                seen.add(region)
                name = f'retry-{index+1}'
                image.crop(region).convert('L').save(folder/(name+'.png'))
                (folder/(name+'.tsv')).write_text(retry['ocr_text'],encoding='utf8')
                meta = dict(g, crop=region, ocr_file=scope['cache_key']+'/'+name+'.tsv')
                letters, unresolved = refine_band(folder/(name+'.png'),retry['ocr_text'],meta)
                pending.extend(dict(view=name,**v) for v in unresolved)
                for letter in letters:
                    center = letter['bounds'][0] + letter['bounds'][2]/2
                    if any(abs(v['bounds'][0]+v['bounds'][2]/2-center)<.25*g['font_height'] for v in allchars):
                        continue
                    letter['reading_view'] = 'short_original_source_crop'
                    allchars.append(letter)
    allchars,view_arbitrations=resolve_contained_packed_fragments(allchars)
    allchars.sort(key=lambda v:v['bounds'][0]);return dict(view_arbitrations=view_arbitrations,grouped_lyric_views=source_view_transcripts(entry['views']),unit=g['unit'],system=g['system'],staff=g['staff'],baseline=g['baseline'],font_height=g['font_height'],source_sha256=g['source_sha256'],characters=allchars,reading_pending=pending)

def apply_verified_dynamic_phrases(a, original, fixed, control, ocr, report):
    job = a.evidence.parent
    summary = job/'source-lyric-role-context.json'
    if not summary.is_file():
        return fixed
    role = json.loads(summary.read_text(encoding='utf8'))
    if not any(change.get('source_evidence')=='source_proven_repeated_p_dynamic' for change in role['changes']):
        return fixed
    evidence = job/'source-lyric-role-evidence'
    plan = evidence/'source-role-plan.json'
    source_score = evidence/'output.musicxml'
    from source_expression_overlay import canonical
    if (sha_file(plan)!=role.get('plan_sha256') or sha_file(source_score)!=role.get('output_sha256')
        or canonical(E.parse(source_score).getroot())!=canonical(original)):
        raise ValueError('dynamic phrase evidence changed')
    from source_dynamic_lyric_phrase import repair_joined_words
    from source_syllable_alignment import without_lyrics
    result, changes = repair_joined_words(fixed,control,a.book,a.source,a.ledger,ocr,
                                          json.loads(plan.read_text(encoding='utf8')))
    if changes and without_lyrics(result)!=without_lyrics(fixed):
        raise ValueError('phrase repair changed non-lyric content')
    report['changes'].extend(changes)
    return result


def apply(a):
    a.evidence = filesystem_path(a.evidence)
    a.cache = filesystem_path(a.cache)
    data=json.loads((a.evidence/'prepared.json').read_text(encoding='utf8'))
    if sha_file(a.book)!=data['source_book_sha256'] or sha_file(a.ledger)!=data['source_ledger_sha256'] or sha_file(a.input)!=data['input_sha256']:raise ValueError('source lyric inputs changed')
    decoded=[];pending=list(data['pending']);scope_keys=set()
    for scope in data['scopes']:
        entry=scope['entry']
        if any(v.get('ocr_text') is None for v in entry['views']):pending.append(dict(unit=scope['unit'],system=scope['system'],staff=scope['staff'],reason=scope.get('deferred_reason','local_OCR_not_completed')));continue
        validate_entry(entry,scope,a.fingerprint,a.source);cache=a.cache/scope['cache_key']/'evidence.json'
        if not cache.exists():cache.parent.mkdir(parents=True,exist_ok=True);save(cache,entry)
        decoded.append(decode_views(scope,a.evidence,a.source));scope_keys.add((scope['unit'],scope['system'],scope['staff']))
    ocr=dict(scopes=decoded);save(a.evidence/'source-characters.json',ocr)
    if scope_keys:
        fonts=scan(a.book,a.source,scope=scope_keys,extra_refs=a.evidence/'source-characters.json')
        save(a.evidence/'source-fonts.json',fonts)
        original=E.parse(a.input).getroot()
        control=_score(a.control) if zipfile.is_zipfile(a.control) else E.parse(a.control).getroot()
        def preserve_dynamic_phrases(score, changes):
            return apply_verified_dynamic_phrases(a, original, score, control, ocr, {"changes": changes})
        fixed,report=apply_source_lyrics(original,control,a.book,a.source,a.ledger,fonts,ocr,
                                         after_font=preserve_dynamic_phrases)
    else:fixed=E.parse(a.input).getroot();report=dict(changes=[],covered=[],pending=[],all_nonlyric_content_exact=True)
    report['pending'].extend(pending);report.update(short_retry_views=sum(len(s.get('retry_views', [])) for s in data['scopes']),source_book_sha256=data['source_book_sha256'],source_ledger_sha256=data['source_ledger_sha256'],input_sha256=data['input_sha256'],cache_hits=sum(s['cache_hit'] for s in data['scopes']),processed_staff_groups=len(scope_keys),eligible_staff_groups=len(data['scopes']),scope='Original source pixels, independently re-read and font-verified CJK syllables; source staves pending are not an accuracy percentage')
    with a.output.open('xb') as stream:E.ElementTree(fixed).write(stream,encoding='utf-8',xml_declaration=True)
    E.parse(a.output);report['output_sha256']=sha_file(a.output);save(a.evidence/'application.json',report);print('SOURCE_SYLLABLE_APPLICATION',len(report['changes']),'covered',len(report['covered']),'pending',len(report['pending']),flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--phase',choices=['prepare','apply'],required=True)
    for name in ['book','source','ledger','input','evidence','cache']:ap.add_argument('--'+name,type=Path,required=True)
    ap.add_argument('--fingerprint',required=True);ap.add_argument('--hint',default='');ap.add_argument('--control',type=Path);ap.add_argument('--output',type=Path);a=ap.parse_args();prepare(a) if a.phase=='prepare' else apply(a)
