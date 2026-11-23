"""Explicit score structures, applied before recognition; no music is discarded."""
SINGLE_STAFF_PROFILE = "single-staff"
SINGLE_STAFF_HINT = "Melody:1"

SATB = "Soprano|S.:1:lyrics; Alto|A.:1:lyrics; Tenor|T.|T:1:lyrics; Bass|B.:1:lyrics"
PRESETS = {"自动识别": "", "SATB＋钢琴（5声部）": SATB+"; Piano|Pn.|Pno.:2", "SATB＋打击乐＋钢琴（6声部）": SATB+"; Percussion|Dr.|打击乐:1:drums; Piano|Pn.|Pno.:2", "SATB无伴奏（4声部）": SATB,
           "男高＋男低＋钢琴（3声部）": "Tenor|T.|T:1:lyrics; Bass|B.|B:1:lyrics; Piano|Pn.|Pno.:2"}
def resolve_hint(profile, structure, custom):
    if profile == SINGLE_STAFF_PROFILE:
        return SINGLE_STAFF_HINT
    if custom.strip():return custom.strip()
    if profile == "piano":return "Piano:2"
    return PRESETS[structure]
def validate_preset(root, hint):
    if hint.strip() == SINGLE_STAFF_HINT:
        parts = root.findall("part")
        if (len(parts) != 1 or len(root.findall("part-list/score-part")) != 1
                or any(n.text != "1" for n in root.findall("part/measure/attributes/staves"))
                or any(n.text != "1" for n in root.findall("part/measure/note/staff"))):
            raise RuntimeError("单声部谱需要一个乐器声部和一条五线谱。检测到其他谱表，请选择适合原谱的谱种。")
        return
    expected = next((text for text,value in PRESETS.items() if value and value == hint.strip()), None)
    if expected is None:return
    names = [p.findtext("part-name", "").strip() for p in root.findall("part-list/score-part")]
    wanted = ["Tenor", "Bass"] if expected.startswith("男高＋男低") else ["Soprano", "Alto", "Tenor", "Bass"]
    if "打击乐" in expected:
        wanted.append("Percussion")
    if "钢琴" in expected:
        wanted.append("Piano")
    if names != wanted or len(root.findall("part")) != len(wanted):
        raise RuntimeError("声部结构与所选编制不符：预期"+"、".join(wanted)+"；实际"+"、".join(names)+"。原始识别及日志已保留，请核对原谱编制或使用高级声部设置；未删除或强行合并音乐。")
