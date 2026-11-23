"""Test a damaged accidental cluster against complete, attached printed glyphs."""

from collections import defaultdict
from xml.etree import ElementTree as ET

import numpy as np

from source_parallel_rest_gap import with_bounds
from source_repeated_piano_rest_lanes import contact
from source_upper_chord_flats import attached_accidentals
from source_vocal_split_clock import coordinate

THRESHOLDS = {"FLAT": .905, "NATURAL": .88, "SHARP": .86}


def samples(archive, units, load, space):
    by_shape = defaultdict(list)
    for unit in units:
        page = ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml"))
        for graph in page.findall("page/system"):
            nodes = with_bounds(graph)
            for head_id, signs in attached_accidentals(graph, nodes).items():
                head = nodes.get(head_id)
                if head is None or head.get("shape") not in ("NOTEHEAD_BLACK", "NOTEHEAD_VOID"):
                    continue
                if len(signs) != 1:
                    continue
                sign = signs[0]
                name = sign.get("shape")
                if name not in THRESHOLDS or float(sign.get("grade", "0")) < .75:
                    continue
                x,y,w,h = coordinate(sign)
                hx,hy,_,_ = coordinate(head)
                if not (.70 * space <= w <= 1.20 * space and 2.35 * space <= h <= 2.85 * space):
                    continue
                if not (.85 * space <= hx - x <= 1.6 * space and .65 * space <= hy - y <= 1.55 * space):
                    continue
                pixels = load(unit)[y:y+h,x:x+w]
                if pixels.shape != (h,w):
                    continue
                by_shape[name].append((unit, graph.get("id"), sign.get("id"), (x,y,w,h), hy-y, pixels.copy()))
    return by_shape


def match_template(image, row, sample, name, space):
    unit, system, source_id, box, dy, template = sample
    left, top = int(row["x"]), int(row["y"])
    h,w = template.shape
    x0,y0 = round(left - 2.55 * space), top - dy - 5
    latest = round(left - .85 * space)
    x1,y1 = latest+w, top-dy+5+h
    if x0 < 0 or y0 < 0 or x1 > image.shape[1] or y1 > image.shape[0]:
        return None
    tile = image[y0:y1,x0:x1]
    search = np.full((tile.shape[0], tile.shape[1] + w + 1), 255, dtype=np.uint8)
    search[:h,:w] = template
    search[:,w+1:] = tile
    rx=(latest-x0)//2
    fit=contact(search,(0,0,w,h),(w+1+rx,5),THRESHOLDS[name],(rx,5))
    if fit is None:
        return None
    x,y,_,_=fit["box"]
    x,y=int(x-w-1+x0),int(y+y0)
    if x+w > left-.12*space:
        return None
    return dict(shape=name,box=(x,y,w,h),source_unit=unit,system=system,donor=source_id,
                source_box=box,scores=[min(score,1.0) for score in fit["scores"]])


def signs_at(image, heads, donors, space):
    result = {}
    for row in heads:
        by_name = {}
        for name, templates in donors.items():
            hits = [hit for template in templates if (hit:=match_template(image,row,template,name,space)) is not None]
            if not hits:
                continue
            hits.sort(key=lambda h:min(h["scores"]), reverse=True)
            first=hits[0]
            if any(abs(first["box"][0]-h["box"][0])>3 or abs(first["box"][1]-h["box"][1])>3 for h in hits[1:]):
                return None
            by_name[name]=first
        if len(by_name)>1:
            return None
        if by_name:
            result[row["head"]]=next(iter(by_name.values()))
    return result
