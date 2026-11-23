"""Remove isolated page-overlay objects only from the in-memory recognition view."""

import ctypes
import re

from pypdfium2 import raw


def object_text(obj, text_page):
    count = raw.FPDFTextObj_GetText(obj, text_page, None, 0)
    if not count or count > 2048:
        return ""
    buf = (ctypes.c_ushort * ((count + 1) // 2))()
    if raw.FPDFTextObj_GetText(obj, text_page, buf, count) != count:
        return ""
    return bytes(buf).decode("utf-16le", errors="replace").strip("\0 ")


def is_page_watermark(obj, text_page, width, height):
    left, bottom, right, top = obj.get_pos()
    matrix = obj.get_matrix()
    if (right - left < width * .50 or top - bottom < height * .32
            or abs(matrix.b) < .35 * abs(matrix.a)):
        return False
    members = list(obj.page.get_objects(form=obj, max_depth=1))
    if not members or any(child.type != raw.FPDF_PAGEOBJ_TEXT for child in members):
        return False
    for child in members:
        tint = [ctypes.c_uint() for _ in range(4)]
        if not raw.FPDFPageObj_GetFillColor(child, *map(ctypes.byref, tint)):
            return False
        r, g, b, alpha = [channel.value for channel in tint]
        if max(r, g, b) - min(r, g, b) > 10 or not (0 < alpha < 130 or 145 < r < 246):
            return False
        if len(object_text(child, text_page)) < 4:
            return False
    return True


def suppress_page_overlays(page):
    width, height = page.get_size()
    objects = list(page.get_objects(max_depth=1))
    if not objects:
        return []
    text_page = page.get_textpage()
    watermarks, footers, emails = [], [], []
    try:
        for obj in objects:
            if obj.type == raw.FPDF_PAGEOBJ_FORM and is_page_watermark(obj, text_page, width, height):
                watermarks.append(obj)
            elif obj.type == raw.FPDF_PAGEOBJ_TEXT:
                left, bottom, right, top = obj.get_pos()
                if not (0 <= bottom < top < height * .085 and top - bottom < 16):
                    continue
                value = object_text(obj, text_page)
                if re.match(r"all rights\s+(?:reserved(?:\s+by)?|by)\b", value, re.I):
                    footers.append((obj, bottom))
                elif re.fullmatch(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", value, re.I):
                    emails.append((obj, top))
    finally:
        text_page.close()
    selected = [(obj, "diagonal_translucent_watermark") for obj in watermarks]
    selected += [(obj, "copyright_footer") for obj, _ in footers]
    selected += [(obj, "copyright_footer_contact") for obj, top in emails
                 if any(0 < bottom - top < 24 for _, bottom in footers)]
    for obj, _ in selected:
        page.remove_obj(obj)
        obj.close()
    return [reason for _, reason in selected]
