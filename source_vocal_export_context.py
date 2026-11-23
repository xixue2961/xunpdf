
"""Run the vocal original-image checks as part of the regular MusicXML export."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET

from source_long_tuplet_context import export_head_ledger, sha
from source_vocal_candidates import requests


def refine_vocal_export(root,job,settings,env,cancel,runner,emit=print,source_job=None,evidence_job=None):
    from runtime_resources import check_memory
    job=Path(job)
    report=dict(state='no_candidate',changes={},pending=[])
    result=root
    try:
        if any(requests(root)):
            info=json.loads((job/'source-native-notation.json').read_text(encoding='utf8'))
            book=Path(info['accepted_native_book'])
            expected=info.get('accepted_native_sha256')
            if not expected and info['source_book']==info['accepted_native_book']:
                expected=info['source_book_sha256']
            if not expected or sha(book)!=expected:
                raise ValueError('原生谱校验值与音符坐标不匹配')
            control=Path(info['accepted_native_xml'])
            ledger=control.with_name('head-ledger.tsv')
            source=Path(source_job) if source_job is not None else job
            if not ledger.is_file():
                prior=job/'source-long-tuplets-ledger'
                if (prior/'result.mxl').is_file() and (prior/'head-ledger.tsv').is_file():
                    control,ledger=prior/'result.mxl',prior/'head-ledger.tsv'
                else:
                    control,ledger=export_head_ledger(book,source,expected,job,settings,env,cancel,runner,emit)
            check_memory(job,'source-vocal-export')
            work=job/'source-vocal-export'
            work.mkdir(exist_ok=False)
            before=work/'input.musicxml'
            ET.ElementTree(root).write(before,encoding='utf-8',xml_declaration=True)
            evidence=Path(evidence_job) if evidence_job is not None else job
            engine=Path(settings['engine_root'])
            executable=engine/'ocr-env/Scripts/python.exe'
            worker=Path(__file__).with_name('source_vocal_export_worker.py')
            runner([executable,worker,before,control,book,source,ledger,evidence/'source-syllable-evidence',work],
                   job/'source-vocal-export.log',env,cancel,timeout=180,emit=emit)
            report=json.loads((work/'changes.json').read_text(encoding='utf8'))
            if any(report['changes'].values()):
                result=ET.parse(work/'output.musicxml').getroot()
                counts={k:len(v) for k,v in report['changes'].items()}
                emit(f"人声原图复核：{counts['flag_durations']} 处时值、{counts['chord_ties']} 处延音、"
                     f"{counts['boxed_symbol_lanes']} 处假音及 {counts['lyric_phrases']} 处逐字歌词")
    except InterruptedError:
        raise
    except Exception as exc:
        report=dict(state='unresolved',changes={},pending=[dict(reason=str(exc))])
        emit('人声原图复核待核对')
    (job/'source-vocal-export-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return result,report

