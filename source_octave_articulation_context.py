
"""Run the original-image articulation check during normal MusicXML export."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def refine_octave_articulations(root, job, settings, env, cancel, runner, emit=print, source_job=None):
    from source_long_tuplet_context import export_head_ledger
    from runtime_resources import check_memory

    job=Path(job)
    report=dict(state='no_candidate',changes=[])
    output=root
    try:
        eligible=any(bar.find('direction/direction-type/octave-shift') is not None
                     and bar.find('note/notations/articulations/staccato') is not None
                     for bar in root.findall('part/measure'))
        if eligible:
            info=json.loads((job/'source-native-notation.json').read_text(encoding='utf8'))
            book=Path(info['accepted_native_book'])
            control=Path(info['accepted_native_xml'])
            ledger=control.with_name('head-ledger.tsv')
            expected=info.get('accepted_native_sha256')
            if not expected and info['source_book']==info['accepted_native_book']:
                expected=info['source_book_sha256']
            if not expected or hashlib.sha256(book.read_bytes()).hexdigest()!=expected:
                raise ValueError('已接受原生谱的校验值不一致')
            source=Path(source_job) if source_job is not None else job
            if not ledger.is_file():
                saved=job/'source-long-tuplets-ledger'
                if (saved/'result.mxl').is_file() and (saved/'head-ledger.tsv').is_file():
                    control,ledger=saved/'result.mxl',saved/'head-ledger.tsv'
                else:
                    control,ledger=export_head_ledger(book,source,expected,job,settings,env,cancel,runner,emit)
            check_memory(job,'source-octave-articulations')
            work=job/'source-octave-articulation'
            work.mkdir(exist_ok=False)
            before=work/'input.musicxml'
            ET.ElementTree(root).write(before,encoding='utf-8',xml_declaration=True)
            app=Path(__file__).resolve().parent
            engine=Path(settings['engine_root'])
            python=engine/'ocr-env/Scripts/python.exe'
            runner([python,app/'source_octave_articulation.py',before,control,book,source,ledger,work],
                   job/'source-octave-articulation.log',env,cancel,timeout=150,emit=emit)
            report=json.loads((work/'changes.json').read_text(encoding='utf8'))
            if report['changes']:
                output=ET.parse(work/'output.musicxml').getroot()
                emit(f"原图演奏记号复核：区分 {len(report['changes'])} 处保持音与八度线虚线")
    except InterruptedError:
        raise
    except Exception as exc:
        report=dict(state='unresolved',changes=[],reason=str(exc))
        emit('原图八度线演奏记号待核对')
    (job/'source-octave-articulation-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return output,report

