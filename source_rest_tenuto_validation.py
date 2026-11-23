"""Validate the saved projection of source-backed false half-rest repairs."""
from collections import Counter
import copy
import csv

from source_lyric_roles import _score_events, _music_signature
from source_native_notation import canonical, score


def _physical_notes(directory):
    with (directory/'note-ledger.tsv').open(encoding='utf8', newline='') as stream:
        rows=list(csv.DictReader(stream, delimiter='\t'))
    notes={}
    for row in rows:
        identity=tuple(row[k] for k in ('unit','system','part','measure','head','repeat_copy'))
        if identity in notes:
            raise ValueError('duplicate physical note address')
        notes[identity]=row
    return notes


def _address(row):
    return row['part'],row['measure'],int(row['note_index'])


def _measure_map(root):
    return {(p.get('id'),m.get('number')):m for p in root.findall('part') for m in p.findall('measure')}


def _remove_added_tenuto(note):
    locations=[(group,container,t) for group in note.findall('notations')
               for container in group.findall('articulations') for t in container.findall('tenuto')]
    if len(locations)!=1:
        raise ValueError('recovered tenuto does not have a single export owner')
    group,container,tenuto=locations[0]
    container.remove(tenuto)
    if not len(container):
        group.remove(container)
    if not len(group):
        note.remove(group)


def verify_saved_rest_tenutos(control, live, saved, proposals):
    """Check every original event and authorize the persisted XML projection."""
    original,transient,persisted=(score(d/'result.mxl') for d in (control,live,saved))
    original_notes=_physical_notes(control)
    saved_notes=_physical_notes(saved)
    retired={(str(v['unit']),v['system'],v['rest']) for v in proposals}
    expected={k for k in original_notes if (k[0],k[1],k[4]) in retired}
    if original_notes.keys()-saved_notes.keys()!=expected or saved_notes.keys()-original_notes.keys():
        raise ValueError('saved half-rest repair changed unexpected note identities')
    if any(original_notes[k]['shape']!='HALF_REST' or original_notes[k]['staff_number']!='2' for k in expected):
        raise ValueError('half-rest proof refers to another exported event')
    if any((live/name).read_bytes()!=(saved/name).read_bytes() for name in ('head-ledger.tsv','note-ledger.tsv')):
        raise ValueError('persisted half-rest repair changed source-note mapping')
    old_events,old_dirs=_score_events(original)
    new_events,new_dirs=_score_events(persisted)
    def generated_rests(events, notes):
        mapped={_address(row) for row in notes.values()}
        if not mapped<=events.keys():
            raise ValueError('note ledger refers outside the XML projection')
        missing=events.keys()-mapped
        if any(events[key]['node'].find('rest') is None for key in missing):
            raise ValueError('unmapped pitched event in the XML projection')
        return Counter((key[:2],_music_signature(events[key]),canonical(events[key]['node']))
                       for key in missing)
    if generated_rests(old_events,original_notes)!=generated_rests(new_events,saved_notes):
        raise ValueError('saved half-rest recovery changed exported generated rests')
    owners={(str(v['unit']),v['system'],v['owner_chord']) for v in proposals}
    leads={}
    for row in saved_notes.values():
        actual=(row['unit'],row['system'],row['chord'])
        if actual not in owners:
            continue
        key=(actual,row['part'],row['measure'])
        if key not in leads or int(row['note_index'])<int(leads[key]['note_index']):
            leads[key]=row
    if {k[0] for k in leads}!=owners:
        raise ValueError('expected tenuto chord did not export')
    added={_address(r) for r in leads.values()}
    scope={k[:2] for k in added}
    affected_bars={k[1] for k in scope}
    for key,row in saved_notes.items():
        before=original_notes[key]
        for field in row:
            if field!='note_index' and row[field]!=before[field]:
                raise ValueError('surviving original source note changed')
        left=old_events[_address(before)]
        address=_address(row)
        right=new_events[address]
        a=copy.deepcopy(left['node'])
        b=copy.deepcopy(right['node'])
        left_sig=_music_signature(left)
        right_sig=_music_signature(right)
        if address in added:
            if row['staff_number']!='2' or b.find('chord') is not None:
                raise ValueError('tenuto is not on the lower-staff chord leader')
            _remove_added_tenuto(b)
        if address[:2] in scope and row['staff_number']=='2':
            for note in (a,b):
                voice=note.find('voice')
                if voice is not None:
                    note.remove(voice)
            left_sig=left_sig[:3]+left_sig[4:]
            right_sig=right_sig[:3]+right_sig[4:]
        if left_sig!=right_sig or canonical(a)!=canonical(b):
            raise ValueError('saved half-rest recovery changed surviving sound or notation')
    directions=lambda items:[(v['part'],v['measure'],v['staff'],v['start'],canonical(v['node'])) for v in items]
    if directions(old_dirs)!=directions(new_dirs):
        raise ValueError('saved half-rest recovery moved a score direction')
    def outside_measures(root):
        header=copy.deepcopy(root)
        for part in header.findall('part'):
            for measure in part.findall('measure'):
                part.remove(measure)
        return canonical(header)
    if outside_measures(original)!=outside_measures(persisted):
        raise ValueError('saved half-rest recovery changed score metadata')
    baseline=_measure_map(original)
    final=_measure_map(persisted)
    if baseline.keys()!=final.keys():
        raise ValueError('saved half-rest recovery changed measures')
    for key in baseline:
        a,b=(copy.deepcopy(m[key]) for m in (baseline,final))
        if key in scope:
            for measure in (a,b):
                for node in list(measure):
                    if node.tag in ('note','forward','backup'):
                        measure.remove(node)
        if canonical(a)!=canonical(b):
            raise ValueError('saved half-rest recovery changed unrelated measure content')
    transient_measures=_measure_map(transient)
    if transient_measures.keys()!=final.keys():
        raise ValueError('live half-rest recovery changed measures')
    restored_cursors=[]
    for key,after in final.items():
        live_measure=transient_measures[key]
        if canonical(live_measure)==canonical(after):
            continue
        before=baseline[key]
        if (key in scope or key[1] not in affected_bars or not len(before) or not len(after)
                or after[-1].tag!='backup' or canonical(before[-1])!=canonical(after[-1])
                or canonical(before)!=canonical(after)):
            raise ValueError('unproved live-to-saved discrepancy in half-rest repair')
        cursor=copy.deepcopy(after[-1])
        live_measure.append(cursor)
        if canonical(live_measure)!=canonical(after):
            raise ValueError('live-to-saved discrepancy exceeds the original trailing cursor')
        restored_cursors.append(dict(part=key[0],measure=key[1],duration=cursor.findtext('duration')))
    if canonical(transient)!=canonical(persisted):
        raise ValueError('live-to-saved projection changed unrelated score metadata')
    return dict(retired_exported_half_rests=len(expected),restored_tenutos=len(added),
                saved_projection_restored_prior_cursors=restored_cursors)
