"""Create the optional Sibelius 2025.8 drum mapping copy of a XUNpdf export."""
from pathlib import Path
from xml.etree import ElementTree as ET


def export_sibelius_drum_copy(source):
    source = Path(source)
    if source.stem.endswith("_Sibelius打击乐"):
        raise ValueError("请用原始 MusicXML 生成 Sibelius 打击乐副本。")

    tree = ET.parse(source)
    changed = False
    for instrument in tree.findall("./part-list/score-part/midi-instrument"):
        unpitched = instrument.find("midi-unpitched")
        if instrument.findtext("midi-channel", "").strip() != "10" or unpitched is None:
            continue
        number = int(unpitched.text or "")
        if not 1 <= number <= 128:
            raise ValueError("打击乐的 MusicXML MIDI 键位超出范围。")
        if number > 1:
            unpitched.text = str(number - 1)
            changed = True

    if not changed:
        return None

    target = source.with_name(f"{source.stem}_Sibelius打击乐.musicxml")
    content = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    try:
        with target.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if target.read_bytes() != content:
            raise
    return target
