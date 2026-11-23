"""Export a Sibelius playback copy with measured tremolos and jumps unfolded."""
from pathlib import Path
from xml.etree import ElementTree as ET

from source_tremolo_performance import expand_double_tremolos
from playback_repeat_order import expand
from musicxml_ties import normalize_playback_cross_voice_ties

SUFFIX = "_Sibelius播放"


def export_sibelius_playback_copy(source):
    source = Path(source).resolve(strict=True)
    if source.stem.endswith("_Sibelius打击乐") or SUFFIX in source.stem:
        raise ValueError("请从标准 MusicXML 导出 Sibelius 播放谱。")
    playback, report = expand_double_tremolos(ET.parse(source).getroot())
    if report["unresolved"]:
        measures = "、".join(dict.fromkeys(item["measure"] for item in report["unresolved"]))
        raise ValueError(f"第 {measures} 小节的震音仍需核对，原谱已保留。")
    playback, repeat_order = expand(playback)
    ties_changed = normalize_playback_cross_voice_ties(playback, repeat_order)
    if not report["changes"] and repeat_order is None and not ties_changed:
        return None
    content = ET.tostring(playback, encoding="utf-8", xml_declaration=True)
    for sequence in range(1, 1000):
        suffix = "" if sequence == 1 else f"_{sequence}"
        target = source.with_name(f"{source.stem}{SUFFIX}{suffix}.musicxml")
        try:
            with target.open("xb") as stream:
                stream.write(content)
                return target
        except FileExistsError:
            if target.read_bytes() == content:
                return target
    raise OSError("同名 Sibelius 播放谱过多，请另选导出文件夹。")
