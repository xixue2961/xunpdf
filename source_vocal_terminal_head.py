"""Restore a missing final sung quarter using parallel rhythm and source ink."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def lyric_text(note):
    lyrics = note.findall('lyric')
    if len(lyrics) != 1 or len(lyrics[0].findall('text')) != 1:
        return None
    value = lyrics[0].findtext('text', '')
    return value if len(value) == 1 and '\u3400' <= value <= '\u9fff' else None


def layout(notes):
    clock = Fraction()
    for e in notes:
        n = e['node']
        if (e['start'] != clock or e['duration'] <= 0 or n.find('pitch') is None
                or any(n.find(tag) is not None for tag in ('chord', 'grace', 'tie', 'time-modification', 'cue'))):
            return None
        clock += e['duration']
    return clock if len({e['node'].findtext('voice', '1') for e in notes}) == 1 else None


def staff_lines(staff, x):
    ys = []
    for line in staff.findall('lines/line'):
        points = sorted((float(p.get('x')), float(p.get('y'))) for p in line.findall('point'))
        for (left, yl), (right, yr) in zip(points, points[1:]):
            if left <= x <= right and right > left:
                ys.append(yl + (yr - yl) * (x - left) / (right - left))
                break
    if len(ys) != 5:
        return None
    scale = (ys[-1] - ys[0]) / 4
    if 11 <= scale <= 17 and all(abs(b - a - scale) < 1.6 for a, b in zip(ys, ys[1:])):
        return ys[2], scale


def head_mask(image, x, y, width, height, level):
    region = image[y:y + height, x + 2:x + width - 4] < level
    left = image[y:y + height, x - 13:x - 6] < level
    if region.shape != (height, width - 6) or left.shape != (height, 7):
        return None
    return region, left.mean(axis=1) <= .8


def printed_head(image, anchor, staff, donors):
    x, _, width, height = anchor
    if not (17 <= width <= 22 and 14 <= height <= 18):
        return None
    center, space = staff
    ds = [(int(r['x']), int(r['y'])) for r in donors
          if abs(int(r['w']) - width) <= 1 and abs(int(r['h']) - height) <= 1]
    levels = (105, 155)
    templates = [[q for a, b in ds if (q := head_mask(image, a, b, width, height, level)) is not None]
                 for level in levels]
    possibilities = []
    for staff_step in range(-5, 6):
        y0 = round(center + staff_step * space / 2 - height / 2)
        for dx in range(-2, 3):
            for dy in (-1, 0, 1):
                u, v = x + dx, y0 + dy
                values = []
                for level, same_font in zip(levels, templates):
                    candidate = head_mask(image, u, v, width, height, level)
                    if candidate is None:
                        break
                    sample, mask = candidate
                    scores = []
                    for template, other in same_font:
                        keep = mask & other
                        if int(keep.sum()) < 7:
                            continue
                        a, b = sample[keep], template[keep]
                        if min(a.mean(), b.mean()) >= .45:
                            scores.append(float((a & b).sum() / (a | b).sum()))
                    values.append(max(scores, default=0))
                if len(values) == 2:
                    possibilities.append((min(values), staff_step, u, v, values))
    best = max(possibilities, default=None)
    if best is None or best[0] < .89:
        return None
    confidence, step, x, y, values = best
    alternate = max((q[0] for q in possibilities if q[1] != step), default=0)
    if confidence - alternate < .19:
        return None

    mid_y = round(center + step * space / 2)
    # A plain upward quarter has a continuous narrow stem without a flag.
    top = mid_y - round(space * 3.5)
    bottom = mid_y - round(space * .58)
    stem_x = x + width - 1
    if top < 0 or x < 40:
        return None
    stem = image[top:bottom, stem_x - 3:stem_x + 5] < 140
    # The start of a stem varies by font. Require two adjacent columns with
    # most of their lower three staff spaces occupied and quiet neighbors.
    core = stem[-round(space * 2.5):, :]
    occupancy = core.mean(axis=0)
    strong = np.flatnonzero(occupancy >= .9)
    if not 2 <= len(strong) <= 4 or strong[-1] - strong[0] + 1 != len(strong):
        return None
    outer = image[top:bottom, stem_x + 5:stem_x + round(space)] < 155
    if int(outer[(outer.mean(axis=1) < .8)].sum()) > 4:
        return None
    # No printed accidental, ornament, or neighboring symbol in the approach.
    left = image[mid_y - round(1.2 * space):mid_y + round(1.2 * space),
                 x - round(2.4 * space):x - round(.45 * space)] < 155
    if int(left[left.mean(axis=1) < .8].sum()) > 4:
        return None
    return dict(step=step, head=[x, y, width, height], scores=values,
                strongest_alternative=alternate, continuous_stem_columns=strong.tolist())


def candidate(target, peer, target_rows, peer_rows, meter):
    if len(target) < 3 or len(peer) != len(target) + 1:
        return False
    if len(target_rows) != len(target) or len(peer_rows) != len(peer):
        return False
    if any(int(r['note_index']) != i or r['shape'] != 'NOTEHEAD_BLACK'
           for group in (target_rows, peer_rows) for i, r in enumerate(group)):
        return False
    if layout(target) != meter - 1 or layout(peer) != meter:
        return False
    if any((a['start'], a['duration']) != (b['start'], b['duration'])
           or abs(int(ra['x']) - int(rb['x'])) > 3
           for a, b, ra, rb in zip(target, peer, target_rows, peer_rows)):
        return False
    base = [lyric_text(e['node']) for e in target]
    full = [lyric_text(e['node']) for e in peer]
    if not all(full) or base != full[:-1]:
        return False
    tail = peer[-1]['node']
    if (peer[-1]['duration'] != 1 or tail.findtext('type') != 'quarter'
            or any(tail.find(tag) is not None for tag in ('dot', 'beam', 'notations', 'accidental'))):
        return False
    end = target[-1]['measure']
    return not any(end.find(tag) is not None for tag in ('backup', 'forward'))


def recover(root, job):
    job = Path(job)
    meta = json.loads((job / 'source-native-notation.json').read_text('utf8'))
    native, xml = (Path(meta[k]) for k in ('accepted_native_book', 'accepted_native_xml'))
    expected = meta.get('accepted_native_sha256')
    if not expected or sha(native) != expected:
        raise ValueError('vocal terminal head archive hash mismatch')
    meter_path = job / 'source-tuplet-evidence/meter/source-meters.json'
    raster = {str(r['unit']): r['image_sha256']
              for r in json.loads(meter_path.read_text('utf8'))['source_images']}
    by_staff = defaultdict(list)
    for r in rows(xml.with_name('note-ledger.tsv')):
        if r['implicit'] == 'false' and r['repeat_copy'] == 'false':
            address = tuple(r[k] for k in ('unit', 'system', 'measure', 'part', 'staff'))
            by_staff[address].append(r)
    grouped = defaultdict(list)
    for address, ledger in by_staff.items():
        grouped[address[:3]].append((address[3:], sorted(ledger, key=lambda r: int(r['note_index']))))

    result = deepcopy(root)
    events, _ = _score_events(result)
    notes = defaultdict(list)
    for (part, bar, _), e in events.items():
        notes[part, bar].append(e)
    parts = result.findall('part')
    order = {p.get('id'): i for i, p in enumerate(parts)}
    local = {}
    for p in parts:
        if any(s.text != '1' for s in p.findall('measure/attributes/staves')):
            continue
        divisions, clef = None, None
        context = dict(inherited_context(p))
        for measure in p.findall('measure'):
            if new_div := measure.findtext('attributes/divisions'):
                divisions = int(new_div)
            if (new_clef := measure.find('attributes/clef')) is not None:
                clef = tuple(new_clef.findtext(k, '') for k in ('sign', 'line', 'clef-octave-change'))
            number = measure.get('number')
            local[p.get('id'), number] = divisions, clef, context[number]

    changes = []
    with ZipFile(native) as accepted, ZipFile(job / 'raw/input.omr') as raw:
        active_page, image = None, None
        for (unit, system, bar), players in grouped.items():
            for (part, staff), target_rows in players:
                if (part, bar) not in local:
                    continue
                div, clef, (key, time) = local[part, bar]
                # Restrict pitch reconstruction to a known clef without an octave sign.
                if not div or clef != ('G', '2', '') or key is None or time not in (('3', '4'), ('4', '4')):
                    continue
                target = notes[part, bar]
                peer = [(other, other_staff, ledger) for (other, other_staff), ledger in players
                        if other != part and (other, bar) in local and abs(order[other] - order[part]) == 1
                        and local[other, bar][2] == (key, time)
                        and candidate(target, notes[other, bar], target_rows, ledger, int(time[0]))]
                if len(peer) != 1:
                    continue
                other, _, peer_rows = peer[0]
                control = graph(accepted, unit, system)
                original = graph(raw, unit, system)
                recognized = {n.get('id'): n for n in control.findall('sig/inters/*') if n.find('bounds') is not None}
                printed = {n.get('id'): n for n in original.findall('sig/inters/*') if n.find('bounds') is not None}
                if not all(native_anchor(r, recognized) and native_anchor(r, printed)
                           for r in target_rows + peer_rows):
                    continue
                anchor = tuple(int(peer_rows[-1][k]) for k in ('x', 'y', 'w', 'h'))
                x = anchor[0]
                board = original.find(f".//staff[@id='{staff}']")
                line = staff_lines(board, x) if board is not None else None
                if not line:
                    continue
                final_character = lyric_text(notes[other, bar][-1]['node'])
                words = []
                for text in original.findall('sig/inters/lyric-item'):
                    if text.get('staff') != staff or text.get('value') != final_character:
                        continue
                    u, v, w, h = coordinate(text)
                    if abs(u + w / 2 - (x + anchor[2] / 2)) <= line[1] and 3.2 * line[1] < v - line[0] < 8 * line[1]:
                        words.append(text.get('id'))
                if len(words) != 1:
                    continue
                if any(n.tag == 'head' and n.get('staff') == staff and abs(coordinate(n)[0] - x) <= anchor[2]
                       for n in printed.values()):
                    continue
                if active_page != unit:
                    png = job / f'unit-{int(unit):03d}.png'
                    if sha(png) != raster.get(unit):
                        raise ValueError('vocal terminal head raster hash mismatch')
                    with Image.open(png) as inp:
                        image = np.asarray(inp.convert('L'))
                    active_page = unit
                optical = printed_head(image, anchor, line, target_rows)
                if not optical:
                    continue
                tone = 4 * 7 + 6 - optical['step']  # Treble middle line is B4.
                octave, index = divmod(tone, 7)
                step = 'CDEFGAB'[index]
                changed_by_key = ('FCGDAEB' if key > 0 else 'BEADGCF')[:abs(key)]
                if step in changed_by_key or any(pitch(e['node'])[0] == step and pitch(e['node'])[1]
                                                  for e in target):
                    continue
                previous = target[-1]['node']
                original_peer = notes[other, bar][-1]['node']
                note = ET.Element('note', {'default-x': original_peer.get('default-x', previous.get('default-x', '0'))})
                pt = ET.SubElement(note, 'pitch')
                ET.SubElement(pt, 'step').text = step
                ET.SubElement(pt, 'octave').text = str(octave)
                ET.SubElement(note, 'duration').text = str(div)
                ET.SubElement(note, 'voice').text = previous.findtext('voice', '1')
                ET.SubElement(note, 'type').text = 'quarter'
                ET.SubElement(note, 'stem').text = 'up'
                lyr = deepcopy(original_peer.find('lyric'))
                lyr.attrib.update(previous.find('lyric').attrib)
                note.append(lyr)
                measure = target[-1]['measure']
                measure.insert(list(measure).index(previous) + 1, note)
                changes.append(dict(part=part, measure=bar, peer=other, lyric=final_character,
                                    raw_lyric_item=words[0], pitch=f'{step}{octave}', unit=unit,
                                    onset=str(target[-1]['start'] + target[-1]['duration']), optical=optical))
    return (result if changes else root), dict(state='restored' if changes else 'no_changes', changes=changes)