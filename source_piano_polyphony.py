"""Run optional polyphonic piano repairs as a saved-book transaction."""
import hashlib
import http.client
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urlsplit

from source_native_notation import same_export,score


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_ottava(source, witness, clip, settings, device, folder, cancel, emit):
    from PIL import Image,ImageOps
    from runtime_resources import check_memory
    import xunpdf

    page=source/f"unit-{witness['unit']:03d}.png"
    if sha(page)!=witness['source_sha256']:
        raise ValueError('source ottava page changed')
    check_memory(folder,'source-ottava-text')
    with Image.open(page) as original:
        label=original.crop(tuple(clip)).convert('L')
    label.save(folder/'ottava-label.png')
    proc=None
    readings=[]
    try:
        proc,url,_=xunpdf.ensure_ocr(settings,folder,device,'multi',cancel,emit)
        addr=urlsplit(url)
        if addr.scheme!='http' or addr.hostname!='127.0.0.1' or not addr.port:
            raise ValueError('unexpected offline OCR address')
        for factor in (2,3):
            if cancel.is_set():
                raise InterruptedError('已取消钢琴原图复核')
            pixels=ImageOps.expand(label.resize((label.width*factor,label.height*factor),Image.Resampling.BICUBIC),
                                   border=30,fill=255)
            payload=BytesIO()
            pixels.save(payload,format='PNG')
            connection=http.client.HTTPConnection(addr.hostname,addr.port,timeout=60)
            try:
                connection.request('POST','/ocr',payload.getvalue(),{'Content-Type':'image/png'})
                response=connection.getresponse()
                body=response.read(2*1024*1024+1)
                if response.status!=200 or len(body)>2*1024*1024:
                    raise ValueError('local ottava OCR failed')
            finally:
                connection.close()
            rows=[f for line in body.decode('utf8').splitlines()
                  if (f:=line.split('\t'))[0]=='L' and len(f)==7]
            readings.append([(f[6],float(f[1])) for f in rows])
    finally:
        if proc is not None:
            xunpdf.stop_owned(proc)
    (folder/'ottava-ocr.json').write_text(json.dumps(readings),encoding='utf8')
    return all(len(v)==1 and v[0][0].strip().casefold()=='8va' and v[0][1]>=.95 for v in readings)


def try_piano_polyphony(job, folder, book, java, python, pkg, settings, device, cancel, run, emit):
    """Return a replacement score only after saving and reloading the complete repair."""
    folder=Path(folder);folder.mkdir()
    current=Path(book)
    stages=[]

    def scan(kind, extra=()):
        report=folder/f'{kind}.json'
        run([python,pkg/'scan_piano_polyphony.py',kind,current,job,report,*extra],f'piano-polyphony-{kind}-scan')
        data=json.loads(report.read_text(encoding='utf8'))
        if data['book_sha256']!=sha(current):
            raise ValueError('piano repair scan belongs to another book')
        return report,data

    def native(kind, cls, rows, *mode):
        nonlocal current
        plan=folder/f'{kind}.tsv'
        with plan.open('x',encoding='utf8') as stream:
            stream.writelines('\t'.join(map(str,row))+'\n' for row in rows)
        dest=folder/kind
        prior=current
        digest=sha(current)
        run([*java,'org.audiveris.omr.score.'+cls,prior,digest,job,plan,sha(plan),dest,*mode],
            f'piano-polyphony-{kind}-apply')
        current=dest/'refined.omr'
        if sha(prior)!=digest:
            raise ValueError('piano source book changed during repair')
        score(dest/'result.mxl')
        stages.append(dict(stage=kind,source_book_sha256=digest,output_book_sha256=sha(current)))

    _,data=scan('tremolo')
    if data['findings']:
        rows=[]
        for finding in data['findings']:
            a,b=[finding['appearance'][str(level)] for level in (160,208)]
            if a['support_cols']!=b['support_cols']:
                raise ValueError('tremolo pixel thresholds disagree')
            rows.append([finding[k] for k in ('unit','system','staff','left_chord','right_chord','source_sha256','measure')]
                      +a['support_cols']+[finding['center_x'],a['slope'],b['slope']]
                      +[v for d in (a,b) for band in d['bands'] for v in band])
        native('whole-tremolo','SourceSlopedTremolo452',rows,'apply')

    path,data=scan('voice')
    if data['candidates']>1:
        raise ValueError('multiple unverified inner piano voices')
    if data.get('notes'):
        witness=data['witness']
        template,=witness['templates']
        rows=[[witness[k] for k in ('unit','system','staff','measure','quarter_rest','source_sha256')]
              +template['bounds']+[template[k] for k in ('unit','system','rest')]
              +[sha(job/f"unit-{template['unit']:03d}.png")]]
        for note in data['notes']:
            rows.append([note[k] for k in ('outer_chord','known_head','stem_x')]+note['below_head_stem_pixels']
                      +note['missing_center']+[note['missing_pitch_position']])
        native('inner-voice','SourceInnerVoice461',rows)
        _,identified=scan('ottava',[path])
        span=identified['span']
        if span is not None:
            ocr=folder/'label';ocr.mkdir()
            if not read_ottava(job,witness,span['label_clip_xyxy'],settings,device,ocr,cancel,emit):
                raise ValueError('possible octave line did not reproduce an 8va label in two original pixel views')
            x,y,_,bottom=span['label_clip_xyxy']
            cy=span['center_y']
            row=[witness['unit'],witness['system'],witness['staff'],witness['measure'],
                 span['first_chord'],span['last_chord'],x,span['normal_dashes'][0][0]-3,
                 span['normal_dashes'][-1][1]+7,cy-4,cy+4,y,bottom,witness['source_sha256']]
            native('ottava','SourceOttava462',[row])
    if not stages:
        return None
    if cancel.is_set():
        raise InterruptedError('已取消钢琴原图复核')
    reload=folder/'reload'
    run([*java,'org.audiveris.omr.score.SourceNativeLedger404',current,sha(current),job,reload],
        'piano-polyphony-reload')
    same_export(current.parent,reload)
    return current,reload/'result.mxl',dict(stage='piano-polyphony',substeps=stages,source_verified_reload=True)
