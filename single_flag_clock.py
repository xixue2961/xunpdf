
"""Confirm a missing single flag in a complete one-voice measure."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import csv
import hashlib
import json

from source_long_tuplet_context import clocks
from source_vocal_candidates import complete_lane, chord_at, has_same_pitch_continuation
from source_lyric_roles import _music_signature, _score_events


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolated_eighth(evidence, staff):
    shape = 'FLAG_1_DOWN' if evidence['stem'] == 'down' else 'FLAG_1'
    close = [match for match in evidence['matches']
             if match['quality'] >= .90 and match['mask_fraction'] >= .55]
    if not close or any(match['reference']['shape'] != shape for match in close):
        return None
    for match in close:
        ref = match['reference']
        if (match['quality'] >= .95 and match['mask_fraction'] >= .65
            and (ref['unit'], ref['system'], ref['staff']) != staff
            and min(v['precision'] for v in match['scores']) >= .95):
            return match
    return None


def repair(root, native, book, source, ledger, scan):
    if scan['source_book_sha256'] != digest(book):
        raise ValueError('Flag evidence does not match the accepted native score')
    by_address = defaultdict(list)
    with ledger.open(encoding='utf8') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            if row['repeat_copy'] == 'false':
                by_address[(int(row['unit']),row['system'],row['staff'],row['chord'])].append(row)
    findings = defaultdict(list)
    for hit in scan['findings']:
        rows = by_address[(hit['unit'],hit['system'],hit['staff'],hit['chord'])]
        addresses = {(row['part'],row['measure']) for row in rows}
        if len(addresses) == 1 and {row['head'] for row in rows} == {head['id'] for head in hit['heads']}:
            findings[next(iter(addresses))].append((hit,rows))
    source_events, _ = _score_events(native)
    before_events, _ = _score_events(root)
    candidate = deepcopy(root)
    changes = []
    source_hashes = {int(unit): hash_value for unit, hash_value in scan['source_images'].items()}
    seen_hash = {}
    for part in candidate.findall('part'):
        measures = part.findall('measure')
        ids = Counter(bar.get('number') for bar in measures)
        for index,(bar,(divisions, capacity)) in enumerate(zip(measures,clocks(part),strict=True),1):
            scope = (part.get('id'),bar.get('number'))
            if scope not in findings or ids[scope[1]] != 1 or capacity is None or divisions % 2:
                continue
            notes, length = complete_lane(bar,divisions)
            if length != capacity + Fraction(1,2):
                continue
            options = []
            for hit,rows in findings[scope]:
                indices = sorted(int(row['note_index']) for row in rows)
                chord = chord_at(notes,indices,divisions)
                if not chord or not has_same_pitch_continuation(notes,chord,indices):
                    continue
                keys = [(*scope,i) for i in indices]
                if any(k not in source_events or k not in before_events
                       or _music_signature(source_events[k]) != _music_signature(before_events[k])
                       or source_events[k]['node'].get('default-x') != before_events[k]['node'].get('default-x')
                       for k in keys):
                    continue
                stem = chord[0].findtext('stem')
                if stem not in ('up','down'):
                    continue
                box = isolated_eighth(dict(hit,stem=stem),(hit['unit'],hit['system'],hit['staff']))
                if box is None:
                    continue
                images = {hit['unit'],box['reference']['unit']}
                for unit in images - seen_hash.keys():
                    seen_hash[unit] = digest(source/f'unit-{unit:03d}.png')
                if (any(seen_hash[unit] != source_hashes.get(unit) for unit in images)
                    or any(row['source_sha256'] != seen_hash[hit['unit']] for row in rows)):
                    continue
                options.append((indices,chord,box))
            if len(options) != 1:
                continue
            indices,chord,box = options[0]
            for note in chord:
                note.find('duration').text = str(divisions//2)
                note.find('type').text = 'eighth'
            assert complete_lane(bar,divisions)[1] == capacity
            changes.append(dict(part=scope[0], measure=scope[1],physical_measure=index,
                                indices=indices, divisions=divisions,
                                quarters_before=str(length),quarters_after=str(capacity), witness=box))
    return (candidate if changes else root), changes

