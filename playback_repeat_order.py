"""Expand a validated D.S. al Coda route in the MIDI-only score."""
from collections import defaultdict
from copy import deepcopy
from xml.etree import ElementTree as ET


KEYS = ("segno", "tocoda", "dalsegno", "coda")
CONTEXT = ("divisions", "key", "time", "staves", "clef", "transpose")


def inherited(measures, stop):
    values = {}
    for measure in measures[:stop]:
        for attrs in measure.findall("attributes"):
            for item in attrs:
                if item.tag in CONTEXT:
                    values[item.tag, item.get("number")] = item
    return values


def expand(root):
    parts = root.findall("part")
    measures = [p.findall("measure") for p in parts]
    if not measures or any(len(p) != len(measures[0]) for p in measures):
        return root, None
    points = defaultdict(set)
    repeats = set()
    for seq in measures:
        for index, measure in enumerate(seq):
            for e in measure.findall("direction/sound"):
                for key in KEYS:
                    if key in e.attrib:
                        points[key].add((index, e.get(key)))
            for repeat in measure.findall("barline/repeat"):
                repeats.add((index, repeat.get("direction"), repeat.get("times")))
    if any(len(points[key]) != 1 for key in KEYS):
        return root, None
    (begin, left), (exit_index, right), (jump, target), (coda, end) = [
        next(iter(points[key])) for key in KEYS]
    if (left != target or right != end or not 0 <= begin < exit_index < jump < coda
            or any(item not in {(begin, "forward", None), (jump, "backward", "1")} for item in repeats)
            or any(m.find("barline/ending") is not None for m in root.findall("part/measure"))):
        return root, None
    order = [*range(jump + 1), *range(begin, exit_index), *range(coda, len(measures[0]))]
    result = deepcopy(root)
    for part, originals in zip(result.findall("part"), measures, strict=True):
        for old in part.findall("measure"):
            part.remove(old)
        preceding = None
        for index, source_index in enumerate(order):
            measure = deepcopy(originals[source_index])
            measure.set("number", str(index + 1))
            for direction in list(measure.findall("direction")):
                if any(key in e.attrib for e in direction.findall("sound") for key in KEYS):
                    measure.remove(direction)
            for barline in measure.findall("barline"):
                repeated = barline.findall("repeat")
                for repeat in repeated:
                    barline.remove(repeat)
                if (repeated and len(barline) == 1
                        and barline.findtext("bar-style") in ("heavy-light", "light-heavy")):
                    # Once the repeat is expanded, this is just its printed
                    # edge. A standalone final edge can add a bar in Sibelius.
                    measure.remove(barline)
            if preceding is not None and source_index != preceding + 1:
                values = inherited(originals, source_index)
                supplied = {(item.tag, item.get("number"))
                            for node in measure.findall("attributes") for item in node}
                needed = [(name, value) for (name, slot), value in values.items()
                          if (name, slot) not in supplied]
                if needed:
                    context = ET.Element("attributes")
                    for name in CONTEXT:
                        for tag, node in needed:
                            if tag == name:
                                context.append(deepcopy(node))
                    at = next((i for i, e in enumerate(measure) if e.tag != "print"), len(measure))
                    measure.insert(at, context)
            part.append(measure)
            preceding = source_index
    return result, order
