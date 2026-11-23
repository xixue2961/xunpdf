"""Normalize source-page furniture without changing any part's music.

Only geometrically proven staff labels and centered source page numbers qualify.
Removed XML stays in a reversible identification/miscellaneous receipt.
"""
import json
import math
import re
import xml.etree.ElementTree as ET

_FAMILIES = (
    {'soprano', 's.', '女高音', '高音女声'},
    {'alto', 'a.', '女低音', '低音女声'},
    {'tenor', 't.', '男高音', '高音男声'},
    {'bass', 'b.', '男低音', '低音男声'},
    {'piano', 'pn.', 'pno.', '钢琴', '鋼琴'},
)
_RECEIPT = 'xun-source-layout-credits'

def _number(node, path):
    v = float(node.findtext(path))
    if not math.isfinite(v):
        raise ValueError('non-finite source layout')
    return v

def _text(value):
    return ' '.join((value or '').split()).casefold()

def _staff_regions(root):
    layout = root.find('defaults/page-layout')
    parts = root.findall('part')
    if layout is None or not parts:
        return []
    try:
        height = _number(layout, 'page-height')
        margins = next((e for e in layout.findall('page-margins') if e.get('type', 'both') in ('both', 'odd')), None)
        if margins is None:
            return []
        first = parts[0].find('measure/print/system-layout')
        if first is None:
            return []
        top = height - _number(margins, 'top-margin') - _number(first, 'top-system-distance')
        left = _number(margins, 'left-margin') + _number(first, 'system-margins/left-margin')
        names = {e.get('id'): e for e in root.findall('part-list/score-part')}
        regions = []
        for pi, part in enumerate(parts):
            measure = part.find('measure')
            if measure is None or part.get('id') not in names:
                return []
            count = int(measure.findtext('attributes/staves', '1'))
            if not 1 <= count <= 8:
                return []
            part_top = None
            for staff in range(1, count + 1):
                if pi or staff > 1:
                    staff_layout = measure.find(f"print/staff-layout[@number='{staff}']")
                    if staff_layout is None:
                        return []
                    top -= _number(staff_layout, 'staff-distance')
                details = measure.find(f"attributes/staff-details[@number='{staff}']")
                if details is None and staff == 1:
                    details = measure.find('attributes/staff-details')
                lines = int(details.findtext('staff-lines', '5')) if details is not None else 5
                if details is not None and details.findtext('staff-size', '100') != '100':
                    return []
                if not 1 <= lines <= 6:
                    return []
                if part_top is None:
                    part_top = top
                top -= (lines - 1) * 10
            entry = names[part.get('id')]
            aliases = {_text(entry.findtext('part-name')), _text(entry.findtext('part-abbreviation'))}
            for family in _FAMILIES:
                if aliases & family:
                    aliases |= family
            aliases.discard('')
            regions.append(dict(part=part.get('id'), left=left, top=part_top, bottom=top, aliases=aliases))
        return regions
    except (TypeError, ValueError, OverflowError):
        return []

def normalize_source_layout_credits(root):
    ident = root.find('identification')
    if ident is None:
        return []
    existing = ident.find(f"miscellaneous/miscellaneous-field[@name='{_RECEIPT}']")
    try:
        prior = json.loads(existing.text) if existing is not None else []
        if not isinstance(prior, list):
            return []
    except (TypeError, ValueError):
        return []
    regions = _staff_regions(root)
    layout = root.find('defaults/page-layout')
    try:
        width = _number(layout, 'page-width')
        height = _number(layout, 'page-height')
        if width <= 0 or height <= 0:
            return []
    except (AttributeError, TypeError, ValueError, OverflowError):
        return []
    decisions = []
    for credit in list(root.findall('credit')):
        words = credit.findall('credit-words')
        types = [_text(e.text) for e in credit.findall('credit-type')]
        if len(words) != 1 or any(t not in ('page number', 'rights') for t in types):
            continue
        # Mixed credit content, links, or images may carry attribution: preserve.
        if any(e.tag not in ('credit-type', 'credit-words') for e in credit) or list(words[0]):
            continue
        word = words[0]
        text = _text(word.text)
        try:
            x = float(word.get('default-x'))
            y = float(word.get('default-y'))
            page = int(credit.get('page', '1'))
            if not all(math.isfinite(v) for v in (x, y)) or page < 1:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        reason = None
        if text == str(page) and abs(x - width / 2) <= width * .08 and 0 <= y <= height * .10:
            reason = 'centered_source_page_number'
        elif (not types or types == ['rights']) and 0 <= y <= height * .10 and re.match(
                r"all rights\s+(?:reserved(?:\s+by)?|by)\b", text):
            reason = 'source_copyright_footer_kept_in_metadata'
        elif page == 1 and not types:
            matches = [v for v in regions if text in v['aliases'] and 0 <= x <= v['left'] - 10 and v['bottom'] - 10 <= y <= v['top'] + 10]
            if len(matches) == 1:
                reason = 'source_staff_label_already_represented_by_part_list'
        if reason:
            decisions.append(dict(reason=reason, page=page, text=word.text, original_xml=ET.tostring(credit, encoding='unicode')))
            root.remove(credit)
    if decisions:
        ident = root.find('identification')
        if ident is None:
            raise ValueError('identification must be prepared before source-layout normalization')
        misc = ident.find('miscellaneous')
        if misc is None:
            misc = ET.SubElement(ident, 'miscellaneous')
        receipt = misc.find(f"miscellaneous-field[@name='{_RECEIPT}']")
        if receipt is None:
            receipt = ET.SubElement(misc, 'miscellaneous-field', name=_RECEIPT)
            prior = []
        else:
            prior = json.loads(receipt.text)
        receipt.text = json.dumps(prior + decisions, ensure_ascii=False, separators=(',', ':'))
    return decisions