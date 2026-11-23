"""Read pitch marks from a vector score for a source-proven parallel scale."""
from contextlib import closing
import ctypes
from dataclasses import dataclass
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pypdfium2 as pdfium
from pypdfium2 import raw

from source_native_notation import sha

STEPS = "CDEFGAB"
SIGNS = {"n": (0, "natural"), "#": (1, "sharp"), "b": (-1, "flat")}


@dataclass(frozen=True)
class Glyph:
    character: str
    left: float
    top: float
    right: float
    bottom: float

    @property
    def y(self):
        return (self.top + self.bottom)/2


class VectorScore:
    def __init__(self, source, mapping):
        self.document = pdfium.PdfDocument(source)
        self.mapping = {str(entry["unit"]): entry for entry in mapping}
        self.cache = {}
        self.curves = {}
        self.beams = {}

    def close(self):
        self.document.close()

    def page(self, unit):
        meta = self.mapping.get(unit, {})
        if meta.get("source_mode") != "page_render":
            return None
        number = meta["pdf_page"]
        if number not in self.cache:
            with closing(self.document[number-1]) as page:
                if page.get_rotation() != 0 or any(abs(a-b)>.05 for a,b in zip(page.get_size(),meta["pdf_page_size"],strict=True)):
                    return None
                height = page.get_height()
                glyphs = []
                with closing(page.get_textpage()) as text:
                    for i in range(text.count_chars()):
                        ch = text.get_text_range(i,1,force_this=True)
                        if ch not in {"œ","˙","w","j","n","#","b","&","√","@","~","?","J",".","Œ","‰"}:
                            continue
                        name = ctypes.create_string_buffer(128)
                        size = raw.FPDFText_GetFontInfo(text,i,name,len(name),None)
                        if not 0<size<=len(name) or name.value.split(b"+")[-1] != b"Maestro":
                            continue
                        left, bottom, right, top = text.get_charbox(i)
                        glyphs.append(Glyph(ch,left,height-top,right,height-bottom))
                paths = []
                curves = []
                beams = []
                for obj in page.get_objects():
                    if obj.type != raw.FPDF_PAGEOBJ_PATH:
                        continue
                    segments = raw.FPDFPath_CountSegments(obj)
                    if segments not in (2, 4, 5, 7, 9):
                        continue
                    l,b,r,t = obj.get_pos()
                    if segments == 2:
                        paths.append((l,height-t,r,height-b,raw.FPDFPageObj_GetDashCount(obj)))
                    elif segments in (4,5):
                        fill,stroke=ctypes.c_long(),ctypes.c_long()
                        if raw.FPDFPath_GetDrawMode(obj,ctypes.byref(fill),ctypes.byref(stroke)) and fill.value and r-l>12 and 1.2<=t-b<=6:
                            beams.append((l,height-t,r,height-b))
                    else:
                        curves.append((l,height-t,r,height-b))
                self.cache[number] = glyphs, paths
                self.curves[number] = curves
                self.beams[number] = beams
        glyphs,paths = self.cache[number]
        crop = meta["crop"]
        width,height = meta["size"]
        xscale = width/(crop[2]-crop[0])
        yscale = height/(crop[3]-crop[1])
        def position(row):
            return ((float(row["x"])/xscale + crop[0])/2,
                    (float(row["y"])/yscale + crop[1])/2)
        return glyphs, paths, position


def open_source(path, job):
    if path is None:
        return None
    source = Path(path)
    manifest = job/"manifest.json"
    mapping = job/"source-map.json"
    if not all(file.is_file() for file in (source,manifest,mapping)):
        return None
    if sha(source) != json.loads(manifest.read_text(encoding="utf8")).get("sha256"):
        return None
    return VectorScore(source,json.loads(mapping.read_text(encoding="utf8")))


def aligned_heads(keys, rows, glyphs, position):
    result = []
    for key in keys:
        x,y = position(rows[key])
        close = [g for g in glyphs if g.character=="œ" and abs(g.left-x)<1.05 and abs(g.top-y)<1.05]
        if len(close)!=1:
            return None
        result.append(close[0])
    return result if len(set(result))==len(keys) else None


def ottava(heads, glyphs, paths):
    choices = []
    for label in glyphs:
        if label.character!="√" or not any(
            label.left<=g.left<=label.right and 1<=g.top-label.bottom<=20 for g in heads
        ):
            continue
        for l,t,r,b,dashes in paths:
            if (dashes!=2 or not 0<=l-label.right<=10 or not t<=label.y<=b+3
                    or b-t>1.7 or r-l<35):
                continue
            hooked = any(d==0 and abs(a-r)<1.5 and abs(c-r)<1.5 and abs(y-t)<1.5 and 2<=z-y<=5
                         for a,y,c,z,d in paths)
            if not hooked:
                continue
            inside = [i for i,g in enumerate(heads) if l-1.5<=g.left and (g.left+g.right)/2<=r+1.5]
            if len(inside)>=3 and inside==list(range(inside[0],inside[-1]+1)):
                choices.append((inside[0],inside[-1],(l,t,r,b)))
    return choices[0] if len(choices)==1 else None


def treble_change(heads, glyphs):
    options = []
    for g in glyphs:
        if g.character!="&" or not 17<=g.bottom-g.top<=35:
            continue
        for i,(left,right) in enumerate(zip(heads,heads[1:]),1):
            if (left.left+2<g.left<g.right<=right.left-1 and
                    abs(g.bottom-right.bottom)<5 and right.top-left.top>14):
                options.append((i,g))
    return options[0] if len(options)==1 else None


def context(part, target):
    fifths = None
    clef = None
    following = None
    measures = part.findall("measure")
    for index,measure in enumerate(measures):
        if measure is target:
            if index+1<len(measures):
                following = measures[index+1].find("attributes/clef[@number='2']")
            break
        for attr in measure.findall("attributes"):
            if attr.find("key") is not None:
                keys = attr.findall("key")
                fifths = keys[0].findtext("fifths") if len(keys)==1 and keys[0].get("number") is None else None
            for item in attr.findall("clef[@number='2']"):
                clef = (item.findtext("sign"),item.findtext("line"),item.findtext("clef-octave-change","0"))
    if clef!=("F","4","0") or following is None or (
        following.findtext("sign"),following.findtext("line"),following.findtext("clef-octave-change","0")
    )!=("G","2","0") or fifths is None or not -7<=int(fifths)<=7:
        return None
    fifths=int(fifths)
    return {step: (1 if fifths>0 else -1)
            for step in ("FCGDAEB" if fifths>0 else "BEADGCF")[:abs(fifths)]}


def pitches(notes,heads,glyphs,key,delta):
    output = []
    used = set()
    for i,(note,head) in enumerate(zip(notes,heads,strict=True)):
        p = note.find("pitch")
        n = STEPS.index(p.findtext("step"))+7*int(p.findtext("octave"))+delta(i)
        step, octave = STEPS[n%7], n//7
        signs = [g for g in glyphs if g.character in SIGNS and 3.7<=head.left-g.left<=7.5
                 and 0.8<=head.left-g.right<=4.2 and abs(g.y-head.y)<=1.08]
        if len(signs)>1 or any(g in used for g in signs):
            return None
        used.update(signs)
        alteration,symbol = SIGNS[signs[0].character] if signs else (key.get(step,0),None)
        if note.find("accidental") is not None and note.findtext("accidental")!=symbol:
            return None
        if n != (output[-1][0]+1 if output else n) or not 0<=octave<=9:
            return None
        output.append((n,step,octave,alteration,symbol))
    return output,used


def set_pitch(note, data):
    _,step,octave,alteration,marker = data
    pitch=note.find("pitch")
    pitch.find("step").text=step
    pitch.find("octave").text=str(octave)
    current=pitch.find("alter")
    if current is not None:
        pitch.remove(current)
    if alteration or marker=="natural":
        value=ET.Element("alter")
        value.text=str(alteration)
        pitch.insert(1,value)
    if marker and note.find("accidental") is None:
        sign=ET.Element("accidental")
        sign.text=marker
        before=next((e for e in note if e.tag in ("time-modification","stem","notehead","staff","beam","notations")),None)
        note.insert(list(note).index(before) if before is not None else len(note),sign)


def direction(kind, staff):
    d=ET.Element("direction",placement="above")
    ET.SubElement(ET.SubElement(d,"direction-type"),"octave-shift",type=kind,size="8",number="1")
    ET.SubElement(d,"staff").text=staff
    return d


def apply(vector,unit,part,original,result,upper,lower,rows):
    page=vector.page(unit)
    if page is None:
        return None
    glyphs,paths,position=page
    up,down=[aligned_heads(lane,rows,glyphs,position) for lane in (upper,lower)]
    key=context(part,original)
    if up is None or down is None or key is None or set(up)&set(down):
        return None
    octave=ottava(up,glyphs,paths)
    clef=treble_change(down,glyphs)
    if octave is None or clef is None:
        return None
    start,stop,stroke=octave
    cut,clef_mark=clef
    upper_notes=result.findall("note")[:len(up)]
    lower_notes=result.findall("note")[len(up):]
    if ({n.findtext("staff") for n in upper_notes[start:stop+1]}!={"1"} or
        {n.findtext("staff") for n in lower_notes}!={"2"}):
        return None
    top=pitches(upper_notes,up,glyphs,key,lambda i: 7 if start<=i<=stop else 0)
    bottom=pitches(lower_notes,down,glyphs,key,lambda i: 12 if i>=cut else 0)
    if top is None or bottom is None or top[1]&bottom[1]:
        return None
    signs=top[1]|bottom[1]
    for g in glyphs:
        # Any sign sufficiently close to these heads must be uniquely explained.
        if g.character in SIGNS and any(3.7<=h.left-g.left<=7.5 and abs(h.y-g.y)<=1.35 for h in up+down) and g not in signs:
            return None
    before,after=upper_notes[start],upper_notes[stop]
    result.insert(list(result).index(before),direction("down","1"))
    result.insert(list(result).index(after)+1,direction("stop","1"))
    attrs=ET.Element("attributes")
    clef_element=ET.SubElement(attrs,"clef",number="2")
    ET.SubElement(clef_element,"sign").text="G"
    ET.SubElement(clef_element,"line").text="2"
    result.insert(list(result).index(lower_notes[cut]),attrs)
    changed=0
    for notes,series in ((upper_notes,top[0]),(lower_notes,bottom[0])):
        for note,pitch in zip(notes,series,strict=True):
            old=ET.tostring(note.find("pitch"))
            set_pitch(note,pitch)
            changed+=old!=ET.tostring(note.find("pitch"))
    return dict(physical_noteheads=len(up)+len(down), explicit_accidentals=len(signs),
                clef_note_index=cut, ottava_first_index=start, ottava_last_index=stop,
                dash_bounds_pdf=stroke, clef_left_pdf=clef_mark.left, pitch_elements_changed=changed)

