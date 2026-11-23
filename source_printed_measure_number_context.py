"""Source-layout omission of printed bar-label text, independent of lyric language."""
from pathlib import Path
import json

def refine_printed_measure_numbers(root,job,emit=print):
    job=Path(job);report=dict(state='not_assessed',changes=[],covered=[],pending=[])
    def finish(tree):
        (job/'source-printed-measure-numbers.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
    try:
        from source_printed_measure_numbers import scan_printed_measure_numbers,apply_printed_measure_number_policy
        from source_tempo_context import _score
        data=json.loads((job/'source-piano-context.json').read_text(encoding='utf8'))['native_recovery'];book=Path(data['native_book']);ledger=Path(data['native_note_ledger']);source=Path(data.get('source_job',str(job)));control=_score(Path(data['native_xml']));plan=scan_printed_measure_numbers(book,source,ledger);(job/'source-printed-measure-number-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8');fixed,details=apply_printed_measure_number_policy(root,control,plan,book,source,ledger);report.update(details,source_book_sha256=plan['source_book_sha256'],source_ledger_sha256=plan['source_ledger_sha256']);report['state']=('partially_omitted' if report['pending'] else 'omitted') if report['changes'] else ('needs_review' if report['pending'] else 'no_printed_measure_number_text');emit('印刷小节号：忽略'+str(len(report['changes']))+'处行首编号文字；小节结构和速度保留');return finish(fixed)
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc));report['pending'].append(dict(reason='printed_number_source_evidence_unavailable',detail=str(exc)));return finish(root)
