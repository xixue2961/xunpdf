
"""Run the drum rhythm check during normal MusicXML export."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def refine_source_drums(root, job, settings, env, cancel, runner, emit=print, source_job=None, evidence_job=None):
    from source_long_tuplet_context import clocks, export_head_ledger
    from runtime_resources import check_memory

    job=Path(job)
    report=dict(state='no_candidate',changes=[])
    output=root
    try:
        from source_drum_candidates import percussion_targets

        eligible=next(percussion_targets(root),None) is not None
        if eligible:
            info=json.loads((job/'source-native-notation.json').read_text(encoding='utf8'))
            book=Path(info['accepted_native_book'])
            control=Path(info['accepted_native_xml'])
            ledger=control.with_name('note-ledger.tsv')
            expected=info.get('accepted_native_sha256')
            if not expected and info['source_book']==info['accepted_native_book']:
                expected=info['source_book_sha256']
            if not expected or hashlib.sha256(book.read_bytes()).hexdigest()!=expected:
                raise ValueError('已接受原生谱的校验值不一致')
            source=Path(source_job) if source_job is not None else job
            if not ledger.is_file():
                saved=job/'source-long-tuplets-ledger'
                if (saved/'result.mxl').is_file() and (saved/'note-ledger.tsv').is_file():
                    control,ledger=saved/'result.mxl',saved/'note-ledger.tsv'
                else:
                    control,_=export_head_ledger(book,source,expected,job,settings,env,cancel,runner,emit)
                    ledger=control.with_name('note-ledger.tsv')
            check_memory(job,'source-drum-exports')
            work=job/'source-drum-export'
            work.mkdir(exist_ok=False)
            before=work/'input.musicxml'
            ET.ElementTree(root).write(before,encoding='utf-8',xml_declaration=True)
            app=Path(__file__).resolve().parent
            engine=Path(settings['engine_root'])
            python=engine/'ocr-env/Scripts/python.exe'
            argv=[python,app/'source_drum_recovery.py',before,control,source,book,ledger,
                  engine/'Audiveris-Bandscore/lib/audiveris.jar',
                  engine/'cache/paddlex/official_models/en_PP-OCRv5_mobile_rec',work]
            runner(argv,
                   job/'source-drum-export.log',env,cancel,timeout=150,emit=emit)
            report=json.loads((work/'changes.json').read_text(encoding='utf8'))
            if report['changes']:
                output=ET.parse(work/'output.musicxml').getroot()
                emit(f"原图打击乐复核：恢复 {len(report['changes'])} 小节")
    except InterruptedError:
        raise
    except Exception as exc:
        report=dict(state='unresolved',changes=[],reason=str(exc))
        emit('原图打击乐节奏待核对')
    (job/'source-drum-export-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return output,report

