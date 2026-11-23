
"""Apply source-proven beam/rest corrections before MusicXML context processing."""
import json
from pathlib import Path

from source_native_notation import canonical, score, sha


def remove_invalid_rest(measure):
    """Normalize only the exact phantom ending emitted for a false one-bar label."""
    style = [n for n in measure.findall('attributes') if n.findtext('measure-style/multiple-rest') == '1']
    rest = [n for n in measure.findall('note') if n.get('print-object') == 'no'
            and n.find("rest[@measure='yes']") is not None]
    if len(style) != 1 or len(rest) != 1 or list(measure)[-2:] != style + rest:
        raise ValueError('the native export did not contain the expected false rest ending')
    if [x.tag for x in style[0]] != ['measure-style'] or [x.tag for x in rest[0]] != ['rest','duration']:
        raise ValueError('unexpected content in native rest ending')
    for item in style + rest:
        measure.remove(item)


def validate_exports(control, corrected, proof):
    old = score(control)
    new = score(corrected)
    p_old = old.findall('part')
    p_new = new.findall('part')
    if not p_old or [p.get('id') for p in p_old] != [p.get('id') for p in p_new]:
        raise ValueError('native part structure changed')
    if tuple(canonical(n) for n in old if n.tag != 'part') != tuple(canonical(n) for n in new if n.tag != 'part'):
        raise ValueError('score metadata changed during source beam repair')
    seen = set()
    changed_parts = set()
    for prior, after in zip(p_old,p_new,strict=True):
        divisions = 1
        bars = prior.findall('measure')
        corrected_bars = after.findall('measure')
        for i,(a,b) in enumerate(zip(bars, corrected_bars, strict=True),1):
            divisions = int(a.findtext('attributes/divisions',str(divisions)))
            if canonical(a)==canonical(b):
                continue
            if a.attrib != b.attrib:
                raise ValueError('measure attributes changed')
            seen.add((i,a.get('number')))
            remove_invalid_rest(a)
            if canonical(a)==canonical(b):
                continue
            notes_a,notes_b=(m.findall('note') for m in (a,b))
            if len(notes_a)!=6 or len(notes_b)!=8 or any(n.find('pitch') is None for n in notes_a+notes_b):
                raise ValueError('unexpected sounding-note delta during source beam repair')
            expected=['eighth','eighth','eighth','16th','16th','quarter','eighth','eighth']
            durations=[divisions//2,divisions//2,divisions//2,divisions//4,divisions//4,
                       divisions,divisions//2,divisions//2]
            if [n.findtext('type') for n in notes_b]!=expected or [int(n.findtext('duration')) for n in notes_b]!=durations:
                raise ValueError('repaired source beat pattern did not survive serialization')
            ignore=('duration','type','beam')
            if any(tuple(canonical(x) for x in before if x.tag not in ignore)
                   != tuple(canonical(x) for x in replacement if x.tag not in ignore)
                   for before,replacement in zip(notes_a,notes_b)):
                raise ValueError('existing sound, lyrics or notation changed unexpectedly')
            if tuple(canonical(x) for x in a if x.tag!='note')!=tuple(canonical(x) for x in b if x.tag!='note'):
                raise ValueError('source beam repair changed other measure content')
            changed_parts.add(prior.get('id'))
    if len(seen)!=1 or len(changed_parts)!=len(proof['corroborated_staves']):
        raise ValueError('native repair does not match the source witness scope')
    index,number=seen.pop()
    return dict(physical_bar=index,musicxml_measure=number,repaired_parts=sorted(changed_parts))


def try_false_multiple_rest(job, work, current, xml, java_command, python, scanner, run):
    work.mkdir()
    book = work/'refined.omr'
    plan = work/'source-beam-rest-proof.json'
    initial_hash=sha(current)
    run([python,scanner,current,job,book,plan], 'beam-rest-scan')
    report=json.loads(plan.read_text(encoding='utf8'))
    if sha(current)!=initial_hash or report['source_book_sha256']!=initial_hash:
        raise ValueError('source changed during beam/rest scan')
    if not report['findings']:
        return None
    [witness]=report['findings']
    out=work/'export'
    run([*java_command,'org.audiveris.omr.score.SourceNativeLedger404',
         book,sha(book),job,out], 'beam-rest-export')
    fixed=out/'result.mxl'
    scope=validate_exports(xml,fixed,witness)
    return book,fixed,dict(stage='beam-rest',projection=scope,source_proof=str(plan))

