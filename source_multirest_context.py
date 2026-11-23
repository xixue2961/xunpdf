
"""Use original stem and beam evidence before dropping suspicious MusicXML rest labels."""
from pathlib import Path
import json
import xml.etree.ElementTree as E


def refine_multirest_export(root, job, settings, device, env, cancel, runner, emit=print, source_job=None):
    job=Path(job)
    report=dict(state='no_false_multiple_rest_signal',changes=[],image_proofs=[])
    def finish(tree):
        (job/'source-multirest-export.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        return tree,report
    if not any(n.text=='1' and not n.attrib for n in root.findall('part/measure/attributes/measure-style/multiple-rest')):
        return finish(root)
    try:
        if cancel.is_set():
            raise InterruptedError('已取消休止记号复核')
        meta=json.loads((job/'source-piano-context.json').read_text(encoding='utf8'))['native_recovery']
        book,ledger=Path(meta['native_book']),Path(meta['native_note_ledger'])
        source=Path(meta.get('source_job') or source_job or job)
        app=Path(__file__).resolve().parent
        engine=Path(settings['engine_root'])
        python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
        work=job/'source-multirest-evidence'
        work.mkdir(exist_ok=False)
        xml=work/'input.musicxml'
        E.ElementTree(root).write(xml,encoding='UTF-8',xml_declaration=True)
        out=work/'output'
        script=app/'rhythm-core/source_multirest_export.py'
        runner(list(map(str,[python,script,book,source,ledger,xml,out])),job/'source-multirest-export.log',
               env,cancel,timeout=120,emit=emit)
        if cancel.is_set():
            raise InterruptedError('已取消休止记号复核')
        details=json.loads((out/'proof.json').read_text(encoding='utf8'))
        report.update(details)
        if details['changes']:
            result=E.parse(out/'score.musicxml').getroot()
            report['state']='restored'
            emit('按原图去掉'+str(len(details['changes']))+'处与实音冲突的假休止记号')
            return finish(result)
        report['state']='needs_review'
        return finish(root)
    except InterruptedError:
        raise
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc))
        return finish(root)

