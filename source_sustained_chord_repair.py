
"""Commit a sustained source chord only after a scoped native re-export."""
import csv
import json

from source_native_notation import canonical, score, sha


def _validate_exports(previous_xml, new_xml, ledger, proof):
    before, after=score(previous_xml),score(new_xml)
    with ledger.open(encoding='utf8',newline='') as f:
        rows=[v for v in csv.DictReader(f,delimiter='	')
              if v['head']==proof['recovered_head'] and v['unit']==str(proof['unit'])
              and v['system']==proof['system'] and v['repeat_copy']=='false']
    if len(rows)!=1:
        raise ValueError('recovered head does not have a unique native music address')
    row=rows[0]
    if row['note_index']!='1' or row['shape']!='NOTEHEAD_VOID' or row['staff']!=proof['staff']:
        raise ValueError('recovered head did not retain its expected source position')
    if tuple(canonical(n) for n in before if n.tag!='part')!=tuple(canonical(n) for n in after if n.tag!='part'):
        raise ValueError('sustained chord recovery changed score metadata')
    targets=[]
    for a,b in zip(before.findall('part'),after.findall('part'),strict=True):
        if a.attrib!=b.attrib:
            raise ValueError('sustained chord recovery changed part identity')
        for index,(x,y) in enumerate(zip(a.findall('measure'),b.findall('measure'),strict=True),1):
            if canonical(x)!=canonical(y):
                targets.append((a.get('id'),index,x,y))
    if len(targets)!=1:
        raise ValueError('unexpected extra scope in sustained chord re-export')
    part,ordinal,measure,new=targets[0]
    if part!=row['part'] or measure.get('number')!=row['measure'] or measure.attrib!=new.attrib:
        raise ValueError('sustained chord changed the wrong native bar')
    notes=measure.findall('note')
    revised=new.findall('note')
    if len(notes)<2 or len(revised)!=len(notes)+1 or canonical(notes[0])!=canonical(revised[0]):
        raise ValueError('expected one half-rest replacement after the retained note')
    first,rest=notes[:2]
    same,other=revised[1:3]
    if rest.find('rest') is None or same.find('chord') is not None or other.find('chord') is None:
        raise ValueError('half-chord voice structure did not survive serialization')
    if any(n.findtext('type')!='half' or n.findtext('duration')!=first.findtext('duration')
           or n.findtext('voice')!=first.findtext('voice') for n in (rest,same,other)):
        raise ValueError('half-chord timing or voice changed')
    if canonical(first.find('pitch'))!=canonical(same.find('pitch')) or other.find('pitch') is None:
        raise ValueError('tied source pitch was changed')
    # The first newly serialized note is the original pitch. The second lies
    # one diatonic step above it on the independently verified source head.
    steps='CDEFGAB'
    def diatonic(node):
        return 7*int(node.findtext('pitch/octave'))+steps.index(node.findtext('pitch/step'))
    if diatonic(other)-diatonic(same)!=1:
        raise ValueError('independent upper head has the wrong diatonic interval')
    for prefix in ('tie','notations/tied'):
        if [n.get('type') for n in same.findall(prefix)]!=['stop','start']:
            raise ValueError('two source tie endpoints did not survive')
    if any(n.find('lyric') is not None for n in (same,other)):
        raise ValueError('unexpected words on a restored continuation')
    def members_without_new_content(bar, removed):
        return tuple(canonical(n) for n in bar if n not in removed)
    if members_without_new_content(measure,[rest])!=members_without_new_content(new,[same,other]):
        raise ValueError('untargeted notation in the same measure changed')
    return dict(part=part,physical_measure=ordinal,musicxml_measure=row['measure'])


def try_sustained_chord(job, work, current, xml, java_command, python, scanner, run):
    work.mkdir()
    book, proof=work/'refined.omr',work/'source-half-chord-proof.json'
    before=sha(current)
    run([python,scanner,current,job,book,proof],'sustained-chord-scan')
    data=json.loads(proof.read_text(encoding='utf8'))
    if data['source_book_sha256']!=before or sha(current)!=before:
        raise ValueError('source changed during sustained-chord diagnosis')
    if not data['findings']:
        return None
    [finding]=data['findings']
    out=work/'export'
    run([*java_command,'org.audiveris.omr.score.SourceNativeLedger404',book,sha(book),job,out],
        'sustained-chord-export')
    fixed=out/'result.mxl'
    scope=_validate_exports(xml,fixed,out/'note-ledger.tsv',finding)
    return book,fixed,dict(stage='sustained-chord',projection=scope,source_proof=str(proof))

