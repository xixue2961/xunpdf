from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import csv
import sys

import numpy as np
from PIL import Image


def bounds(node):
    return tuple(int(node.find("bounds").get(k)) for k in "xywh")


def patch(image, node):
    x,y,w,h = bounds(node)
    return image[y:y+h,x:x+w]


def agreement(image, target, reference):
    left, right = patch(image, target), patch(image, reference)
    if left.shape != right.shape or left.size == 0:
        return None
    a, b = left.reshape(-1).astype(float), right.reshape(-1).astype(float)
    if min(a.std(), b.std()) < 20:
        return None
    gray = float(np.corrcoef(a,b)[0,1])
    thresholds = []
    for cut in (160,208):
        x,y = a < cut, b < cut
        if min(x.std(),y.std()) < .05:
            return None
        similarity = float(np.corrcoef(x,y)[0,1])
        common = np.logical_and(x,y).sum()
        thresholds.append((similarity, common/max(x.sum(),y.sum())))
    return gray, thresholds


def verified_match(image, target, donor):
    value = agreement(image,target,donor)
    return value is not None and value[0] >= .965 and all(
        correlation >= .90 and overlap >= .95 for correlation,overlap in value[1])


def systems(file, unit):
    return ET.fromstring(file.read(f"sheet#{unit}/sheet#{unit}.xml")).findall("page/system")


def verified_rests(raw, repaired, decisions, source):
    """Recover a missing first-bar rest only if raw glyph and staff font agree."""
    raw, repaired, decisions, source = map(Path,(raw,repaired,decisions,source))
    with decisions.open(encoding="utf-8",newline="") as stream:
        retired = [row for row in csv.DictReader(stream,delimiter="\t") if row["status"] == "retired"]
    output=[]
    with ZipFile(raw) as first, ZipFile(repaired) as second:
        for row in retired:
            unit, sid, staff_id = (row[k] for k in ("unit","system","staff"))
            original_systems = systems(first,unit)
            accepted_systems = systems(second,unit)
            original, accepted = original_systems[0], accepted_systems[0]
            if original.get("id") != sid or accepted.get("id") != sid:
                continue
            stack=accepted.find(f"stack[@id='{row['stack']}']")
            if stack is None or accepted.find("stack") is not stack:
                continue
            owners=[part for part in accepted.findall("part") if part.find(f"staff[@id='{staff_id}']") is not None]
            if len(owners)!=1 or len(owners[0].findall("staff"))!=2:
                continue
            measure=owners[0].find(f"measure[@id='{row['stack']}']")
            if measure is None:
                continue
            chords={n.get("id"):n for n in accepted.findall("sig/inters/head-chord") if n.get("staff")==staff_id}
            positions=sorted(bounds(chords[i])[0] for i in measure.findtext("head-chords","").split() if i in chords)
            if len(positions)!=2:
                continue
            existing={bounds(n) for n in accepted.findall("sig/inters/rest") if n.get("staff")==staff_id}
            candidates=[n for n in original.findall("sig/inters/rest[@shape='EIGHTH_REST']")
                        if n.get("staff")==staff_id and n.get("glyph") and bounds(n) not in existing
                        and positions[0]+bounds(n)[2] < bounds(n)[0] < positions[1]-bounds(n)[2]
                        and -.9<=float(n.get("pitch","99"))<=.9]
            if len(candidates)!=1:
                continue
            target=candidates[0]
            with Image.open(source/f"unit-{int(unit):03d}.png") as image:
                pixels=np.asarray(image.convert("L"))
            donors=[d for d in accepted.findall("sig/inters/rest[@shape='EIGHTH_REST']")
                    if d.get("staff")==staff_id and bounds(d)[0]>int(stack.get("right"))
                    and d.get("glyph") and verified_match(pixels,target,d)]
            if not donors:
                continue
            output.append((unit,sid,staff_id,row["stack"],target.get("id"),target.get("glyph"),*bounds(target),target.get("pitch")))
    return output


if __name__ == "__main__":
    result=verified_rests(*sys.argv[1:5])
    with Path(sys.argv[5]).open("x",encoding="utf-8") as stream:
        for entry in result:
            stream.write("\t".join(map(str,entry))+"\n")
    print(f"Restored source rest candidates: {len(result)}")

