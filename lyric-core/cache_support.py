"""Portable, content-addressed raw OCR evidence for original lyric pixels."""
from pathlib import Path
import hashlib,json,base64,io

def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def recognizer_fingerprint(engine):
    engine=Path(engine);paths=[engine/'source/dev/paddleocr/paddle_ocr_server.py']
    for model in ['PP-OCRv5_mobile_det','PP-OCRv5_server_rec']:
        folder=engine/'cache/paddlex/official_models'/model
        if not folder.is_dir():raise ValueError('offline lyric OCR model missing: '+model)
        paths.extend(sorted(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts and not any(part.startswith('.') for part in p.relative_to(folder).parts) and p.suffix not in {'.lock','.log','.tmp','.pyc'}))
    h=hashlib.sha256()
    for path in paths:
        h.update(str(path.relative_to(engine)).replace('\\','/').encode('utf8'));h.update(sha_file(path).encode('ascii'))
    return h.hexdigest()
def scope_signature(group,fingerprint):
    return dict(protocol='source-lyric-raw-422.1',recognizer_fingerprint=fingerprint,source_sha256=group['source_sha256'],left=group['left'],right=group['right'],baseline=group['baseline'],font_height=group['font_height'])
def scope_key(group,fingerprint):return hashlib.sha256(json.dumps(scope_signature(group,fingerprint),sort_keys=True,separators=(',',':')).encode('utf8')).hexdigest()
def cache_root(settings,base):
    if settings.get('lyric_source_cache_root'):path=Path(settings['lyric_source_cache_root'])
    else:path=Path(settings.get('piano_core_cache_root',str(Path(base)/'.codex-task-cache/piano-core'))).parent/'source-lyric-evidence422'
    return path if path.is_absolute() else Path(base)/path

def validate_entry(entry,group,fingerprint,source):
    import numpy as np
    from PIL import Image
    if entry['signature']!=scope_signature(group,fingerprint):raise ValueError('cached lyric source signature changed')
    source=Path(source);path=source/f"unit-{group['unit']:03d}.png"
    if sha_file(path)!=group['source_sha256']:raise ValueError('cached original lyric page changed')
    x0,y0,x1,y1=entry['crop'];expected=[group['left'],max(0,int(group['baseline']-1.12*group['font_height'])),group['right'],int(group['baseline']+.18*group['font_height'])]
    with Image.open(path) as image:
        expected[2]=min(expected[2],image.width);expected[3]=min(expected[3],image.height)
        if entry['crop']!=expected:raise ValueError('cached source crop geometry changed')
        original=np.asarray(image.convert('L'))[y0:y1,x0:x1]
    kinds=set()
    for view in entry['views']:
        payload=base64.b64decode(view['image_base64'],validate=True)
        if len(payload)>4*1024*1024 or len(view['ocr_text'].encode('utf8'))>2*1024*1024:raise ValueError('cached lyric payload exceeds limit')
        tile=np.asarray(Image.open(io.BytesIO(payload)).convert('L'));kinds.add(view['kind'])
        if view['kind']=='original':wanted=original
        elif view['kind']=='packed':
            columns=view['column_map']
            if not columns or any(not isinstance(v,int) or not 0<=v<original.shape[1] for v in columns) or any(a>=b for a,b in zip(columns,columns[1:])):raise ValueError('cached packed source columns changed')
            wanted=original[:,columns]
        else:raise ValueError('unknown source lyric reading view')
        if not np.array_equal(tile,wanted):raise ValueError('cached OCR image does not preserve original pixels')
    if kinds!={'original','packed'}:raise ValueError('two independent source reading layouts required')
    return entry
