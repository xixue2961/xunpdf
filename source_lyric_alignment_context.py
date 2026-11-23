"""Optional source-pixel syllable recheck with portable raw OCR evidence reuse."""
from pathlib import Path
import sys,json,hashlib,copy,uuid,base64,http.client,time
from io import BytesIO
from urllib.parse import urlsplit
import xml.etree.ElementTree as E

def read_lyric_image(endpoint, pixels, cancel, *, timeout=60, request_type=http.client.HTTPConnection):
    """Retry one transient loopback failure without discarding the other lyric bands."""
    for attempt in range(2):
        if cancel.is_set():
            raise InterruptedError('已取消歌词复核')
        connection=request_type(endpoint.hostname,endpoint.port,timeout=timeout)
        try:
            connection.request('POST','/ocr',pixels,{'Content-Type':'image/png'})
            response=connection.getresponse()
            body=response.read(2*1024*1024+1)
            if response.status!=200 or len(body)>2*1024*1024:
                raise ValueError('local lyric OCR failed or exceeded response limit')
            return body.decode('utf8')
        except (OSError,http.client.HTTPException):
            if attempt:
                raise
        finally:
            connection.close()


def lyric_progress(log, total, emit):
    """Expose complete worker records while leaving the original audit log intact."""
    position = log.stat().st_size if log.is_file() else 0
    pending = b""
    done = 0
    shown = 0
    if total:
        emit(f"歌词字形复核 0/{total} 行")

    def update(message):
        nonlocal position, pending, done, shown
        if total:
            try:
                with log.open("rb") as stream:
                    stream.seek(position)
                    pending += stream.read()
                    position = stream.tell()
            except OSError:
                emit(message)
                return
            *lines, pending = pending.split(b"\n")
            done += sum(line.startswith(b"SOURCE_HAN_FONT_SCOPE ") for line in lines)
            seen = min(done, total)
            if seen > shown:
                suffix = "，正在对齐音符" if seen == total else ""
                emit(f"歌词字形复核 {seen}/{total} 行{suffix}")
                shown = seen
                return
        emit(message)
    return update



def wait_for_lyric_ocr_capacity(snapshot, cancel, emit, *, wait_seconds=20, clock=time.monotonic):
    # Only a new model requires room for another OCR process.
    end = clock() + wait_seconds
    announced = False
    while True:
        if cancel.is_set():
            raise InterruptedError('已取消等待歌词识别内存')
        memory = snapshot()
        if (memory.get('commit_available', 0) >= 3 * 1024**3
                and memory.get('physical_available', 0) >= 2 * 1024**3):
            return memory, True
        remaining = end - clock()
        if remaining <= 0:
            return memory, False
        if not announced:
            emit('等待本地歌词模型所需内存，最多 20 秒')
            announced = True
        cancel.wait(min(.5, remaining))


def borrowed_lyric_endpoint(job):
    manifest = Path(job) / 'manifest.json'
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text(encoding='utf8'))
    url = data.get('constants', {}).get('org.audiveris.omr.text.paddle.PaddleOCR.serverUrl')
    parsed = urlsplit(url or '')
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
        return None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=.5)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        response.read(65536)
        return url if response.status == 200 else None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def refine_source_syllables(root,control,ledger,book,source,job,settings,device,env,cancel,runner,emit=print,parts_hint='',requested_device=None,*,owned_ocr=None):
    base=Path(__file__).resolve().parent;pkg=base/'lyric-core';job=Path(job);proc=owned_ocr[0] if owned_ocr is not None else None;did_ocr=False
    if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
    report=dict(state='not_assessed',changes=[],covered=[],pending=[],scope='Original-pixel CJK syllables and unique physical note columns; no musical-event edits')
    def finish(tree):
        (job/'source-syllable-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
    try:
        import xunpdf
        from cache_support import cache_root,recognizer_fingerprint,sha_file
        from runtime_resources import snapshot
        from source_lyric_retry import retry_regions
        from PIL import Image
        engine=Path(settings['engine_root']);engine=engine if engine.is_absolute() else base/engine;runtime=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
        evidence=job/'source-syllable-evidence'
        if evidence.exists():evidence=job/('source-syllable-evidence-'+uuid.uuid4().hex[:8])
        evidence.mkdir();inp=evidence/'input.musicxml';out=evidence/'output.musicxml';E.ElementTree(root).write(inp,encoding='utf-8',xml_declaration=True)
        fingerprint=recognizer_fingerprint(engine);cache=cache_root(settings,base)
        common=['--book',book,'--source',source,'--ledger',ledger,'--input',inp,'--evidence',evidence,'--cache',cache,'--fingerprint',fingerprint,'--hint',parts_hint]
        command=[runtime,pkg/'worker.py','--phase','prepare']+common
        runner(list(map(str,command)),job/'source-syllables-prepare.log',env,cancel,timeout=300,emit=emit)
        prepared=json.loads((evidence/'prepared.json').read_text(encoding='utf8'));missing=[s for s in prepared['scopes'] if any(v.get('ocr_text') is None for v in s['entry']['views'])];hits=sum(s['cache_hit'] for s in prepared['scopes']);emit('原图歌词：复用'+str(hits)+'行已保存像素证据，'+str(len(missing))+'行需要重新读字')
        memory=snapshot();report['ocr_memory_check']=memory;url=None;actual=device
        def regions(scope):
            original = next(v for v in scope['entry']['views'] if v['kind']=='original')
            return retry_regions(scope, original['ocr_text'] or '')

        planned = sum(len(regions(scope)) for scope in prepared['scopes'])
        needs_ocr = bool(missing or planned)
        ready = False
        if needs_ocr:
            if proc is not None and proc.poll() is None:
                _, url, actual = owned_ocr
                report['ocr_process_mode'] = 'reused_owned_process'
            else:
                if proc is not None:
                    xunpdf.stop_owned(proc)
                    proc = None
                url = borrowed_lyric_endpoint(job)
                if url is not None:
                    report['ocr_process_mode'] = 'borrowed_live_process'
            if url is None:
                waited, ready = wait_for_lyric_ocr_capacity(snapshot, cancel, emit)
                report['ocr_memory_last_check'] = waited
        if needs_ocr and (url is not None or ready):
            if url is None:
                selected='auto' if requested_device=='auto' and device=='gpu' else device
                proc,url,actual=xunpdf.ensure_ocr(settings,evidence,selected,'multi',cancel,emit)
                report['ocr_process_mode'] = 'new_process'
            parsed=urlsplit(url)
            if parsed.scheme!='http' or parsed.hostname!='127.0.0.1':raise ValueError('lyric OCR must use the owned local loopback service')
            did_ocr=True
            failures=0
            request_timeout=90 if actual=='cpu' else 60
            for index,scope in enumerate(missing):
                if cancel.is_set():raise InterruptedError('已取消歌词复核')
                if failures>=3:
                    scope['deferred_reason']='local_OCR_service_unavailable';continue
                try:
                    for view in scope['entry']['views']:
                        if view.get('ocr_text') is not None:continue
                        raw=base64.b64decode(view['image_base64'],validate=True)
                        view['ocr_text']=read_lyric_image(parsed,raw,cancel,timeout=request_timeout)
                    failures=0
                    scope['entry']['actual_ocr_device']=actual
                    if index%4==0:emit('歌词带重读 '+str(index+1)+'/'+str(len(missing)))
                except InterruptedError:
                    raise
                except (OSError,ValueError,http.client.HTTPException) as exc:
                    scope['deferred_reason']='local_OCR_incomplete: '+str(exc)
                    failures+=1
            completed = 0
            for scope in prepared['scopes']:
                if failures>=3:
                    break
                if any(v['ocr_text'] is None for v in scope['entry']['views']):
                    continue
                candidates = regions(scope)
                if not candidates:
                    continue
                scope['retry_views'] = []
                with Image.open(Path(source)/f"unit-{scope['unit']:03d}.png") as original:
                    for region in candidates:
                        if cancel.is_set():
                            raise InterruptedError('已取消歌词复核')
                        payload = BytesIO()
                        original.crop(region).convert('L').save(payload, format='PNG')
                        try:
                            result=read_lyric_image(parsed,payload.getvalue(),cancel,timeout=request_timeout)
                            scope['retry_views'].append(dict(region=region,ocr_text=result))
                            failures=0
                            completed+=1
                        except InterruptedError:
                            raise
                        except (OSError,ValueError,http.client.HTTPException) as exc:
                            scope['retry_deferred_reason']=str(exc)
                            failures+=1
                            break
            emit(f'原图局部歌词重读完成 {completed} 处')
        elif missing:
            for scope in missing:scope['deferred_reason']='normal_3GiB_OCR_headroom_unavailable; cached source evidence retained'
            emit('暂不加载新文字模型：内存余量不足；继续验证可复用的原图字形，其余行保留待核')
        if proc is not None:xunpdf.stop_owned(proc);proc=None
        (evidence/'prepared.json').write_text(json.dumps(prepared,ensure_ascii=False,indent=2),encoding='utf8')
        anchor_report=dict(state='not_assessed',changes=[])
        try:
            from source_lyric_anchor_control import prepare_anchor_control
            control,anchor_report=prepare_anchor_control(control,ledger,book,source,job,evidence,runtime,runner,env,cancel,emit)
        except InterruptedError:raise
        except Exception as exc:anchor_report=dict(state='unresolved',changes=[],reason=str(exc))
        command=[runtime,pkg/'worker.py','--phase','apply']+common+['--control',control,'--output',out]
        font_log=job/'source-syllables-apply.log'
        eligible={(s['unit'],s['system'],s['staff']) for s in prepared['scopes']
                  if all(v.get('ocr_text') is not None for v in s['entry']['views'])}
        show_progress=lyric_progress(font_log,len(eligible),emit)
        runner(list(map(str,command)),font_log,env,cancel,timeout=1800,emit=show_progress)
        details=json.loads((evidence/'application.json').read_text(encoding='utf8'))
        if sha_file(inp)!=details['input_sha256'] or sha_file(out)!=details['output_sha256']:raise ValueError('source syllable input/output changed')
        report.update(details,source_rhythm_anchor=anchor_report,evidence_directory=str(evidence),cache_root=str(cache),recognizer_fingerprint=fingerprint,actual_new_OCR_device=actual if did_ocr else None)
        if anchor_report['state']=='unresolved':report['pending'].append(dict(reason='source_rhythm_anchor_unresolved',detail=anchor_report['reason']))
        report['state']=('partially_restored' if report['pending'] else 'restored') if report['changes'] else ('needs_review' if report['pending'] else 'already_aligned')
        emit('逐字原图复核：修正'+str(len(report['changes']))+'个歌词位置，'+str(len(report['pending']))+'项待核')
        return finish(E.parse(out).getroot() if report['changes'] else root)
    except InterruptedError:raise
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc));report['pending'].append(dict(reason='source_syllable_recheck_unresolved',detail=str(exc)));emit('逐字歌词复核未完成，保留此前结果并记录原因');return finish(root)
    finally:
        if proc is not None:
            import xunpdf
            xunpdf.stop_owned(proc)
