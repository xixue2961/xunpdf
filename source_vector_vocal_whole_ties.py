"""Restore source-proven vocal whole notes mistaken for full-measure rests."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

from source_long_tuplet_context import clocks
from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import add_mark, add_tie, current_key
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import STEPS, open_source
from source_vector_sustained_chords import anchored_site, note_value


CLEF_BASES = {"G_CLEF": 38, "G_CLEF_8VB": 31, "F_CLEF": 26}


def single_arc(curves, predicate):
    hits = [q for q in curves if predicate(q)]
    return bool(hits) and all(max(abs(a - b) for a, b in zip(q, hits[0])) < .7 for q in hits[1:])


def single_measure_rest(measure, div):
    notes = measure.findall("note")
    if len(notes) != 1:
        return None
    n = notes[0]
    if (n.find("rest[@measure='yes']") is None or n.findtext("duration") != str(4 * div)
            or any(c.tag not in ("rest", "duration", "voice", "staff") for c in n)):
        return None
    return n


def glyph_site(vector, site):
    view = vector.page(site[0])
    if view is None:
        return None
    bounds = scope_at(site, view, 1)
    if bounds is None:
        return None
    lo, hi, top, bottom = bounds
    space = (bottom - top) / 4
    if not 3.5 < space < 5.6:
        return None
    staff = site[1].find(f"part[@id='{site[2]}']/staff")
    clefs = [c for c in site[1].findall("sig/inters/clef")
             if c.get("staff") == staff.get("id") and c.get("frozen") == "true"]
    if len(clefs) != 1 or clefs[0].get("shape") not in CLEF_BASES:
        return None
    base = CLEF_BASES[clefs[0].get("shape")]
    glyphs = [g for g in view[0] if lo < g.left < hi and top - 8 < g.y < bottom + 8]
    curves = vector.curves[vector.mapping[site[0]]["pdf_page"]]
    return view[2], bounds, base, glyphs, curves


def whole_source(part, measure, source, first):
    _, (lo, hi, top, bottom), base, glyphs, _ = source
    key = current_key(part, measure)
    if key is None or any(g.character in ("œ", "˙", "Œ", "‰", "j", "J", "#", "n") for g in glyphs):
        return None
    whole = [g for g in glyphs if g.character == "w"]
    if len(whole) != 1 or not 6.8 < whole[0].right - whole[0].left < 8.8:
        return None
    signs = [g for g in glyphs if g.character == "b"]
    if first:
        if any(v != -1 for v in key.values()) or len(signs) != len(key) or any(g.right >= whole[0].left - 4 for g in signs):
            return None
    elif signs:
        return None
    head = whole[0]
    position = (head.y - top) * 8 / (bottom - top)
    if abs(position - round(position)) > .22:
        return None
    pitch = base - round(position)
    step = STEPS[pitch % 7]
    return head, (step, pitch // 7, key.get(step, 0))


def actual_endpoint(key, event, source, ledger, original):
    if key not in ledger or key not in original:
        return None
    node, old = event["node"], original[key]["node"]
    if (node.find("pitch") is None or old.find("pitch") is None or
            note_value(node) != note_value(old) or node.get("default-x") is None or
            node.get("default-x") != old.get("default-x") or node.find("grace") is not None):
        return None
    position, _, _, glyphs, _ = source
    x, y = position(ledger[key])
    hits = [g for g in glyphs if g.character in ("w", "˙", "œ")
            and abs(g.left - x) < .95 and abs(g.top - y) < .95]
    return hits[0] if len(hits) == 1 else None


def same_system_tie(left, right, curves):
    if abs(left.y - right.y) > .35:
        return False
    return single_arc(curves, lambda q: left.left + 2 < q[0] < left.right + 3 and
                      right.left - 2 < q[2] < right.right and
                      (2 < left.y - q[3] < 6 or 2 < q[1] - left.y < 6))


def incoming_proof(head, source, previous, prev_source):
    _, (lo, hi, _, _), _, _, curves = source
    _, (_, stop, _, _), _, _, prior_curves = prev_source
    for above in (True, False):
        left_arc = single_arc(prior_curves, lambda q: previous.left + 2 < q[0] < previous.right + 3
                              and stop - 2 < q[2] < stop + 2
                              and (2 < previous.y - q[3] < 6 if above else 2 < q[1] - previous.y < 6))
        right_arc = single_arc(curves, lambda q: head.left - 9 < q[0] < head.left - 3
                               and head.left < q[2] < head.right
                               and (2 < head.y - q[3] < 6 if above else 2 < q[1] - head.y < 6))
        if left_arc and right_arc:
            return True
    return False


def bridge_phrase(previous, prev_ink, begin, landing, current_ink):
    if not previous or prev_ink is None:
        return False
    _, (_, old_right, _, _), _, _, old_curves = prev_ink
    new_curves = current_ink[-1]
    # The printed long phrase leaves the last staff and resumes above both tied wholes.
    leaving = single_arc(old_curves, lambda q: previous.left + 2 < q[0] < previous.right + 3
                         and old_right - 2 < q[2] < old_right + 2
                         and 3 < previous.y - q[3] < 8 and q[2] - q[0] > 40)
    arriving = single_arc(new_curves, lambda q: begin.left - 12 < q[0] < begin.left - 4
                          and landing.left < q[2] < landing.right
                          and 3 < landing.y - q[3] < 8 and q[2] - q[0] > 40)
    return leaving and arriving


def install_note(rest, pitch, ties):
    step, octv, alt = pitch
    n = E.Element("note")
    p = E.SubElement(n, "pitch")
    E.SubElement(p, "step").text = step
    if alt:
        E.SubElement(p, "alter").text = str(alt)
    E.SubElement(p, "octave").text = str(octv)
    E.SubElement(n, "duration").text = rest.findtext("duration")
    E.SubElement(n, "voice").text = rest.findtext("voice", "1")
    E.SubElement(n, "type").text = "whole"
    if rest.find("staff") is not None:
        E.SubElement(n, "staff").text = rest.findtext("staff")
    for kind in ties:
        add_tie(n, kind)
    return n


def recover(root, job, source):
    report = {"state": "unchanged", "changes": []}
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, reference, ledger = control
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    events, _ = _score_events(fixed)
    original, _ = _score_events(reference)
    scopes, rows, sites = defaultdict(list), defaultdict(list), defaultdict(set)
    for k, e in events.items():
        scopes[k[:2]].append((k, e))
    for k, r in ledger.items():
        rows[k[:2]].append(r)
        sites[k[0], r["unit"], r["system"]].add(r["native_part"])
    listed = {p.get("id"): p.findtext("part-name", "").casefold() for p in root.findall("part-list/score-part")}
    pianos = [p for p, name in listed.items() if name == "piano"]
    if len(pianos) != 1:
        vector.close()
        return root, report
    piano = pianos[0]
    verified, frames = {}, {}
    with closing(vector), ZipFile(archive) as graph:
        def signed_scope(key):
            site = anchored_site(rows[key], graph, frames)
            if site is None:
                return None
            unit = site[0]
            if unit not in verified:
                image = job / f"unit-{int(unit):03d}.png"
                verified[unit] = sha(image) if image.is_file() else None
            return site if verified[unit] == site[3] else None

        for part in fixed.findall("part"):
            pid = part.get("id")
            if listed.get(pid) not in ("soprano", "alto", "tenor", "bass"):
                continue
            bars = part.findall("measure")
            rhythm = clocks(part)
            for i, bar in enumerate(bars):
                if i == 0 or bar.find("print[@new-system='yes']") is None:
                    continue
                labels = [m.get("number") for m in bars[i:i + 2]]
                rests, proof, inks = [], [], []
                identity = None
                for offset, label in enumerate(labels):
                    div, capacity = rhythm[i + offset]
                    rest = single_measure_rest(bars[i + offset], div) if capacity == 4 else None
                    if rest is None:
                        break
                    site = signed_scope((piano, label))
                    if site is None or site[4] != offset or (identity is not None and identity != (site[0], site[1].get("id"))):
                        break
                    identity = (site[0], site[1].get("id"))
                    native = sites[pid, *identity]
                    if len(native) != 1:
                        break
                    ink = glyph_site(vector, (site[0], site[1], next(iter(native)), site[3], offset))
                    if ink is None:
                        break
                    found = whole_source(part, bars[i + offset], ink, offset == 0)
                    if found is None:
                        break
                    rests.append(rest)
                    proof.append(found)
                    inks.append(ink)
                if not proof:
                    continue

                if len(proof) == 1:
                    previous_label = bars[i - 1].get("number")
                    previous_scope = signed_scope((pid, previous_label))
                    previous_items = scopes[pid, previous_label]
                    if previous_scope is None or not previous_items or (previous_scope[0], previous_scope[1].get("id")) == identity:
                        continue
                    k, e = previous_items[-1]
                    n = e["node"]
                    if (n.find("pitch") is None or n.find("chord") is not None or n.find("tie[@type='start']") is not None
                            or note_value(n) != proof[0][1] or e["start"] + e["duration"] != 4
                            or n.findtext("voice", "1") != rests[0].findtext("voice", "1")):
                        continue
                    prev_ink = glyph_site(vector, previous_scope)
                    g = actual_endpoint(k, e, prev_ink, ledger, original) if prev_ink is not None else None
                    if g is None or not incoming_proof(proof[0][0], inks[0], g, prev_ink):
                        continue
                    add_tie(n, "start")
                    marks = [("stop",)]
                else:
                    if i + 2 >= len(bars) or proof[0][1] != proof[1][1]:
                        continue
                    after = bars[i + 2].get("number")
                    landing = signed_scope((pid, after))
                    items = scopes[pid, after]
                    if (landing is None or not items or (landing[0], landing[1].get("id")) != identity or landing[4] != 2):
                        continue
                    k, e = items[0]
                    n = e["node"]
                    if (e["start"] != 0 or n.find("pitch") is None or n.find("tie[@type='stop']") is not None
                            or note_value(n) != proof[0][1] or n.findtext("voice", "1") != rests[0].findtext("voice", "1")
                            or rests[0].findtext("voice", "1") != rests[1].findtext("voice", "1")):
                        continue
                    following = glyph_site(vector, landing)
                    g = actual_endpoint(k, e, following, ledger, original) if following is not None else None
                    if (g is None or not same_system_tie(proof[0][0], proof[1][0], inks[0][-1])
                            or not same_system_tie(proof[1][0], g, inks[1][-1])):
                        continue
                    add_tie(n, "stop")
                    marks = [("start",), ("stop", "start")]
                    previous_label = bars[i - 1].get("number")
                    context = signed_scope((pid, previous_label))
                    earlier = scopes[pid, previous_label]
                    if context is not None and earlier and earlier[0][1]["start"] == 0:
                        prior_key, prior = earlier[0]
                        pn = prior["node"]
                        old_ink = glyph_site(vector, context)
                        first_glyph = actual_endpoint(prior_key, prior, old_ink, ledger, original) if old_ink is not None else None
                        if (pn.find("pitch") is not None and note_value(pn) != note_value(n)
                                and pn.find("notations/slur") is None and n.find("notations/slur") is None
                                and (context[0], context[1].get("id")) != identity
                                and bridge_phrase(first_glyph, old_ink, proof[0][0], g, inks[0])):
                            used = {slur.get("number", "1") for slur in part.findall("measure/note/notations/slur")}
                            number = next((str(number) for number in range(1, 17) if str(number) not in used), None)
                            if number:
                                add_mark(pn, E.Element("slur", type="start", number=number, placement="above"))
                                add_mark(n, E.Element("slur", type="stop", number=number))
                                report["phrase_restored"] = {"part": pid, "from": previous_label, "to": after}

                for offset, (rest, (_, pitch), ties) in enumerate(zip(rests, proof, marks, strict=True)):
                    current = bars[i + offset]
                    pos = list(current).index(rest)
                    current[pos] = install_note(rest, pitch, ties)
                    report["changes"].append({"part": pid, "measure": current.get("number"),
                                              "pitch": list(pitch), "tie": list(ties), "source_unit": identity[0]})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
