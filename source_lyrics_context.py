"""Read initially missing Chinese lyrics with owned local OCR after part collation."""
from pathlib import Path
import copy,hashlib,http.client,json,os,sys,xml.etree.ElementTree as E
from urllib.parse import urlsplit
from runtime_resources import MemoryBudgetError, wait_for_released_memory

def refine_source_lyrics(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None,requested_device=None):
    job=Path(job);source=Path(source_job) if source_job is not None else job;proc=None
    report=dict(state='not_assessed',changes=[],pending=[],scope='source CJK recovery, dynamics roles and per-note lyric alignment; all musical events preserved')
    def finish(tree):
        (job/'source-lyrics-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
    try:
        from source_lyrics_recovery import scan_missing_lyrics,apply_missing_lyrics,flagged_parts,sha
        if not flagged_parts(root,parts_hint):report['state']='no_flagged_lyric_parts';return finish(root)
        import xunpdf
        app=xunpdf.ROOT;pkg=app/'piano-core'
        if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
        from bounded_process import run_bounded
        book=source/'raw/input.omr';book_hash=sha(book);evidence=job/'source-lyrics-evidence';evidence.mkdir(exist_ok=True);control_dir=None
        piano_path=job/'source-piano-context.json'
        if piano_path.is_file():
            piano=json.loads(piano_path.read_text(encoding='utf8'));meta=piano.get('native_recovery',{});work=piano.get('evidence_directory')
            if meta.get('source_book_sha256')==book_hash and work:
                candidate=Path(work)/'native-core/control'
                if all((candidate/name).is_file() for name in ('head-ledger.tsv','result.mxl')):control_dir=candidate
        if control_dir is None:
            engine=Path(settings['engine_root']);java=engine/settings['java_relative']/'bin/java.exe';prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix
            cp=os.pathsep.join([str(pkg/'runtime'),str(prefix),str(engine/'Audiveris-Bandscore/lib/*')]);control_dir=evidence/'control';wait_for_released_memory(evidence,cancel,emit=emit,phase='source-lyrics-control')
            run_bounded(runner,[java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',cp,'org.audiveris.omr.score.PianoExport357',book,source,control_dir],job/'source-lyrics-control.log',env,cancel,timeout=300,emit=emit)
        control=xunpdf.read_score(control_dir/'result.mxl');ledger=control_dir/'head-ledger.tsv';E.ElementTree(copy.deepcopy(root)).write(evidence/'input.musicxml',encoding='utf-8',xml_declaration=True)
        emit('按实际声部和原图字框复核遗漏的中文歌词')
        def ocr(data):
            nonlocal proc
            if cancel.is_set():raise InterruptedError('歌词识别已取消')
            if proc is None:
                runtime_device='auto' if requested_device=='auto' and device=='gpu' else device
                proc,url,actual=xunpdf.ensure_ocr(settings,evidence,runtime_device,'multi',cancel,emit);report['actual_ocr_device']=actual;report['ocr_url']=url
            parsed=urlsplit(report['ocr_url']);connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=60)
            try:
                connection.request('POST','/ocr',data,{'Content-Type':'image/png'});response=connection.getresponse();body=response.read(2*1024*1024+1)
                if response.status!=200 or len(body)>2*1024*1024:raise ValueError('local lyric OCR failed or exceeded response limit')
                return body.decode('utf8')
            finally:connection.close()
        plan=scan_missing_lyrics(root,control,book,source,ledger,parts_hint,ocr,cancel,evidence);(evidence/'source-lyrics-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
        fixed=apply_missing_lyrics(root,plan,book,source,ledger);report.update(changes=plan['changes'],pending=plan['pending'],evidence_directory=str(evidence),source_book_sha256=book_hash,plan='source-lyrics-evidence/source-lyrics-plan.json')
        report['state']=('partially_restored' if plan['pending'] else 'restored') if plan['changes'] else ('needs_review' if plan['pending'] else 'no_missing_source_cjk_lyrics')
        emit('初始缺词复核：补回'+str(len(plan['changes']))+'字；继续核对已有歌词和力度')
        fixed,role_report=refine_source_lyric_roles(fixed,control_dir/'result.mxl',ledger,book,source,job,settings,device,env,cancel,runner,emit)
        report['source_lyric_role_context']=dict(state=role_report['state'],changes=len(role_report['changes']),pending=len(role_report['pending']),report='source-lyric-role-context.json')
        report['pending'].extend(dict(kind='source_dynamic_lyric_role',**v) for v in role_report['pending'])
        if role_report['state']=='unresolved':report['pending'].append(dict(kind='source_dynamic_lyric_role',reason=role_report.get('reason','unresolved')))
        from source_lyric_alignment_context import refine_source_syllables
        owned_ocr = None
        if proc is not None and proc.poll() is None:
            owned_ocr = (proc, report['ocr_url'], report['actual_ocr_device'])
            proc = None
        fixed,syllable_report=refine_source_syllables(fixed,control_dir/'result.mxl',ledger,book,source,job,settings,device,env,cancel,runner,emit,parts_hint=parts_hint,requested_device=requested_device,owned_ocr=owned_ocr)
        report['source_syllable_context']=dict(state=syllable_report['state'],changes=len(syllable_report['changes']),pending=len(syllable_report['pending']),report='source-syllable-context.json')
        report['syllable_changes']=syllable_report['changes']
        report['pending'].extend(dict(kind='source_syllable_alignment',**v) for v in syllable_report['pending'])
        changed=bool(report['changes'] or role_report['changes'] or syllable_report['changes'])
        if report['pending']:report['state']='partially_restored' if changed else 'needs_review'
        elif changed:report['state']='restored'
        return finish(fixed)
    except MemoryBudgetError as exc:
        report.update(state="interrupted_memory",reason=str(exc));finish(root);raise
    except InterruptedError:raise
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc));emit('中文歌词复核未通过验证，保留此前内容，详见报告');return finish(root)
    finally:
        if proc is not None:
            import xunpdf
            xunpdf.stop_owned(proc)
def refine_source_lyric_roles(root,control,ledger,book,source,job,settings,device,env,cancel,runner,emit=print):
    """Run source-font recognition in the owned OCR Python, not the GUI Python."""
    job=Path(job);report=dict(state='not_assessed',changes=[],covered=[],pending=[])
    def finish(tree):
        (job/'source-lyric-role-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
    try:
        evidence=job/'source-lyric-role-evidence';evidence.mkdir(exist_ok=False)
        inp=evidence/'input.musicxml';out=evidence/'output.musicxml';E.ElementTree(root).write(inp,encoding='utf-8',xml_declaration=True)
        python=Path(settings['engine_root'])/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
        script=Path(__file__).resolve().parent/'source_lyric_roles.py'
        args=[python,script,'--book',book,'--source',source,'--ledger',ledger,'--input',inp,'--control',control,'--output',out,'--evidence',evidence]
        runner(list(map(str,args)),job/'source-lyric-role.log',env,cancel,timeout=300,emit=emit)
        details=json.loads((evidence/'application.json').read_text(encoding='utf8'))
        if hashlib.sha256(inp.read_bytes()).hexdigest()!=details['input_sha256'] or hashlib.sha256(out.read_bytes()).hexdigest()!=details['output_sha256']:raise ValueError('lyric role worker input/output changed')
        report.update(details,state=('partially_restored' if details['pending'] else 'restored') if details['changes'] else ('needs_review' if details['pending'] else 'already_covered' if details['covered'] else 'no_source_role_conflicts'))
        if details['changes']:emit('按原图把'+str(len(details['changes']))+'处假歌词还原为正确声部的力度')
        return finish(E.parse(out).getroot() if details['changes'] else root)
    except MemoryBudgetError as exc:
        report.update(state="interrupted_memory",reason=str(exc));finish(root);raise
    except InterruptedError:raise
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc));emit('力度与歌词归属复核未通过，保留此前结果并记录原因');return finish(root)
