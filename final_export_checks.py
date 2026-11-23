"""Final shared export boundary: preserve music, normalize shared chord beams, refuse conflicts."""
from pathlib import Path
import copy,xml.etree.ElementTree as E
from collections import Counter
from musicxml_compat import normalize_chord_beams
from musicxml_ties import normalize_tie_chain_order

def omit_numbering_text(tree):
    """Leave bar-number display to the score editor; keep musical number fields."""
    omitted_text = 0
    omitted_layout = 0
    for measure in tree.findall("part/measure"):
        for layout in measure.findall("print"):
            for numbering in layout.findall("measure-numbering"):
                layout.remove(numbering)
                omitted_layout += 1
        for direction in measure.findall("direction"):
            types = direction.findall("direction-type")
            if (len(types) != 1 or len(types[0]) != 1
                    or any(child.tag not in {"direction-type", "offset", "voice", "staff"}
                           for child in direction)
                    or direction.get("directive") == "yes"):
                continue
            word = types[0][0]
            if word.tag == "words" and (word.text or "").strip().isdecimal():
                measure.remove(direction)
                omitted_text += 1
    return dict(untyped_numeric_directions=omitted_text, display_overrides=omitted_layout)


def format_triplet_numbers(tree):
    formatted = 0
    for note in tree.findall("part/measure/note"):
        if (note.findtext("time-modification/actual-notes") != "3"
                or note.findtext("time-modification/normal-notes") != "2"):
            continue
        for tuplet in note.findall("notations/tuplet[@type='start']"):
            if tuplet.get("show-number") not in (None, "actual"):
                continue
            actual = tuplet.find("tuplet-actual")
            if actual is None:
                actual = E.Element("tuplet-actual")
                tuplet.insert(0, actual)
            number = actual.find("tuplet-number")
            if number is None:
                number = E.Element("tuplet-number")
                number.text = "3"
                actual.insert(0, number)
            if number.text == "3" and "font-size" not in number.attrib:
                number.set("font-size", "small")
                formatted += 1
    return formatted


def complete_triplet_display_ratios(tree):
    """Pair small triplet labels with their written normal count."""
    completed = 0
    for note in tree.findall("part/measure/note"):
        if (note.findtext("time-modification/actual-notes") != "3"
                or note.findtext("time-modification/normal-notes") != "2"):
            continue
        for tuplet in note.findall("notations/tuplet[@type='start']"):
            if tuplet.get("show-number") not in (None, "actual"):
                continue
            number = tuplet.find("tuplet-actual/tuplet-number")
            if (number is None or number.text != "3" or number.get("font-size") != "small"
                    or tuplet.find("tuplet-normal") is not None):
                continue
            # A renderer can treat an explicit actual count alone as a separate ratio.
            normal = E.SubElement(tuplet, "tuplet-normal")
            E.SubElement(normal, "tuplet-number").text = "2"
            completed += 1
    return completed

def order_numbered_lyrics(tree):
    """Put independent numbered lyric rows in verse order."""
    changes = 0
    for note in tree.findall("part/measure/note"):
        slots = [i for i, child in enumerate(note) if child.tag == "lyric"]
        lyrics = [note[i] for i in slots]
        numbers = [lyric.get("number", "") for lyric in lyrics]
        if len(numbers) < 2 or not all(n.isdecimal() for n in numbers):
            continue
        positions = [int(n) for n in numbers]
        if len(set(positions)) != len(positions) or positions == sorted(positions):
            continue
        for i, lyric in zip(slots, sorted(lyrics, key=lambda item: int(item.get("number")))):
            note[i] = lyric
        changes += 1
    return changes

def write_review_export(root,path):
    tree=copy.deepcopy(root)
    numbering = omit_numbering_text(tree)
    lyric_row_order = order_numbered_lyrics(tree)
    triplet_numbers = format_triplet_numbers(tree)
    triplet_ratios = complete_triplet_display_ratios(tree)
    ids=[p.get('id') for p in tree.findall('part')]
    listed=[p.get('id') for p in tree.findall('part-list/score-part')]
    if not ids or len(ids)!=len(set(ids)) or ids!=listed:
        raise ValueError('最终导出声部标识重复或与声部表不一致；未写新文件')
    beams=normalize_chord_beams(tree)
    if beams['unresolved']:
        raise ValueError('最终导出存在冲突的和弦符杠或时值；未写新文件')
    duplicate_playback_ties=0
    for note in tree.findall("part/measure/note"):
        seen=set()
        for tie in note.findall("tie"):
            if len(tie) or (tie.text or "").strip() or tie.get("type") not in ("start", "stop"):
                continue
            identity=tuple(sorted(tie.attrib.items()))
            if identity in seen:
                note.remove(tie)
                duplicate_playback_ties+=1
            else:
                seen.add(identity)
    ties=normalize_tie_chain_order(tree)
    ties['duplicate_playback_markers_removed']=duplicate_playback_ties
    if normalize_chord_beams(copy.deepcopy(tree))['changes']:
        raise ValueError('最终符杠规范化不稳定；未写新文件')
    labels={p.get('id'):[m.get('number') for m in p.findall('measure')] for p in tree.findall('part')}
    issues=[]
    reference=next(iter(labels.values()))
    for pid,nums in labels.items():
        if len(nums)!=len(set(nums)):issues.append(dict(kind='重复小节标识',part=pid))
        if nums!=reference:issues.append(dict(kind='声部小节序列不一致',part=pid))
    dynamics={'p','pp','ppp','pppp','f','ff','fff','ffff','mp','mf','sf','sfz','sffz','fp','fz','rfz'}
    for p in tree.findall('part'):
        for m in p.findall('measure'):
            for n in m.findall('note'):
                for lyric in n.findall('lyric'):
                    text=lyric.findtext('text','').strip()
                    if text.lower() in dynamics:
                        issues.append(dict(kind='疑似力度混入歌词',part=p.get('id'),measure=m.get('number'),text=text))
    # xb refuses accidental replacement. All checks run before creating a file.
    with Path(path).open('xb') as stream:
        E.ElementTree(tree).write(stream,encoding='utf-8',xml_declaration=True)
    return dict(beam_compatibility=beams,tie_compatibility=ties,numbering=numbering,triplet_numbers=triplet_numbers,triplet_ratios=triplet_ratios,lyric_row_order=lyric_row_order,issues=issues,
                scope='Final encoding and structural checks; not source-note accuracy or complete measure-loss detection')