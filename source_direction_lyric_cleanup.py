"""Suppress lyric fragments twice exported as ordinary words."""
from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import re


def sparse_lyric_duplicate(direction, measure, text, voice_lyrics, peers):
    """Require a matching local anchor and independent same-verse text."""
    fragment = re.fullmatch(r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", text.strip())
    if fragment is None or len(direction) != 1:
        return False
    first, last = fragment.groups()
    remaining = list(measure)[list(measure).index(direction) + 1:]
    anchor = next((item for item in remaining if item.tag != "direction"), None)
    if anchor is None or anchor.tag != "note" or anchor.find("pitch") is None:
        return False
    for lyric in anchor.findall("lyric"):
        verse = lyric.get("number", "1")
        phrase = voice_lyrics.get(verse, "")
        if lyric.findtext("text") != first or not any(p.get(verse) == phrase for p in peers):
            continue
        suffix = []
        for note in remaining:
            if note.tag in ("backup", "forward"):
                break
            if note.tag != "note" or note.findtext("voice", "1") != anchor.findtext("voice", "1"):
                continue
            suffix.extend(l.findtext("text", "") for l in note.findall("lyric") if l.get("number", "1") == verse)
        sung = "".join(suffix)
        if sung.startswith(first) and last in sung[1:]:
            return True
    return False

def cleanup(root):
    report = {"state": "unchanged", "changes": [], "pending": []}
    parts = root.findall("part")
    vocal_roles = {"soprano", "alto", "tenor", "bass"}
    by_id = {p.get("id"): p.findtext("part-name", "").casefold()
             for p in root.findall("part-list/score-part")}
    lines = defaultdict(dict)
    for part in parts:
        pid = part.get("id")
        if by_id.get(pid) not in vocal_roles:
            continue
        for index, bar in enumerate(part.findall("measure")):
            lyrics = defaultdict(list)
            for item in bar.findall("note/lyric"):
                if item.findtext("text"):
                    lyrics[item.get("number", "1")].append(item.findtext("text"))
            lines[pid][index] = {n: "".join(words) for n, words in lyrics.items()}
    clone = deepcopy(root)
    for part in clone.findall("part"):
        pid = part.get("id")
        role = by_id.get(pid)
        for i, measure in enumerate(part.findall("measure")):
            for direction in measure.findall("direction"):
                types = direction.findall("direction-type")
                if (len(types) != 1 or len(types[0]) != 1 or types[0][0].tag != "words"
                        or direction.get("directive") == "yes"
                        or any(c.tag not in ("direction-type", "staff", "voice", "offset") for c in direction)):
                    continue
                words = types[0][0]
                text = re.sub(r"\s+", "", words.text or "")
                token = re.sub(r"^\d+(?=[\u4e00-\u9fff])", "", text)
                token = re.sub(r"[、，。？！；：,.?!;:]", "", token)
                if not re.fullmatch(r"[\u4e00-\u9fff]+", token):
                    continue
                current = []
                stitched = []
                following = []
                for vf, bars in lines.items():
                    verses = bars.get(i, {})
                    following.extend(bars.get(i + 1, {}).values())
                    for verse, phrase in verses.items():
                        current.append(phrase)
                        stitched.append(phrase + bars.get(i + 1, {}).get(verse, "")
                                        + bars.get(i + 2, {}).get(verse, ""))
                try:
                    y = float(words.get("default-y", "0"))
                except ValueError:
                    continue
                in_lyrics = ((role in vocal_roles and direction.get("placement") == "below" and y < -50)
                             or (direction.get("placement") == "above" and 0 < y < 24))
                minimum = 1 if role in vocal_roles and y < -50 else 2
                certain = (any(token in p for p in current)
                           or (len(token) >= 3 and any(p and token[0] in p for p in current)
                               and any(token in phrase for phrase in stitched)))
                if in_lyrics and not certain and 0 < y < 24:
                    if len(token) == 2 and sum(phrase.startswith(token) for phrase in following) >= 2:
                        certain = True
                    if 4 <= len(token) <= 7 and all(any(c in p for p in current) for c in token):
                        # Interleaved letters from the two printed lyric rows.
                        certain = any(token[:i] + token[i+1:] in verse
                                      for i in range(len(token)) for verse in current)
                if in_lyrics and not certain and 6 <= len(token) <= 24 and y < -50:
                    # OCR can capture two bars of the same lyric with a character
                    # missing or corrupted. Require the anchored sung text on both sides.
                    for phrase in stitched:
                        if len(phrase) < len(token) - 2:
                            continue
                        limit = max(0, len(phrase) - len(token) + 3)
                        for start in range(limit):
                            candidate = phrase[start:start + len(token)]
                            if (len(candidate) >= 6 and token[0] == candidate[0]
                                    and SequenceMatcher(None, token, candidate, autojunk=False).ratio() >= .84):
                                certain = True
                                break
                        if certain:
                            break
                if in_lyrics and not certain and role in vocal_roles and y < -50:
                    peers = [bars.get(i, {}) for vf, bars in lines.items() if vf != pid]
                    certain = sparse_lyric_duplicate(direction, measure, words.text or "",
                                                     lines[pid].get(i, {}), peers)
                if in_lyrics and len(token) >= minimum and certain:
                    measure.remove(direction)
                    report["changes"].append({"part": pid, "measure": measure.get("number"), "text": words.text})
                elif in_lyrics and role in vocal_roles and y < -50 and len(token) >= 2:
                    report["pending"].append({"part": pid, "measure": measure.get("number"), "text": words.text})
    if report["changes"]:
        report["state"] = "restored"
        return clone, report
    return root, report
