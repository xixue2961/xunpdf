"""Export standard XUNpdf MusicXML to MIDI through the local renderer."""
import argparse
import base64
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent


def export_midi(source):
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    args = [str(python), "-B", str(Path(__file__).resolve()), str(Path(source).resolve())]
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "MIDI 导出程序未完成。")
    path = Path(result.stdout.strip())
    if not path.is_file():
        raise RuntimeError("MIDI 文件没有成功写入。")
    return path


def prepare_playback_xml(data):
    """Expand playback and prepare notation that the MIDI renderer consumes."""
    has_jump = "dalsegno=" in data and "tocoda=" in data
    has_tremolo = "<tremolo" in data
    has_swing = "<swing" in data
    has_ties = "<tie" in data
    has_tuplet_display_ratio = "<tuplet-actual" in data
    if not (has_jump or has_tremolo or has_swing or has_ties or has_tuplet_display_ratio):
        return data

    score = ET.fromstring(data)
    adjusted = False
    if has_tuplet_display_ratio:
        from final_export_checks import complete_triplet_display_ratios

        adjusted = bool(complete_triplet_display_ratios(score))
    order = None
    if has_jump:
        from playback_repeat_order import expand

        score, order = expand(score)
        adjusted = adjusted or order is not None
    if has_ties:
        from musicxml_ties import normalize_playback_cross_voice_ties

        if normalize_playback_cross_voice_ties(score, order):
            adjusted = True
    if has_swing:
        from source_swing_performance import realize_swing

        if realize_swing(score):
            adjusted = True
    if has_tremolo:
        from source_tremolo_performance import expand_double_tremolos, expand_rolled_tremolo_chords

        score, tremolos = expand_double_tremolos(score)
        rolls = expand_rolled_tremolo_chords(score)
        adjusted = adjusted or bool(tremolos["changes"]) or bool(rolls)
    return ET.tostring(score, encoding="unicode") if adjusted else data


def write_local_midi(source):
    source = Path(source).resolve(strict=True)
    if source.stem.endswith("_Sibelius打击乐"):
        raise ValueError("请从标准 MusicXML 导出 MIDI，Sibelius 打击乐副本采用专用鼓组键位。")
    runtime = ROOT / "render-runtime"
    bundles = runtime / "packages"
    bundled = (bundles / "verovio").is_dir()
    if bundled:
        sys.path.insert(0, str(bundles))
    import verovio

    if bundled and not Path(verovio.__file__).resolve().is_relative_to(bundles):
        raise RuntimeError("本地 MIDI 组件加载路径异常。")

    renderer = verovio.toolkit(False)
    # Use a relative resource path for the native Windows renderer.
    resources = (runtime / "verovio-6.4.1-data" if bundled
                 else Path(verovio.__file__).resolve().parent / "data")
    os.chdir(resources.parent)
    if not renderer.setResourcePath(resources.name):
        raise RuntimeError("本地 MIDI 字体和声音映射资源未加载。")
    renderer.setOptions({"inputFrom": "musicxml"})
    data = source.read_text(encoding="utf-8-sig")
    data = prepare_playback_xml(data)
    if not renderer.loadData(data):
        raise ValueError("无法读取识谱产生的 MusicXML。")
    midi = base64.b64decode(renderer.renderToMIDI(), validate=True)
    if len(midi) <= 22 or not midi.startswith(b"MThd"):
        raise RuntimeError("本地引擎未生成有效 MIDI。")

    from midi_tempo_positions import preserve_tempo_positions

    midi = preserve_tempo_positions(midi, data, renderer)

    from midi_expression_positions import add_crescendo_expression

    midi = add_crescendo_expression(midi, data, renderer)

    from midi_fermata_positions import render_fermatas

    midi = render_fermatas(midi, data, renderer)

    for sequence in range(1, 1000):
        suffix = "" if sequence == 1 else f"_{sequence}"
        output = source.with_name(f"{source.stem}{suffix}.mid")
        try:
            with output.open("xb") as handle:
                handle.write(midi)
            return output
        except FileExistsError:
            if output.read_bytes() == midi:
                return output
    raise RuntimeError("同名 MIDI 文件过多，请另选导出文件夹。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    try:
        output = write_local_midi(parser.parse_args().source)
        sys.stdout.reconfigure(encoding="utf-8")
        print(output)
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        sys.stderr.reconfigure(encoding="utf-8")
        parser.exit(1, f"{error}\n")
