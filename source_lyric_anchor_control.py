"""Rebuild lyric anchor timing using the existing source-pixel rhythm verifier."""
from pathlib import Path
import hashlib,json,xml.etree.ElementTree as E

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def prepare_anchor_control(control,ledger,book,source,job,evidence,runtime,runner,env,cancel,emit=print):
    from source_tempo_context import _score
    from source_rhythm_overlay import apply_verified_rhythm
    source=Path(source);job=Path(job);evidence=Path(evidence);base=Path(__file__).resolve().parent
    report=dict(state='unchanged',changes=[],reason='no_source_meter_evidence')
    meters=next((p for root in [job,source] for p in [root/'source-tuplet-evidence/meter/source-meters.json',root/'source-rhythm-evidence/meter/source-meters.json'] if p.is_file()),None)
    full_ledger=Path(ledger).with_name('note-ledger.tsv')
    if meters is None or not full_ledger.is_file():return Path(control),report
    folder=evidence/'anchor-rhythm';folder.mkdir();inp=folder/'source-control.musicxml';original=_score(Path(control));E.ElementTree(original).write(inp,encoding='utf-8',xml_declaration=True)
    hashes={str(p):sha(p) for p in [Path(book),full_ledger,meters,inp]}
    args=[runtime,base/'source_rhythm_scan.py','--score',inp,'--book',book,'--job',source,'--ledger',full_ledger,'--meters',meters,'--out',folder]
    runner(list(map(str,args)),job/'source-syllable-anchor-rhythm.log',env,cancel,timeout=300,emit=emit)
    plan=json.loads((folder/'source-rhythm-plan.json').read_text(encoding='utf8'))
    if hashes!={p:sha(p) for p in hashes} or plan['source_book_sha256']!=sha(book) or plan['ledger_sha256']!=sha(full_ledger) or plan['input_xml_sha256']!=sha(inp):raise ValueError('lyric rhythm anchor evidence changed')
    fixed,details=apply_verified_rhythm(original,plan,source)
    report.update(state='source_rhythm_verified' if details['changes'] else 'unchanged',changes=details['changes'],source_book_sha256=sha(book),ledger_sha256=sha(full_ledger),meter_evidence_sha256=sha(meters),pending=plan['pending'])
    (folder/'application.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    if not details['changes']:return Path(control),report
    out=folder/'verified-control.musicxml';E.ElementTree(fixed).write(out,encoding='utf-8',xml_declaration=True);E.parse(out)
    return out,report
