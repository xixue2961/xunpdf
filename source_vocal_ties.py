"""Apply original-pixel-backed in-measure vocal ties with an exact export guard."""
import csv
import hashlib
import json
from pathlib import Path

from source_native_notation import canonical,same_export,score
from source_lyric_roles import _score_events


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_ties(control, saved, proposed):
    before=score(control/'result.mxl')
    after=score(saved/'result.mxl')
    with (control/'note-ledger.tsv').open(encoding='utf8') as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    address={}
    for row in rows:
        if row['head']=='0' or row['repeat_copy']!='false':
            continue
        key=(row['unit'],row['system'],row['head'])
        address.setdefault(key,[]).append((row['part'],row['measure'],int(row['note_index'])))
    old=_score_events(before)[0];new=_score_events(after)[0]
    expected=set()
    removed_dots=0
    for proposal in proposed:
        for marker,label in [('start','first'),('stop','last')]:
            refs=address[str(proposal['unit']),proposal['system'],proposal[label]]
            if len(refs)!=1 or refs[0] in expected or refs[0][:2]!=(proposal['part'],proposal['measure']):
                raise ValueError('tie source endpoint address is ambiguous')
            key=refs[0]
            expected.add(key)
            first=old[key]['node'];last=new[key]['node']
            ties=last.findall(f"tie[@type='{marker}']")
            notations=last.find('notations')
            tied=[] if notations is None else notations.findall(f"tied[@type='{marker}']")
            if len(ties)!=1 or len(tied)!=1:
                raise ValueError('original curve did not become one matching logical and visual tie')
            last.remove(ties[0]);notations.remove(tied[0])
            if not len(notations):last.remove(notations)
            if first.find('notations/articulations/staccato') is not None and last.find('notations/articulations/staccato') is None:
                note_marks=first.find('notations')
                articulation=note_marks.find('articulations')
                articulation.remove(articulation.find('staccato'))
                if not len(articulation):note_marks.remove(articulation)
                if not len(note_marks):first.remove(note_marks)
                removed_dots+=1
    if canonical(before)!=canonical(after):
        raise ValueError('tie stage changed fields outside independently confirmed arcs or their endpoint aliases')
    return dict(ties=len(proposed),removed_endpoint_staccatos=removed_dots)


def try_vocal_ties(job, work, app, book, xml, java, python, scanner, run):
    work.mkdir()
    control=xml.parent
    ledger=control/'note-ledger.tsv'
    original_hash=sha(book)
    if xml.name!='result.mxl' or not ledger.is_file():
        control=work/'control'
        run([*java,'org.audiveris.omr.score.SourceNativeLedger404',book,original_hash,job,control],'vocal-tie-control')
        ledger=control/'note-ledger.tsv'
        if canonical(score(xml))!=canonical(score(control/'result.mxl')):
            raise ValueError('vocal tie control differs from the accepted source export')
    evidence=work/'source-vocal-arcs.json'
    run([python,scanner,book,control/'result.mxl',ledger,job,app,evidence],'vocal-tie-scan')
    plan=json.loads(evidence.read_text(encoding='utf8'))
    if (plan['source_book_sha256'],plan['source_xml_sha256'],plan['source_ledger_sha256'])!=(
            original_hash,sha(control/'result.mxl'),sha(ledger)):
        raise ValueError('vocal arc evidence provenance changed')
    proposals=plan['proposals']
    if not proposals:
        return None
    edges=work/'vocal-tie-plan.tsv'
    with edges.open('x',encoding='utf8') as f:
        for example in proposals:
            row=[example[k] for k in ('unit','system','staff','first','last')]+example['source_curve_bounds']+[example['source_sha256']]
            f.write('\t'.join(map(str,row))+'\n')
    applied=work/'apply'
    run([*java,'org.audiveris.omr.score.SourceIntraMeasureTies467',book,original_hash,job,edges,sha(edges),applied],
        'vocal-tie-apply')
    fixed=applied/'refined.omr'
    saved=work/'reload'
    run([*java,'org.audiveris.omr.score.SourceNativeLedger404',fixed,sha(fixed),job,saved],'vocal-tie-reload')
    same_export(applied,saved)
    details=validate_ties(control,saved,proposals)
    return fixed,saved/'result.mxl',dict(stage='vocal-intrameasure-ties',projection=details,
                                         source_book_sha256=original_hash,output_book_sha256=sha(fixed))
