
"""Commit a recovered vocal beam only if every other musical field is preserved."""
from collections import Counter, defaultdict
import csv
import json

from source_native_notation import score, canonical, sha


def _slur_pairs(root):
    pairs=Counter()
    orphan=Counter()
    for part in root.findall('part'):
        open_lines=defaultdict(list)
        for ordinal,measure in enumerate(part.findall('measure'),1):
            for index,note in enumerate(measure.findall('note')):
                for slur in note.findall('notations/slur'):
                    focus=(part.get('id'),ordinal,index)
                    key=slur.get('number','1')
                    role=slur.get('type')
                    if role=='start':
                        open_lines[key].append(focus)
                    elif role=='stop' and open_lines[key]:
                        pairs[(open_lines[key].pop(),focus)]+=1
                    elif role=='stop':
                        orphan[('stop',focus)]+=1
        for value in open_lines.values():
            for v in value:
                orphan[('start',v)]+=1
    return pairs,orphan


def validate_vocal_beam_repair(before,after,ledger,witness):
    old, new=score(before),score(after)
    targets=[*witness['new_heads'],witness['already_printed_slur'][1]]
    with ledger.open(encoding='utf8',newline='') as f:
        rows=[v for v in csv.DictReader(f,delimiter='	')
              if v['head'] in targets and v['unit']==str(witness['unit'])
              and v['system']==witness['system'] and v['repeat_copy']=='false'
              and v['staff']==witness['staff']]
    if len(rows)!=4 or {v['head'] for v in rows}!=set(targets):
        raise ValueError('recovered vocal heads have no unique source addresses')
    by_id={v['head']:v for v in rows}
    addresses=[by_id[h] for h in targets]
    if len({v['part'] for v in addresses})!=1 or len({v['measure'] for v in addresses[:3]})!=1:
        raise ValueError('recovered notes do not belong to one voice')
    first,following=addresses[0],addresses[-1]
    pid,number=first['part'],first['measure']
    old_bars=old.find(f"part[@id='{pid}']").findall('measure')
    indices=[k for k,bar in enumerate(old_bars,1) if bar.get('number')==number]
    if len(indices)!=1:
        raise ValueError('ambiguous target measure number')
    [ordinal]=indices
    if old_bars[ordinal].get('number')!=following['measure']:
        raise ValueError('source slur does not join the next bar')
    prior=old_bars[ordinal-1]
    final=new.find(f"part[@id='{pid}']").findall('measure')[ordinal-1]
    previous_notes=prior.findall('note')
    new_notes=final.findall('note')
    if len(new_notes)!=len(previous_notes)+2:
        raise ValueError('expected one rest replaced by three notes')
    rest=previous_notes[-1]
    restored=new_notes[-3:]
    if [int(v['note_index']) for v in addresses[:3]]!=list(range(len(previous_notes)-1,len(previous_notes)+2)):
        raise ValueError('new notes did not stay in their source order')
    if rest.find('rest') is None or rest.findtext('type')!='quarter':
        raise ValueError('the replaced source event is not the expected quarter rest')
    if [n.findtext('type') for n in restored]!=['16th','eighth','16th']:
        raise ValueError('the restored note types do not match the original beams')
    durations=[int(n.findtext('duration')) for n in restored]
    if durations[0]*2!=durations[1] or durations[0]!=durations[2] or sum(durations)!=int(rest.findtext('duration')):
        raise ValueError('the restored notes do not fill the original beat')
    if any(n.find('pitch') is None or n.findtext('voice')!=rest.findtext('voice') or n.find('lyric') is not None for n in restored):
        raise ValueError('the restoration changed voice or added unsupported lyrics')
    anchor=(pid,ordinal,int(addresses[-2]['note_index']))
    terminal=(pid,ordinal+1,int(following['note_index']))
    old_pairs,old_orphans=_slur_pairs(old)
    new_pairs,new_orphans=_slur_pairs(new)
    if (new_pairs!=old_pairs+Counter({(anchor,terminal):1})
            or old_orphans!=new_orphans+Counter({('stop',terminal):1})):
        raise ValueError('re-export changed an unrelated slur pairing')
    index=list(final).index(restored[0])
    for n in restored:
        final.remove(n)
    final.insert(index,rest)
    for root in (old,new):
        for slur in root.findall('.//notations/slur'):
            slur.attrib.pop('number',None)
    if canonical(old)!=canonical(new):
        raise ValueError('vocal beam repair changed unrelated score fields')
    return dict(part=pid,physical_measure=ordinal,printed_measure=number,restored_pitched_notes=3,
                restored_existing_slur=True)


def try_lyric_band_rest(job,work,current,xml,java_command,python,scanner,run):
    work.mkdir()
    book,plan=work/'refined.omr',work/'source-beam-and-lyric-ink.json'
    sha_before=sha(current)
    run([python,scanner,current,job,book,plan],'lyric-rest-scan')
    proofs=json.loads(plan.read_text(encoding='utf8'))
    if sha_before!=sha(current):
        raise ValueError('original source changed during vocal-beam scan')
    if not proofs:
        return None
    [witness]=proofs
    path=job/f"unit-{witness['unit']:03d}.png"
    if sha(path)!=witness['source_sha256']:
        raise ValueError('the original lyric-band image changed')
    out=work/'export'
    run([*java_command,'org.audiveris.omr.score.SourceNativeLedger404',book,sha(book),job,out],
        'lyric-rest-export')
    fixed=out/'result.mxl'
    delta=validate_vocal_beam_repair(xml,fixed,out/'note-ledger.tsv',witness)
    return book,fixed,dict(stage='lyric-band-rest',projection=delta,source_proof=str(plan))

