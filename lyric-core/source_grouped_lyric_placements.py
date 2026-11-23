"""Resolve an existing grouped CJK lyric only when two source views agree."""
import re


def grouped_lyric_placements(text, anchor, characters, columns, views, font_height):
    if not re.fullmatch(r"[\u3400-\u9fff]{2,}[，。；、！？：]*", text):
        return ()
    if not views[0] or views[0] != views[1] or views[0].count(text) != 1:
        return ()
    length = len(text.rstrip("，。；、！？："))
    columns = sorted(columns)
    windows = []
    for first in range(len(characters) - length + 1):
        segment = characters[first:first + length]
        printed = "".join(c["text"] + (c.get("punctuation") or "") for c in segment)
        if printed == text:
            windows.append(segment)
    if len(windows) != 1:
        return ()

    placements = []
    for char in windows[0]:
        if char["confidence"] < .98:
            return ()
        x, _, width, _ = char["bounds"]
        center = x + width / 2
        distances = sorted((abs(center - column), index, key)
                           for index, (column, key) in enumerate(columns))
        if not distances or distances[0][0] > .25 * font_height:
            return ()
        if len(distances) > 1 and distances[1][0] - distances[0][0] < .5 * font_height:
            return ()
        _, index, key = distances[0]
        if placements and index != placements[-1][0] + 1:
            return ()
        placements.append((index, key, char["text"] + (char.get("punctuation") or "")))

    if anchor not in {key for _, key, _ in placements}:
        return ()
    return tuple((key, char) for _, key, char in placements)


def source_view_transcripts(views):
    by_kind = {view["kind"]: view.get("ocr_text") for view in views}
    return {
        kind: "".join(line.split("\t")[-1] for line in (by_kind.get(kind) or "").splitlines()
                      if line.startswith("C\t"))
        for kind in ("original", "packed")
    }


def apply_grouped_lyrics(events, candidates, local_ocr, scope, changes):
    from xml.etree import ElementTree as ET

    transcripts = local_ocr.get("grouped_lyric_views", {})
    views = (transcripts.get("original"), transcripts.get("packed"))
    if not views[0] or views[0] != views[1]:
        return

    def can_sing(note):
        return note.find("tie[@type='stop']") is None and note.find("grace") is None

    columns = [(x, key) for x, key, _ in candidates if can_sing(events[key]["node"])]
    characters = local_ocr["characters"]
    for _, anchor in columns:
        note = events[anchor]["node"]
        existing = note.findall("lyric")
        if len(existing) != 1 or any(child.tag not in {"syllabic", "text"} for child in existing[0]):
            continue
        prior = existing[0]
        if len(prior.findall("text")) != 1 or prior.get("number") not in (None, "1"):
            continue

        placements = grouped_lyric_placements(
            prior.findtext("text", ""), anchor, characters, columns, views, local_ocr["font_height"],
        )
        if not placements:
            continue
        targets = [(key, text, events[key]["node"]) for key, text in placements]
        voice = note.findtext("voice", "1")
        if any(key[0] != anchor[0] or target.findtext("voice", "1") != voice
               for key, _, target in targets):
            continue
        if any(key != anchor and [(lyric.get("number") or "1", lyric.findtext("text"))
                                 for lyric in target.findall("lyric")]
               not in ([], [("1", text)]) for key, text, target in targets):
            continue

        note.remove(prior)
        for key, text, target in targets:
            if target.find("lyric") is not None:
                continue
            lyric = ET.Element("lyric", number="1", placement="below")
            ET.SubElement(lyric, "syllabic").text = "single"
            ET.SubElement(lyric, "text").text = text
            insertion = next((index for index, child in enumerate(target)
                              if child.tag in {"play", "listen"}), len(target))
            target.insert(insertion, lyric)
            changes.append(dict(
                part=key[0], measure=key[1], note_index=key[2],
                before=[prior.findtext("text")] if key == anchor else [], after=text,
                source_scope=list(scope), evidence_mode="two_source_views_and_unique_note_columns",
            ))
