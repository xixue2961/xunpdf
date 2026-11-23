
"""Run the paired-staff rhythm check during normal MusicXML export."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def refine_paired_staff(root, job, settings, env, cancel, runner, emit=print, source_job=None, evidence_job=None):
    from source_long_tuplet_context import clocks, export_head_ledger
    from runtime_resources import check_memory

    job=Path(job)
    report=dict(state='no_candidate',changes=[])
    output=root
    try:
        from source_paired_staff_candidates import due

        eligible=any(due(bar,divisions,capacity) for p in root.findall('part')
                     for bar,(divisions,capacity) in zip(p.findall('measure'),clocks(p),strict=True))
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
            check_memory(job,'source-paired-staffs')
            work=job/'source-paired-staff'
            work.mkdir(exist_ok=False)
            before=work/'input.musicxml'
            ET.ElementTree(root).write(before,encoding='utf-8',xml_declaration=True)
            app=Path(__file__).resolve().parent
            engine=Path(settings['engine_root'])
            python=engine/'ocr-env/Scripts/python.exe'
            argv=[python,app/'source_paired_staff.py',before,control,book,source,ledger,work]
            evidence=(Path(evidence_job) if evidence_job is not None else job)/'source-long-tuplets-pixels.json'
            if evidence.is_file():
                argv.extend(('--tuplet-evidence',evidence))
            runner(argv,
                   job/'source-paired-staff.log',env,cancel,timeout=150,emit=emit)
            report=json.loads((work/'changes.json').read_text(encoding='utf8'))
            if report['changes']:
                output=ET.parse(work/'output.musicxml').getroot()
                emit(f"钢琴双谱表复核：恢复 {len(report['changes'])} 处符杠节奏")
    except InterruptedError:
        raise
    except Exception as exc:
        report=dict(state='unresolved',changes=[],reason=str(exc))
        emit('钢琴双谱表节奏待核对')
    (job/'source-paired-staff-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return output,report

