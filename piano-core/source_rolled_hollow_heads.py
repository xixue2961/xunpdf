
"""Scan an original raster for aligned hollow heads within a printed stem."""
from collections import defaultdict
import csv
from hashlib import file_digest, sha256
from itertools import product
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def bounds(node):
    return tuple(int(node.find("bounds").get(a)) for a in ("x", "y", "w", "h"))


def head_scores(image, row, spacing, depth):
    x, y, w, h = (int(row[k]) for k in ("x", "y", "w", "h"))
    left = x + round(.30 * spacing)
    width = w - (left - x) - 1
    donor = image[y:y+h, left:left+width]
    line_left, line_right = x + round(1.9 * spacing), x + round(3.7 * spacing)
    line = (image[:, line_left:line_right] < 180).mean(axis=1) >= .60
    candidates = []
    for dx, dy in product(range(-1, 2), range(-2, 3)):
        top = y + round(depth * spacing / 2) + dy
        xx = left + dx
        sample = image[top:top+h, xx:xx+width]
        keep = ~(line[y:y+h] | line[top:top+h])
        if sample.shape != donor.shape or keep.sum() < h / 2:
            continue
        scores = []
        for cutoff in (110, 160, 195):
            ref, ink = donor[keep] < cutoff, sample[keep] < cutoff
            scores.append(float((ref & ink).sum()) / max(1, (ref | ink).sum()))
        candidates.append((min(scores), x+dx, top, w, h))
    return max(candidates, default=(0, 0, 0, 0, 0))


def source_wave(image, rows, spacing, donors):
    left = min(int(r["x"]) for r in rows)
    highest = min(int(r["y"]) for r in rows)
    lowest = max(int(r["y"]) + int(r["h"]) for r in rows)
    x = round(left - 2.2 * spacing)
    y = round(highest - .4 * spacing)
    stop_y = round(lowest + 2.5 * spacing)
    roi = image[y:stop_y, x:round(left - .35 * spacing)]
    height = round(1.5 * spacing)
    answers = []
    for donor in donors:
        x0, y0, w, h = bounds(donor)
        if w > roi.shape[1] or h < height or roi.shape[0] < height:
            continue
        for dy in range(0, h-height+1, max(1, round(spacing/2))):
            patch = image[y0+dy:y0+dy+height, x0:x0+w]
            score = cv2.matchTemplate((roi < 160).astype("uint8"), (patch < 160).astype("uint8"),
                                      cv2.TM_CCOEFF_NORMED)
            _, metric, _, (xx, yy) = cv2.minMaxLoc(score)
            answers.append((float(metric), x+xx, y+yy, w, height, donor.get("id")))
    return max(answers, default=(0, 0, 0, 0, 0, ""))


def scan(job):
    receipt = json.loads((job/"source-native-notation.json").read_text(encoding="utf8"))
    ledger = Path(receipt["accepted_native_xml"]).with_name("head-ledger.tsv")
    if not ledger.is_file() and not receipt["stages"]:
        ledger=job/"source-native-notation/curve-overlap-ledger/head-ledger.tsv"
    if not ledger.is_file():
        return dict(standard_candidate_chords_scanned=0,findings=[],reason="source ledger unavailable")
    groups = defaultdict(list)
    with ledger.open(encoding="utf8",newline="") as stream:
        for r in csv.DictReader(stream, delimiter="\t"):
            if r["repeat_copy"]=="false" and r["shape"]=="NOTEHEAD_VOID":
                groups[(int(r["unit"]),r["system"],r["chord"])].append(r)
    found = []
    potential = 0
    unit, image, digest = None, None, None
    archive=Path(receipt["accepted_native_book"])
    with archive.open("rb") as stream:
        book_digest=file_digest(stream,"sha256").hexdigest()
    with ZipFile(archive) as book:
        for (page, system, chord), rows in sorted(groups.items()):
            if len(rows)!=2 or len({r["staff"] for r in rows})!=1:
                continue
            if page!=unit:
                unit=page
                data=(job/f"unit-{unit:03d}.png").read_bytes()
                digest=sha256(data).hexdigest()
                image=cv2.imdecode(np.frombuffer(data,np.uint8),0)
                sheet=ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml"))
            if any(r["source_sha256"]!=digest for r in rows):
                continue
            sy=sheet.find(f"page/system[@id='{system}']")
            part=next(
                (p for p in sy.findall("part") if any(s.get("id")==rows[0]["staff"] for s in p.findall("staff"))),None)
            if part is None or len(part.findall("staff"))!=2:
                continue
            staff=next(s for s in part.findall("staff") if s.get("id")==rows[0]["staff"])
            ys=[float(line.find("point").get("y")) for line in staff.findall("lines/line")]
            spacing=(max(ys)-min(ys))/4
            low=max(rows,key=lambda r:int(r["y"]))
            if abs(int(rows[0]["x"])-int(rows[1]["x"]))>1:
                continue
            relations=sy.findall("sig/relations/*")
            stems=[r.get("target") for r in relations if r.get("source")==chord and r.find("chord-stem") is not None]
            if len(stems)!=1:
                continue
            stem=sy.find(f"sig/inters/stem[@id='{stems[0]}']")
            endpoints=[float(n.get("y")) for n in stem.findall("median/*")]
            if len(endpoints)!=2:
                continue
            down=max(endpoints)-int(low["y"])-int(low["h"])/2
            if not 3.7*spacing<=down<=6.5*spacing:
                continue
            dots={r.get("target") for r in relations if r.find("augmentation") is not None}
            if any(r["head"] not in dots for r in rows):
                continue
            potential+=1
            scores={i:head_scores(image,low,spacing,i) for i in (1,2,3,4,5)}
            if scores[2][0]<.78 or scores[4][0]<.65 or max(scores[i][0] for i in (1,3,5))>=.30:
                continue
            x=int(low["x"])
            low_center=int(low["y"])+int(low["h"])/2
            accidentals=[n for n in sy.findall("sig/inters/alter") if n.get("staff")==low["staff"]]
            if any(x-3*spacing<=bounds(n)[0]<=x and low_center-.25*spacing<=bounds(n)[1]+bounds(n)[3]/2<=low_center+2.5*spacing
                   for n in accidentals):
                continue
            marks=[n for n in sy.findall("sig/inters/arpeggiato") if float(n.get("grade","0"))>=.65]
            wave=source_wave(image,rows,spacing,marks)
            if wave[0]<.87:
                continue
            found.append(dict(unit=unit,system=system,chord=chord,rows=rows,scores=scores,
                              source_wave=wave,interline=spacing,source_sha256=digest))
    return dict(standard_candidate_chords_scanned=potential,findings=found,source_book_sha256=book_digest)


if __name__=="__main__":
    import sys
    job=Path(sys.argv[1])
    print(json.dumps(scan(job),ensure_ascii=False,indent=2))

