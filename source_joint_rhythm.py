"""Apply one independently verified flag, tuplet and eighth-rest stack as a transaction."""
import copy
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from source_lyric_roles import _score_events
from source_native_notation import canonical, same_export, score
from source_rest_tenuto_validation import _address, _physical_notes


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path):
    return json.loads(path.read_text(encoding='utf8'))


def _write_plan(path, records):
    path.write_text('\n'.join('\t'.join(map(str,r)) for r in records),encoding='utf8')


def _scope(root, address):
    part,measure=address
    return root.find(f"part[@id='{part}']/measure[@number='{measure}']")


def _neutral_note(node):
    note=copy.deepcopy(node)
    for child in list(note):
        if child.tag in ('duration','type','voice','time-modification','beam'):
            note.remove(child)
    for notations in note.findall('notations'):
        for tuplet in notations.findall('tuplet'):
            notations.remove(tuplet)
        if not len(notations):
            note.remove(notations)
    return canonical(note)


def validate_joint(control, saved, selection, pair):
    before=score(control/'result.mxl')
    after=score(saved/'result.mxl')
    old=_physical_notes(control)
    new=_physical_notes(saved)
    unit,system=str(pair['unit']),pair['system']
    removed=old.keys()-new.keys()
    added=new.keys()-old.keys()
    if len(removed)!=1 or len(added)!=2:
        raise ValueError('joint repair removed or introduced unexpected physical events')
    missing=old[next(iter(removed))]
    if (missing['unit'],missing['system'],missing['head'],missing['shape'])!=(unit,system,pair['head'],'WHOLE_NOTE'):
        raise ValueError('retired head differs from source overlap proof')
    target=missing['part'],missing['measure']
    added_rows=[new[k] for k in added]
    if any((r['part'],r['measure'],r['staff_number'],r['shape'])!=(*target,'2','EIGHTH_REST') for r in added_rows):
        raise ValueError('unproved rest position or type')
    standard=lambda row:tuple(int(row[k]) for k in ('x','y','w','h'))
    if {standard(r) for r in added_rows}!={tuple(pair['first']),tuple(pair['second'])}:
        raise ValueError('rest bounds differ from original pixels')
    old_events,old_dirs=_score_events(before)
    new_events,new_dirs=_score_events(after)
    allowed={r[3] for r in selection['flags']}|set(selection['triplet'][3].split(','))
    for key in old.keys()&new.keys():
        row,updated=old[key],new[key]
        if any(row[k]!=updated[k] for k in row if k!='note_index'):
            raise ValueError('surviving note physical identity changed')
        first,last=old_events[_address(row)],new_events[_address(updated)]
        if _neutral_note(first['node'])!=_neutral_note(last['node']):
            raise ValueError('surviving note pitch or notation changed')
        if first['duration']!=last['duration'] and row['chord'] not in allowed:
            raise ValueError('unproved existing note duration change')
    direction=lambda values:[(v['part'],v['measure'],v['staff'],v['start'],canonical(v['node'])) for v in values]
    if direction(old_dirs)!=direction(new_dirs):
        raise ValueError('joint repair changed a sounding direction')
    expected=Fraction(selection['expected_whole_notes'])*4
    for staff in ('1','2'):
        events=[v for k,v in new_events.items() if k[:2]==target
                  and v['node'].findtext('staff','1')==staff and v['node'].find('chord') is None
                  and v['duration']>0]
        end=Fraction(0)
        for event in sorted(events,key=lambda e:e['start']):
            if event['start']!=end:
                raise ValueError('repaired staff contains a gap or overlapping events')
            end+=event['duration']
        if end!=expected:
            raise ValueError('joint repair does not fill the printed meter')
    for root in (before,after):
        measure=_scope(root,target)
        part=root.find(f"part[@id='{target[0]}']")
        part.remove(measure)
    if canonical(before)!=canonical(after):
        raise ValueError('joint repair changed XML outside the proven measure')
    return dict(part=target[0],measure=target[1],left_and_right_beats=str(expected),
                original_head_retired=missing['head'],eighth_rests_added=2)


def try_joint_rhythm(job, root, book, xml, java_command, python, pkg, run):
    """Return a replacement pair only after all three repairs and a reload pass."""
    earlier=job/'source-native-rhythm'
    provenance=job/'source-native-rhythm.json'
    flags=earlier/'flags/missing-flags.json'
    triplets=earlier/'triplets/triplet-pixels.json'
    if not all(p.is_file() for p in (provenance,flags,triplets)):
        return None
    receipt=_load(provenance)
    book_hashes={receipt['source_book_sha256'],receipt['native_book_sha256']}
    for step in receipt.get('stages',[]):
        book_hashes.update((step['input_native_sha256'],step['output_native_sha256']))
    if any(_load(p)['source_book_sha256'] not in book_hashes for p in (flags,triplets)):
        raise ValueError('joint rhythm witnesses do not belong to this run')
    folder=root/'joint-rhythm';folder.mkdir()
    choices=folder/'choices.json'
    run([python,pkg/'select_joint_rhythm.py',book,job,flags,triplets,choices],'joint-rhythm-select')
    proposed=_load(choices)
    if not proposed:
        return None
    if len(proposed)!=1:
        raise ValueError('ambiguous simultaneous composite rhythm repairs')
    candidate,=proposed

    def command(cls):
        return [*java_command,'org.audiveris.omr.score.'+cls]

    def execute(cls,source,plan,dest,label):
        run([*command(cls),source,_sha(source),job,plan,_sha(plan),dest,'apply'],label)
        return dest/'refined.omr'

    initial=book
    control=xml.parent
    if not (control/'note-ledger.tsv').is_file() or not (control/'result.mxl').is_file():
        control=folder/'control'
        run([*command('SourceNativeLedger404'),book,_sha(book),job,control],'joint-rhythm-control')
    flag_plan=folder/'flags.tsv'
    _write_plan(flag_plan,candidate['flags'])
    book=execute('SourceMissingNotation412',book,flag_plan,folder/'flags','joint-rhythm-flags')
    tuplet_plan=folder/'tuplet.tsv'
    _write_plan(tuplet_plan,[candidate['triplet']])
    book=execute('SourceTripletPixels413',book,tuplet_plan,folder/'tuplet','joint-rhythm-tuplet')
    pair_path=folder/'pairs.json'
    run([python,pkg/'scan_paired_rests.py',book,job,pair_path],'joint-rhythm-rest-scan')
    pair,=_load(pair_path)
    if any(pair[k]!=candidate[k] for k in ('unit','system','measure')):
        raise ValueError('rest evidence belongs to another rhythm stack')
    columns=('unit','system','staff','measure','head','chord','neighbor','stem','flag')
    pair_plan=folder/'rests.tsv'
    _write_plan(pair_plan,[[*(pair[k] for k in columns),*(w[0] for w in pair['witnesses']),
                            *pair['first'],*pair['second'],pair['source_sha256']]])
    repaired=folder/'rest'
    book=execute('SourceRestPair449',book,pair_plan,repaired,'joint-rhythm-rest')
    saved=folder/'reload'
    run([*command('SourceNativeLedger404'),book,_sha(book),job,saved],'joint-rhythm-reload')
    same_export(repaired,saved)
    report=validate_joint(control,saved,candidate,pair)
    return book,saved/'result.mxl',dict(stage='joint-rhythm',source_book_sha256=_sha(initial),
            output_book=str(book),output_book_sha256=_sha(book),plan=str(choices),validation=report)
